"""Reverse proxy to the selected template's environment.

The trusted caller supplies the template and verified user/chat/model context.
The template's stored scope determines the instance; paths mirror Open Terminal.
"""

import asyncio
import json
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

import httpx
import websockets
from websockets.exceptions import InvalidHandshake
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger
from starlette.background import BackgroundTask

from machines.backends.base import ProvisioningError
from machines.config import settings
from machines.utils.context import (
    CONTEXT_HEADERS, TEMPLATE_HEADERS, CHAT_HEADERS, MODEL_HEADERS, terminal_identity, get_terminal_id,
    DEFAULT_CONTEXT_ID,
)
from machines.utils.template_specs import TemplateNotFoundError, resolve_template_spec
from machines.routers.auth import verify_proxy_user, verify_proxy_credentials, require_server_key

router = APIRouter()

# ---------------------------------------------------------------------------
# Proxy client
# ---------------------------------------------------------------------------

_proxy_clients: dict[tuple[str, int], httpx.AsyncClient] = {}



async def _get_proxy_client(host: str, port: int) -> httpx.AsyncClient:
    client = _proxy_clients.pop((host, port), None)
    if client is None:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.proxy_connect_timeout_seconds,
                read=settings.proxy_read_timeout_seconds,
                write=settings.proxy_write_timeout_seconds,
                pool=settings.proxy_pool_timeout_seconds,
            ),
            limits=httpx.Limits(
                max_connections=32,
                max_keepalive_connections=4,
                keepalive_expiry=30.0,
            ),
        )
    _proxy_clients[(host, port)] = client
    if len(_proxy_clients) > 1200:
        await _proxy_clients.pop(next(iter(_proxy_clients))).aclose()
    return client


async def close_proxy_client() -> None:
    clients = list(_proxy_clients.values())
    _proxy_clients.clear()
    for client in clients:
        await client.aclose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------








def _routing_header(headers, names):
    current, legacy = (headers.get(name) for name in names)
    if current is not None and legacy is not None and current != legacy:
        raise HTTPException(400, f"Conflicting {names[0]} and {names[1]}")
    return current if current is not None else legacy


async def _request_target(request, user_id):
    template_id = _routing_header(request.headers, TEMPLATE_HEADERS)
    if not template_id:
        raise HTTPException(400, f"Missing {TEMPLATE_HEADERS[0]}")
    if any(request.headers.get(name) for name in CONTEXT_HEADERS):
        raise HTTPException(400, "Client-selected instance contexts are no longer supported")
    spec = await _resolve_template_spec(template_id)
    scope = (spec or {}).get("scope")
    context, _ = terminal_identity(template_id, scope, user_id=user_id,
                                   chat_id=_routing_header(request.headers, CHAT_HEADERS),
                                   model_id=_routing_header(request.headers, MODEL_HEADERS))
    return template_id, context


# ---------------------------------------------------------------------------
# Resolve instance
# ---------------------------------------------------------------------------


@dataclass
class InstanceInfo:
    """Resolved machine instance."""
    host: str
    port: int
    api_key: str


async def _touch_activity(backend, template_id, context_id):
    try:
        await backend.touch_activity(template_id, context_id=context_id)
    except Exception:
        logger.debug("Failed to persist activity for {}", template_id)


async def _resolve_instance(
    request,
    user_id: str,
    template_id: str,
    context_id: str = DEFAULT_CONTEXT_ID,
    existing_only: bool = False,
) -> InstanceInfo:
    """Return a running instance, auto-provisioning if needed."""
    backend = request.app.state.backend
    try:
        if existing_only:
            info = await backend.get_terminal_info(template_id=template_id, context_id=context_id)
            if info is None:
                raise HTTPException(status_code=409, detail="Machine is not running")
            return InstanceInfo(
                host=info["host"],
                port=info["port"], api_key=info["api_key"],
            )
        info = await backend.ensure_terminal(
            template_id=template_id, context_id=context_id, record_activity=True
        )
    except TemplateNotFoundError as exc:
        raise HTTPException(404, "Template not found") from exc
    except ProvisioningError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if info is None:
        raise RuntimeError(f"Failed to provision machine for user {user_id}")
    return InstanceInfo(
        host=info["host"],
        port=info["port"],
        api_key=info["api_key"],
    )


