"""Catch-all reverse proxy into terminal instances.

Routing is based on the ``X-User-Id`` header — the caller (e.g. Open WebUI
backend) sets this header and the proxy resolves / provisions the correct
instance automatically.  The path structure mirrors open-terminal exactly
(``/execute``, ``/files/list``, …) so the two are interchangeable.

Named policy endpoints:
  /p/{policy_id}/*  — provisions using the named policy config
"""

import asyncio
import hmac
import json
import time
from dataclasses import dataclass
from typing import Optional

import httpx
import websockets
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger

from terminals.config import settings
from terminals.routers.auth import validate_token, verify_api_key, verify_user_id

router = APIRouter()

# ---------------------------------------------------------------------------
# Proxy client
# ---------------------------------------------------------------------------

_proxy_client: Optional[httpx.AsyncClient] = None

# Active WebSocket connection counter (for stats endpoint)
active_ws_connections: int = 0


async def _get_proxy_client() -> httpx.AsyncClient:
    global _proxy_client
    if _proxy_client is None:
        _proxy_client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))
    return _proxy_client


async def close_proxy_client() -> None:
    global _proxy_client
    if _proxy_client is not None:
        await _proxy_client.aclose()
        _proxy_client = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client_ip(request) -> str:
    """Extract client IP, respecting X-Forwarded-For."""
    headers = getattr(request, "headers", {})
    forwarded = headers.get("x-forwarded-for") if hasattr(headers, "get") else None
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = getattr(request, "client", None)
    return client.host if client else ""


def _user_agent(request) -> str:
    headers = getattr(request, "headers", {})
    return headers.get("user-agent", "") if hasattr(headers, "get") else ""


def _request_id(request) -> Optional[str]:
    return getattr(getattr(request, "state", None), "request_id", None)


# ---------------------------------------------------------------------------
# Resolve instance
# ---------------------------------------------------------------------------


@dataclass
class InstanceInfo:
    """Resolved terminal instance."""
    instance_id: str
    host: str
    port: int
    api_key: str


async def _resolve_instance(
    request,
    user_id: str,
    policy_id: str = "default",
    spec: Optional[dict] = None,
) -> InstanceInfo:
    """Return a running instance, auto-provisioning if needed."""
    backend = request.app.state.backend
    info = await backend.ensure_terminal(user_id, policy_id=policy_id, spec=spec)
    if info is None:
        raise RuntimeError(f"Failed to provision terminal for user {user_id}")
    try:
        await backend.touch_activity(user_id, policy_id=policy_id)
    except Exception:
        logger.debug("touch_activity failed for user {}", user_id)
    return InstanceInfo(
        instance_id=info["instance_id"],
        host=info["host"],
        port=info["port"],
        api_key=info["api_key"],
    )


# ---------------------------------------------------------------------------
# Internal proxy helper
# ---------------------------------------------------------------------------


async def _proxy_request(
    request: Request, user_id: str, path: str,
    background_tasks: Optional[BackgroundTasks] = None,
    policy_id: str = "default",
    spec: Optional[dict] = None,
) -> Response:
    """Forward an HTTP request to the user's terminal instance.

    Streams both the request body (upload) and response body (download)
    to avoid buffering large payloads in memory.
    """
    instance = await _resolve_instance(
        request, user_id, policy_id=policy_id, spec=spec,
    )

    target_url = f"http://{instance.host}:{instance.port}/{path}"
    if request.query_params:
        target_url += f"?{request.query_params}"

    headers = dict(request.headers)
    # Replace auth with the instance's own API key.
    headers["authorization"] = f"Bearer {instance.api_key}"
    # Remove hop-by-hop / routing headers.
    for h in ("host", "transfer-encoding", "connection", "x-user-id"):
        headers.pop(h, None)

    # Read the request body upfront (needed for retries).
    body = await request.body()

    client = await _get_proxy_client()

    # Retry on connection errors — the container may still be starting.
    max_retries = 5
    for attempt in range(max_retries):
        try:
            upstream_resp = await client.send(
                client.build_request(
                    method=request.method,
                    url=target_url,
                    headers=headers,
                    content=body,
                ),
                stream=True,
            )
            break
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            if attempt < max_retries - 1:
                logger.debug("Proxy attempt {} to {} failed ({}), retrying...", attempt + 1, target_url, e)
                await asyncio.sleep(1)
            else:
                logger.error("Proxy request to {} failed after {} retries: {}", target_url, max_retries, e)
                return Response(
                    content='{"error": "Terminal instance not reachable"}',
                    status_code=502,
                    media_type="application/json",
                )

    # Strip hop-by-hop from response.
    response_headers = dict(upstream_resp.headers)
    for h in ("transfer-encoding", "connection", "content-encoding"):
        response_headers.pop(h, None)

    return StreamingResponse(
        content=upstream_resp.aiter_bytes(),
        status_code=upstream_resp.status_code,
        headers=response_headers,
        background=BackgroundTasks([upstream_resp.aclose]),
    )


