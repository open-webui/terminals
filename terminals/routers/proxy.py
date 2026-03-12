"""Catch-all reverse proxy into terminal instances.

Routing is based on the ``X-User-Id`` header — the caller (e.g. Open WebUI
backend) sets this header and the proxy resolves / provisions the correct
instance automatically.  The path structure mirrors open-terminal exactly
(``/execute``, ``/files/list``, …) so the two are interchangeable.

Named policy endpoints:
  /p/{policy_id}/*  — provisions using the named policy config
"""

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Optional

import httpx
import websockets
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import JSONResponse
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
        await backend.touch_activity(user_id)
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
    """Forward an HTTP request to the user's terminal instance."""
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

    body = await request.body()

    client = await _get_proxy_client()
    upstream = await client.request(
        method=request.method,
        url=target_url,
        headers=headers,
        content=body,
    )

    # Strip hop-by-hop from response too.
    response_headers = dict(upstream.headers)
    for h in ("transfer-encoding", "connection", "content-encoding", "content-length"):
        response_headers.pop(h, None)

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
    )


# ---------------------------------------------------------------------------
# OpenAPI spec passthrough (tool discovery — no X-User-Id required)
# ---------------------------------------------------------------------------

_SPEC_CACHE_TTL = 300  # seconds
_cached_spec: Optional[dict] = None
_cached_spec_ts: float = 0.0

_SYSTEM_USER_ID = "system"


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
    global _cached_spec, _cached_spec_ts

    now = time.monotonic()
    if _cached_spec is not None and (now - _cached_spec_ts) < _SPEC_CACHE_TTL:
        return JSONResponse(content=_cached_spec)

    # Provision a system instance to fetch the spec from.
    instance = await _resolve_instance(request, _SYSTEM_USER_ID)

    client = await _get_proxy_client()
    try:
        resp = await client.get(
            f"http://{instance.host}:{instance.port}/openapi.json",
            headers={"Authorization": f"Bearer {instance.api_key}"},
        )
        resp.raise_for_status()
        spec = resp.json()
    except Exception as e:
        logger.error("Failed to fetch OpenAPI spec from instance: {}", e)
        return JSONResponse(
            content={"error": "Failed to fetch OpenAPI spec from terminal instance"},
            status_code=502,
        )

    # Strip security schemes and per-operation security requirements.
    # Auth is handled transparently by the orchestrator proxy — the model
    # should not see or ask for Bearer tokens.
    spec.pop("security", None)
    spec.get("components", {}).pop("securitySchemes", None)
    for _path_methods in spec.get("paths", {}).values():
        for _op in _path_methods.values():
            if isinstance(_op, dict):
                _op.pop("security", None)

    _cached_spec = spec
    _cached_spec_ts = now

    return JSONResponse(content=spec)


# ---------------------------------------------------------------------------
# Named policy proxy — /p/{policy_id}/{path}
# ---------------------------------------------------------------------------


async def _resolve_policy_spec(policy_id: str) -> tuple[str, dict | None]:
    """Look up a policy by ID. Returns (policy_id, merged spec) or raises 404."""
    from terminals.db.session import async_session as db_session

    if db_session is None:
        return policy_id, None

    from terminals.models.policy import Policy
    from terminals.routers.policy import _merge_defaults

    async with db_session() as session:
        from sqlalchemy import select
        result = await session.execute(select(Policy).where(Policy.id == policy_id))
        policy = result.scalar_one_or_none()
        if policy is None:
            raise HTTPException(status_code=404, detail=f"Policy '{policy_id}' not found")

        return policy_id, _merge_defaults(policy.data or {})


@router.get(
    "/p/{policy_id}/openapi.json",
    dependencies=[Depends(verify_api_key)],
)
async def policy_openapi_spec(
    policy_id: str,
    request: Request,
):
    """Serve the OpenAPI spec via a named policy container."""
    _id, spec = await _resolve_policy_spec(policy_id)
    instance = await _resolve_instance(request, _SYSTEM_USER_ID, policy_id=_id, spec=spec)

    client = await _get_proxy_client()
    try:
        resp = await client.get(
            f"http://{instance.host}:{instance.port}/openapi.json",
            headers={"Authorization": f"Bearer {instance.api_key}"},
        )
        resp.raise_for_status()
        spec_data = resp.json()
    except Exception as e:
        logger.error("Failed to fetch OpenAPI spec from policy instance: {}", e)
        return JSONResponse(
            content={"error": "Failed to fetch OpenAPI spec"},
            status_code=502,
        )

    # Strip auth from spec — orchestrator handles auth transparently.
    spec_data.pop("security", None)
    spec_data.get("components", {}).pop("securitySchemes", None)
    for _path_methods in spec_data.get("paths", {}).values():
        for _op in _path_methods.values():
            if isinstance(_op, dict):
                _op.pop("security", None)

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
    ws: WebSocket, token: str, user_id: str
) -> Optional[str]:
    """Validate WS auth, return verified user_id or close the socket.

    Returns the effective user_id on success, or ``None`` if closed.
    """
    verified_user_id = None
    if settings.open_webui_url:
        try:
            verified_user_id = await validate_token(token)
        except Exception:
            await ws.close(code=4001, reason="Invalid token")
            return None
    elif settings.api_key and token != settings.api_key:
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
    await ws.accept()

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

    try:
        async with websockets.connect(upstream_url) as upstream:
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
    token: str = Query(""),
    user_id: str = Query(""),
):
    """Default WebSocket terminal proxy (no policy)."""
    effective_user = await _validate_ws_auth(ws, token, user_id)
    if effective_user is None:
        return
    await _ws_proxy_handler(ws, session_id, effective_user)


@router.websocket("/p/{policy_id}/api/terminals/{session_id}")
async def ws_policy_terminal_proxy(
    ws: WebSocket,
    policy_id: str,
    session_id: str,
    token: str = Query(""),
    user_id: str = Query(""),
):
    """Policy-scoped WebSocket terminal proxy."""
    effective_user = await _validate_ws_auth(ws, token, user_id)
    if effective_user is None:
        return

    try:
        _id, spec = await _resolve_policy_spec(policy_id)
    except HTTPException:
        await ws.close(code=4004, reason=f"Policy '{policy_id}' not found")
        return

    await _ws_proxy_handler(ws, session_id, effective_user, policy_id=_id, spec=spec)