async def _try_resolve_instance(
    conn, user_id: str, template_id: str, context_id: str, *, reraise_http: bool = True
) -> Optional[InstanceInfo]:
    """Like _resolve_instance, but returns None instead of raising (except HTTP errors, if asked)."""
    try:
        return await _resolve_instance(conn, user_id, template_id=template_id, context_id=context_id)
    except HTTPException:
        if reraise_http:
            raise
        logger.error("Re-resolving machine for user {} was rejected", user_id)
    except Exception as exc:
        logger.error("Could not re-resolve machine for user {}: {}", user_id, exc)
    return None


# ---------------------------------------------------------------------------
# Internal proxy helper
# ---------------------------------------------------------------------------


async def _proxy_request(
    request: Request, user_id: str, path: str,
    background_tasks: Optional[BackgroundTasks] = None,
    *,
    template_id: str,
    context_id: str = DEFAULT_CONTEXT_ID,
) -> Response:
    """Forward an HTTP request to the user's machine instance.

    Streams both the request body (upload) and response body (download)
    to avoid buffering large payloads in memory.
    """
    # Inspecting machine health must never provision, wake, or keep an instance alive.
    existing_only = path.rstrip("/") == "system/info"
    instance = await _resolve_instance(
        request, user_id, template_id=template_id, context_id=context_id,
        existing_only=existing_only,
    )

    target_url = f"http://{instance.host}:{instance.port}/{path}"
    if request.query_params:
        target_url += f"?{request.query_params}"

    headers = dict(request.headers)
    # Replace auth with the instance's own API key.
    headers["authorization"] = f"Bearer {instance.api_key}"
    # Remove hop-by-hop / routing headers.
    for name in ("host", "transfer-encoding", "connection", "x-user-id",
                 *CONTEXT_HEADERS, *TEMPLATE_HEADERS, *CHAT_HEADERS, *MODEL_HEADERS):
        headers.pop(name.lower(), None)

    raw_content_length = request.headers.get("content-length")
    try:
        content_length = int(raw_content_length) if raw_content_length else None
    except ValueError:
        content_length = None
    stream_body = (
        (
            settings.replay_body_limit is not None
            and content_length is not None
            and content_length > settings.replay_body_limit
        )
        or request.headers.get("transfer-encoding") is not None
    )
    body = None if stream_body else await request.body()

    client = await _get_proxy_client(instance.host, instance.port)

    # Retry on connection errors — the container may still be starting.
    max_retries = 1 if stream_body or existing_only else 5
    for attempt in range(max_retries):
        try:
            content = request.stream() if stream_body else body
            upstream_resp = await client.send(
                client.build_request(
                    method=request.method,
                    url=target_url,
                    headers=headers,
                    content=content,
                ),
                stream=True,
            )
            break
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            request.app.state.backend.invalidate_status(template_id, context_id)
            if attempt < max_retries - 1:
                logger.debug("Proxy attempt {} to {} failed ({}), retrying...", attempt + 1, target_url, e)
                await asyncio.sleep(1)
                instance = await _try_resolve_instance(request, user_id, template_id, context_id)
                if instance is not None:
                    target_url = f"http://{instance.host}:{instance.port}/{path}"
                    if request.query_params:
                        target_url += f"?{request.query_params}"
                    headers["authorization"] = f"Bearer {instance.api_key}"
                    client = await _get_proxy_client(instance.host, instance.port)
                    continue
            else:
                logger.error("Proxy request to {} failed after {} retries: {}", target_url, max_retries, e)
            return Response(
                content='{"error": "Machine is unreachable"}',
                status_code=502,
                media_type="application/json",
            )
        except httpx.ReadTimeout as e:
            logger.warning(
                "Proxy request to {} timed out waiting for upstream response: {}",
                target_url,
                e,
            )
            return Response(
                content='{"error": "Upstream response timed out"}',
                status_code=504,
                media_type="application/json",
            )
        except (httpx.RemoteProtocolError, httpx.ReadError) as e:
            request.app.state.backend.invalidate_status(
                template_id, context_id
            )
            if attempt < max_retries - 1:
                logger.debug(
                    "Proxy attempt {} to {} hit a stale upstream connection ({}), retrying...",
                    attempt + 1,
                    target_url,
                    e,
                )
                instance = await _try_resolve_instance(request, user_id, template_id, context_id)
                if instance is None:
                    return Response(
                        content='{"error": "Upstream connection failed"}',
                        status_code=502,
                        media_type="application/json",
                    )
                target_url = f"http://{instance.host}:{instance.port}/{path}"
                if request.query_params:
                    target_url += f"?{request.query_params}"
                headers["authorization"] = f"Bearer {instance.api_key}"
                client = await _get_proxy_client(instance.host, instance.port)
            else:
                logger.error("Proxy request to {} failed after {} retries: {}", target_url, max_retries, e)
                return Response(
                    content='{"error": "Upstream connection failed"}',
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
        background=BackgroundTask(upstream_resp.aclose),
    )