# ---------------------------------------------------------------------------
# OpenAPI spec passthrough (tool discovery — no X-User-Id required)
# ---------------------------------------------------------------------------

_SPEC_CACHE_TTL = 300  # seconds
_spec_cache: dict[str, tuple[float, dict]] = {}  # key → (timestamp, spec)

_SYSTEM_USER_ID = "system"

# Max retries when waiting for a freshly provisioned container to become ready.
_SPEC_FETCH_RETRIES = 10
_SPEC_FETCH_DELAY = 1  # seconds between retries


async def _fetch_spec_with_retry(instance: InstanceInfo) -> dict:
    """Fetch the OpenAPI spec from an instance, retrying on connection errors.

    Newly provisioned containers may need a few seconds to start serving.
    This retries with a short delay to avoid returning 502 on first access.
    """
    client = await _get_proxy_client()
    url = f"http://{instance.host}:{instance.port}/openapi.json"
    headers = {"Authorization": f"Bearer {instance.api_key}"}

    last_err = None
    for attempt in range(_SPEC_FETCH_RETRIES):
        try:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_err = e
            if attempt < _SPEC_FETCH_RETRIES - 1:
                logger.debug("Spec fetch attempt {} failed ({}), retrying...", attempt + 1, e)
                await asyncio.sleep(_SPEC_FETCH_DELAY)
    raise last_err


def _strip_auth_from_spec(spec: dict) -> dict:
    """Remove security schemes — auth is handled transparently by the proxy.

    Returns a shallow copy to avoid mutating the cached dict.
    """
    spec = {**spec}
    spec.pop("security", None)
    components = spec.get("components")
    if components:
        spec["components"] = {**components}
        spec["components"].pop("securitySchemes", None)
    paths = spec.get("paths")
    if paths:
        new_paths = {}
        for path_key, path_methods in paths.items():
            new_methods = {}
            for method, op in path_methods.items():
                if isinstance(op, dict) and "security" in op:
                    op = {k: v for k, v in op.items() if k != "security"}
                new_methods[method] = op
            new_paths[path_key] = new_methods
        spec["paths"] = new_paths
    return spec


async def _get_cached_spec(
    request: Request,
    policy_id: str = "default",
    spec: Optional[dict] = None,
) -> dict:
    """Return cached OpenAPI spec, fetching from a system instance if stale."""
    now = time.monotonic()
    cached = _spec_cache.get(policy_id)
    if cached and (now - cached[0]) < _SPEC_CACHE_TTL:
        return cached[1]

    instance = await _resolve_instance(
        request, _SYSTEM_USER_ID, policy_id=policy_id, spec=spec,
    )
    raw = await _fetch_spec_with_retry(instance)
    stripped = _strip_auth_from_spec(raw)
    _spec_cache[policy_id] = (now, stripped)
    return stripped


@router.get(
    "/openapi.json",
    dependencies=[Depends(verify_api_key)],
)
async def get_openapi_spec(request: Request):
    """Return the open-terminal OpenAPI spec.

    The spec is fetched from a running instance and cached for 5 minutes.
    This endpoint does **not** require ``X-User-Id`` — it is used by
    Open WebUI for tool discovery before any user context exists.
    """
    try:
        spec = await _get_cached_spec(request)
    except Exception as e:
        logger.error("Failed to fetch OpenAPI spec from instance: {}", e)
        return JSONResponse(
            content={"error": "Failed to fetch OpenAPI spec from terminal instance"},
            status_code=502,
        )
    return JSONResponse(content=spec)


# ---------------------------------------------------------------------------
# Named policy proxy — /p/{policy_id}/{path}
# ---------------------------------------------------------------------------

_POLICY_CACHE_TTL = 60  # seconds
_policy_cache: dict[str, tuple[float, tuple[str, dict | None]]] = {}


async def _resolve_policy_spec(policy_id: str) -> tuple[str, dict | None]:
    """Look up a policy by ID (cached for 60s). Returns (policy_id, merged spec)."""
    now = time.monotonic()
    cached = _policy_cache.get(policy_id)
    if cached and (now - cached[0]) < _POLICY_CACHE_TTL:
        return cached[1]

    from terminals.db.session import async_session as db_session

    if db_session is None:
        result = (policy_id, None)
        _policy_cache[policy_id] = (now, result)
        return result

    from terminals.models.policy import Policy
    from terminals.routers.policy import _merge_defaults

    async with db_session() as session:
        from sqlalchemy import select
        row = await session.execute(select(Policy).where(Policy.id == policy_id))
        policy = row.scalar_one_or_none()
        if policy is None:
            raise HTTPException(status_code=404, detail=f"Policy '{policy_id}' not found")

        result = (policy_id, _merge_defaults(policy.data or {}))
        _policy_cache[policy_id] = (now, result)
        return result


@router.get(
    "/p/{policy_id}/openapi.json",
    dependencies=[Depends(verify_api_key)],
)
async def policy_openapi_spec(
    policy_id: str,
    request: Request,
):
    """Serve the OpenAPI spec via a named policy container (cached)."""
    _id, spec = await _resolve_policy_spec(policy_id)

    try:
        spec_data = await _get_cached_spec(request, policy_id=_id, spec=spec)
    except Exception as e:
        logger.error("Failed to fetch OpenAPI spec from policy instance: {}", e)
        return JSONResponse(
            content={"error": "Failed to fetch OpenAPI spec"},
            status_code=502,
        )

    return JSONResponse(content=spec_data)


@router.api_route(
    "/p/{policy_id}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def policy_proxy(
    policy_id: str,
    path: str,
    request: Request,
    background_tasks: BackgroundTasks,
    x_user_id: str = Depends(verify_user_id),
):
    """Proxy a request through a named policy endpoint."""
    _id, spec = await _resolve_policy_spec(policy_id)
    return await _proxy_request(
        request, x_user_id, path, background_tasks,
        policy_id=_id, spec=spec,
    )


# ---------------------------------------------------------------------------
# Header-based catch-all proxy — default endpoint (backward compat)
# ---------------------------------------------------------------------------


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def proxy(
    path: str,
    request: Request,
    background_tasks: BackgroundTasks,
    x_user_id: str = Depends(verify_user_id),
):
    """Reverse-proxy any request into the user's Open Terminal instance.

    Uses settings.* defaults — no policy lookup.
    """
    return await _proxy_request(request, x_user_id, path, background_tasks)


# ---------------------------------------------------------------------------
# WebSocket proxy for interactive terminal sessions
# ---------------------------------------------------------------------------


async def _validate_ws_auth(
    ws: WebSocket, user_id: str
) -> Optional[str]:
    """Accept WebSocket and validate via first-message auth.

    After connecting, the client must send a JSON text frame::

        {"type": "auth", "token": "<api_key_or_jwt>"}

    Returns the effective user_id on success, or ``None`` after closing
    the socket with an appropriate error code.
    """
    await ws.accept()

    # First-message authentication
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=10.0)
        payload = json.loads(raw)
        if payload.get("type") != "auth":
            await ws.close(code=4001, reason="Expected auth message")
            return None
        token = payload.get("token", "")
    except (asyncio.TimeoutError, json.JSONDecodeError):
        await ws.close(code=4001, reason="Auth timeout or invalid payload")
        return None
    except Exception:
        await ws.close(code=4001, reason="Auth failed")
        return None

    verified_user_id = None
    if settings.open_webui_url:
        try:
            verified_user_id = await validate_token(token)
        except Exception:
            await ws.close(code=4001, reason="Invalid token")
            return None
    elif settings.api_key and not hmac.compare_digest(token, settings.api_key):
        await ws.close(code=4001, reason="Invalid API key")
        return None

    if not user_id:
        await ws.close(code=4002, reason="Missing user_id")
        return None

    if verified_user_id is not None and verified_user_id != user_id:
        await ws.close(code=4003, reason="user_id does not match authenticated identity")
        return None

    return user_id