# ---------------------------------------------------------------------------
# OpenAPI spec passthrough (tool discovery uses the same verified context as execution)
# ---------------------------------------------------------------------------

# Max retries when waiting for a freshly provisioned container to become ready.
_SPEC_FETCH_RETRIES = 10
_SPEC_FETCH_DELAY = 1  # seconds between retries


async def _fetch_spec_with_retry(instance: InstanceInfo) -> dict:
    """Fetch the OpenAPI spec from an instance, retrying on connection errors.

    Newly provisioned containers may need a few seconds to start serving.
    This retries with a short delay to avoid returning 502 on first access.
    """
    client = await _get_proxy_client(instance.host, instance.port)
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


async def _resolve_template_spec(template_id):
    try:
        return await resolve_template_spec(template_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except TemplateNotFoundError as exc:
        raise HTTPException(404, "Template not found") from exc


@router.get("/openapi.json")
async def get_openapi_spec(request: Request, user_id: str = Depends(verify_proxy_user)):
    template_id, context_id = await _request_target(request, user_id)
    instance = await _resolve_instance(request, user_id, template_id, context_id)
    return JSONResponse(content=_strip_auth_from_spec(await _fetch_spec_with_retry(instance)))


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def proxy(path: str, request: Request, background_tasks: BackgroundTasks,
                x_user_id: str = Depends(verify_proxy_user)):
    template_id, context_id = await _request_target(request, x_user_id)
    return await _proxy_request(request, x_user_id, path, background_tasks,
                                template_id=template_id, context_id=context_id)


# ---------------------------------------------------------------------------
# WebSocket proxy
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
        ws.state.auth = payload
    except (asyncio.TimeoutError, json.JSONDecodeError):
        await ws.close(code=4001, reason="Auth timeout or invalid payload")
        return None
    except Exception:
        await ws.close(code=4001, reason="Auth failed")
        return None

    try:
        require_server_key(token)
    except HTTPException:
        await ws.close(code=4001, reason="Invalid server key")
        return None

    user_id = ws.headers.get("x-user-id") or user_id
    if not user_id:
        await ws.close(code=4002, reason="Missing user_id")
        return None

    return user_id


async def _relay_websocket(
    ws: WebSocket, path: str, user_id: str, *, template_id: str, context_id: str = DEFAULT_CONTEXT_ID
) -> None:
    try:
        instance = await _resolve_instance(
            ws, user_id, template_id=template_id, context_id=context_id
        )
    except Exception as e:
        logger.error("Failed to resolve instance for WS: {}", e)
        await ws.close(code=4003, reason="Failed to resolve machine")
        return

    # Retry WebSocket connection — the container may still be starting.
    max_retries = 5
    upstream = None
    for attempt in range(max_retries):
        upstream_url = f"ws://{instance.host}:{instance.port}/{quote(path, safe='/')}"
        if ws.query_params:
            upstream_url += f"?{ws.query_params}"
        try:
            upstream = await websockets.connect(
                upstream_url,
                additional_headers={"X-Session-Id": ws.headers.get("x-session-id", "")},
                compression="deflate" if settings.ws_compression else None,
                max_size=20 * 1024 * 1024,
            )
            break
        except (
            ConnectionRefusedError,
            OSError,
            InvalidHandshake,
        ) as e:
            ws.app.state.backend.invalidate_status(template_id, context_id)
            if attempt < max_retries - 1:
                logger.debug("WS connect attempt {} to {} failed ({}), retrying...", attempt + 1, upstream_url, e)
                await asyncio.sleep(1)
                instance = await _try_resolve_instance(ws, user_id, template_id, context_id, reraise_http=False)
                if instance is not None:
                    continue
            else:
                logger.error("WS connect to {} failed after {} retries: {}", upstream_url, max_retries, e)
            await ws.close(code=4003, reason="Machine is unreachable")
            return

    try:
        async with upstream:
            # Browser views authenticate upstream with the capability in the URL.
            if not path.startswith("browser/view/"):
                await upstream.send(json.dumps({
                    **ws.state.auth,
                    "token": instance.api_key,
                    "chat_id": ws.headers.get("x-session-id", ""),
                }))

            backend = ws.app.state.backend

            # Long-lived sockets count as activity for as long as frames flow,
            # so the idle reaper doesn't tear down a machine in use. Client
            # keepalive pings don't count, or a forgotten tab would never idle.
            async def _client_to_upstream():
                try:
                    while True:
                        msg = await ws.receive()
                        if msg["type"] == "websocket.disconnect":
                            break
                        if msg.get("text") != '{"type":"ping"}':
                            await _touch_activity(backend, template_id, context_id)
                        if "bytes" in msg and msg["bytes"]:
                            await upstream.send(msg["bytes"])
                        elif "text" in msg and msg["text"]:
                            await upstream.send(msg["text"])
                except Exception as e:
                    logger.debug("client→upstream closed: {}", e)

            async def _upstream_to_client():
                try:
                    async for message in upstream:
                        await _touch_activity(backend, template_id, context_id)
                        if isinstance(message, bytes):
                            await ws.send_bytes(message)
                        else:
                            await ws.send_text(message)
                except Exception as e:
                    logger.debug("upstream→client closed: {}", e)

            tasks = [
                asyncio.create_task(_client_to_upstream()),
                asyncio.create_task(_upstream_to_client()),
            ]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

            # Preserve the upstream reconnect signal; 1005 and 1006 cannot be sent.
            if upstream.close_code not in (None, 1005, 1006):
                try:
                    await ws.close(code=upstream.close_code, reason=upstream.close_reason)
                except Exception:
                    pass
    except Exception as e:
        logger.error("Machine WebSocket proxy error: {}", e)


@router.websocket("/{path:path}")
async def ws_proxy(ws: WebSocket, path: str, user_id: str = Query("")):
    if path.startswith("browser/view/"):
        try:
            effective_user = await verify_proxy_credentials(ws.headers.get("authorization"), ws.headers.get("x-user-id"))
        except HTTPException as exc:
            await ws.close(code=4001, reason=str(exc.detail))
            return
        await ws.accept()
    else:
        effective_user = await _validate_ws_auth(ws, user_id)
        if effective_user is None:
            return
    try:
        template_id, context_id = await _request_target(ws, effective_user)
    except HTTPException as exc:
        await ws.close(code=4004, reason=str(exc.detail))
        return
    key = get_terminal_id(template_id, context_id)
    _instance_sockets.setdefault(key, set()).add(ws)
    try:
        await _relay_websocket(ws, path, effective_user, template_id=template_id, context_id=context_id)
    finally:
        try:
            await ws.close()
        except Exception:
            pass
        sockets = _instance_sockets.get(key)
        if sockets is not None:
            sockets.discard(ws)
            if not sockets:
                _instance_sockets.pop(key, None)


_instance_sockets = {}


async def close_instance_sessions(key):
    for socket in list(_instance_sockets.pop(key, ())):
        try:
            await socket.close(code=1012, reason="Machine restarted")
        except Exception:
            pass