async def _ws_proxy_handler(
    ws: WebSocket,
    session_id: str,
    user_id: str,
    policy_id: str = "default",
    spec: Optional[dict] = None,
):
    """Core WebSocket proxy logic, shared by default and policy routes."""
    global active_ws_connections
    active_ws_connections += 1

    try:
        instance = await _resolve_instance(
            ws, user_id, policy_id=policy_id, spec=spec
        )
    except Exception as e:
        logger.error("Failed to resolve instance for WS: {}", e)
        await ws.close(code=4003, reason="Failed to resolve terminal instance")
        return

    upstream_url = f"ws://{instance.host}:{instance.port}/api/terminals/{session_id}"

    # Retry WebSocket connection — the container may still be starting.
    max_retries = 5
    upstream = None
    for attempt in range(max_retries):
        try:
            upstream = await websockets.connect(upstream_url)
            break
        except (ConnectionRefusedError, OSError) as e:
            if attempt < max_retries - 1:
                logger.debug("WS connect attempt {} to {} failed ({}), retrying...", attempt + 1, upstream_url, e)
                await asyncio.sleep(1)
            else:
                logger.error("WS connect to {} failed after {} retries: {}", upstream_url, max_retries, e)
                await ws.close(code=4003, reason="Terminal instance not reachable")
                return

    try:
        async with upstream:
            await upstream.send(json.dumps({"type": "auth", "token": instance.api_key}))

            async def _client_to_upstream():
                try:
                    while True:
                        msg = await ws.receive()
                        if msg["type"] == "websocket.disconnect":
                            break
                        elif "bytes" in msg and msg["bytes"]:
                            await upstream.send(msg["bytes"])
                        elif "text" in msg and msg["text"]:
                            await upstream.send(msg["text"])
                except Exception as e:
                    logger.debug("client→upstream closed: {}", e)

            async def _upstream_to_client():
                try:
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await ws.send_bytes(message)
                        else:
                            await ws.send_text(message)
                except Exception as e:
                    logger.debug("upstream→client closed: {}", e)

            await asyncio.gather(
                _client_to_upstream(),
                _upstream_to_client(),
                return_exceptions=True,
            )
    except Exception as e:
        logger.error("WebSocket terminal proxy error: {}", e)
    finally:
        active_ws_connections -= 1
        try:
            await ws.close()
        except Exception:
            pass


@router.websocket("/api/terminals/{session_id}")
async def ws_terminal_proxy(
    ws: WebSocket,
    session_id: str,
    user_id: str = Query(""),
):
    """Default WebSocket terminal proxy (no policy)."""
    effective_user = await _validate_ws_auth(ws, user_id)
    if effective_user is None:
        return
    await _ws_proxy_handler(ws, session_id, effective_user)


@router.websocket("/p/{policy_id}/api/terminals/{session_id}")
async def ws_policy_terminal_proxy(
    ws: WebSocket,
    policy_id: str,
    session_id: str,
    user_id: str = Query(""),
):
    """Policy-scoped WebSocket terminal proxy."""
    effective_user = await _validate_ws_auth(ws, user_id)
    if effective_user is None:
        return

    try:
        _id, spec = await _resolve_policy_spec(policy_id)
    except HTTPException:
        await ws.close(code=4004, reason=f"Policy '{policy_id}' not found")
        return

    await _ws_proxy_handler(ws, session_id, effective_user, policy_id=_id, spec=spec)


