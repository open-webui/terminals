"""Terminals Orchestrator — FastAPI app that provisions and proxies to per-user Open Terminal pods.

Open WebUI connects to this as a single ``TERMINAL_SERVER_CONNECTIONS`` entry.
The orchestrator:
  1. Extracts ``X-User-Id`` from the incoming request (set by Open WebUI's proxy)
  2. Ensures a Terminal CRD exists for that user (creates one on first request)
  3. Waits for the terminal pod to become Ready
  4. Proxies the request to the user's Open Terminal pod
  5. Touches ``lastActivityAt`` on each request to prevent idle culling
"""

import asyncio
import logging

import aiohttp
import uvicorn
from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from terminals.config import API_KEY, HOST, PORT, PROVISION_TIMEOUT_SECONDS
from terminals.k8s import (
    delete_terminal,
    ensure_terminal,
    get_api_key,
    get_terminal,
    touch_activity,
    wait_for_ready,
)

log = logging.getLogger(__name__)

app = FastAPI(
    title="Terminals Orchestrator",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,  # Disable built-in OpenAPI — let the catch-all proxy serve the terminal's spec
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


STREAMING_CONTENT_TYPES = ("application/octet-stream", "image/", "application/pdf")
STRIPPED_RESPONSE_HEADERS = frozenset(
    ("transfer-encoding", "connection", "content-encoding", "content-length")
)


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------


def _authenticate(request: Request) -> bool:
    """Validate the bearer token from Open WebUI matches our API key."""
    if not API_KEY:
        return True  # No key configured — accept all (dev mode)
    auth = request.headers.get("authorization", "")
    return auth == f"Bearer {API_KEY}"


def _get_user_id(request: Request) -> str | None:
    """Extract user ID from the X-User-Id header set by Open WebUI's terminal proxy."""
    return request.headers.get("x-user-id")


# ---------------------------------------------------------------------------
# Resolve user terminal — get or create, wait for ready
# ---------------------------------------------------------------------------


async def _resolve_terminal(user_id: str) -> tuple[str, str] | None:
    """Ensure a terminal exists and is running. Returns (service_url, api_key) or None."""
    terminal = ensure_terminal(user_id)
    status = terminal.get("status") or {}
    phase = status.get("phase")

    if phase != "Running":
        terminal = await wait_for_ready(user_id, timeout=PROVISION_TIMEOUT_SECONDS)
        if terminal is None:
            return None
        status = terminal.get("status") or {}

    service_url = status.get("serviceUrl")
    secret_name = status.get("apiKeySecret")
    if not service_url or not secret_name:
        return None

    api_key = get_api_key(secret_name)
    if not api_key:
        return None

    return service_url, api_key


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/openapi.json", include_in_schema=False)
async def openapi_spec():
    """Proxy the Open Terminal OpenAPI spec from any running terminal pod.

    Open WebUI fetches this to discover available tools. This endpoint
    doesn't require auth or X-User-Id since it returns a static schema.
    The spec is cached after the first successful fetch.
    """
    global _cached_openapi_spec
    if _cached_openapi_spec is not None:
        return JSONResponse(_cached_openapi_spec)

    # Find any Running terminal to fetch the spec from
    from terminals.k8s import _list_running_terminals
    terminals = _list_running_terminals()
    if not terminals:
        return JSONResponse(
            {"error": "No running terminals available to fetch spec from"},
            status_code=503,
        )

    for terminal in terminals:
        status = terminal.get("status") or {}
        service_url = status.get("serviceUrl")
        secret_name = status.get("apiKeySecret")
        if not service_url or not secret_name:
            continue
        api_key = get_api_key(secret_name)
        if not api_key:
            continue

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(
                    f"{service_url.rstrip('/')}/openapi.json",
                    headers={"Authorization": f"Bearer {api_key}"},
                ) as resp:
                    if resp.status == 200:
                        _cached_openapi_spec = await resp.json()
                        return JSONResponse(_cached_openapi_spec)
        except Exception:
            continue

    return JSONResponse({"error": "Could not fetch spec from any terminal"}, status_code=503)


_cached_openapi_spec: dict | None = None


@app.get("/")
async def get_status(request: Request):
    """Return the terminal status for the authenticated user."""
    if not _authenticate(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    user_id = _get_user_id(request)
    if not user_id:
        return JSONResponse({"error": "X-User-Id header required"}, status_code=400)

    terminal = get_terminal(user_id)
    if terminal is None:
        return {"status": "none", "phase": None}

    status = terminal.get("status") or {}
    return {
        "status": "exists",
        "phase": status.get("phase"),
        "serviceUrl": status.get("serviceUrl"),
        "lastActivityAt": status.get("lastActivityAt"),
    }


@app.delete("/")
async def stop_terminal(request: Request):
    """Explicitly stop and delete a user's terminal."""
    if not _authenticate(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    user_id = _get_user_id(request)
    if not user_id:
        return JSONResponse({"error": "X-User-Id header required"}, status_code=400)

    deleted = delete_terminal(user_id)
    return {"deleted": deleted}


PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


@app.api_route("/{path:path}", methods=PROXY_METHODS, include_in_schema=False)
async def proxy_to_terminal(path: str, request: Request):
    """Proxy any request to the user's Open Terminal pod."""
    if not _authenticate(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    user_id = _get_user_id(request)
    if not user_id:
        return JSONResponse({"error": "X-User-Id header required"}, status_code=400)

    resolved = await _resolve_terminal(user_id)
    if resolved is None:
        return JSONResponse(
            {"error": "Terminal not ready. Try again shortly."}, status_code=503
        )

    service_url, api_key = resolved
    target_url = f"{service_url.rstrip('/')}/{path}"
    if request.query_params:
        target_url += f"?{request.query_params}"

    # Touch activity asynchronously
    asyncio.create_task(asyncio.to_thread(touch_activity, user_id))

    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-User-Id": user_id,
    }
    content_type = request.headers.get("content-type")
    if content_type:
        headers["Content-Type"] = content_type

    body = await request.body()
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=300, connect=10),
    )

    try:
        upstream_response = await session.request(
            method=request.method,
            url=target_url,
            headers=headers,
            data=body or None,
        )

        upstream_content_type = upstream_response.headers.get("content-type", "")
        filtered_headers = {
            k: v
            for k, v in upstream_response.headers.items()
            if k.lower() not in STRIPPED_RESPONSE_HEADERS
        }

        if any(t in upstream_content_type for t in STREAMING_CONTENT_TYPES):

            async def cleanup():
                await upstream_response.release()
                await session.close()

            return StreamingResponse(
                content=upstream_response.content.iter_any(),
                status_code=upstream_response.status,
                headers=filtered_headers,
                background=BackgroundTask(cleanup),
            )

        response_body = await upstream_response.read()
        status_code = upstream_response.status
        await upstream_response.release()
        await session.close()

        return Response(
            content=response_body, status_code=status_code, headers=filtered_headers
        )

    except Exception as error:
        await session.close()
        log.exception("Proxy error for user %s: %s", user_id, error)
        return JSONResponse(
            {"error": f"Terminal proxy error: {error}"}, status_code=502
        )


# ---------------------------------------------------------------------------
# WebSocket proxy
# ---------------------------------------------------------------------------


@app.websocket("/api/terminals/{session_id}")
async def ws_terminal(ws: WebSocket, session_id: str):
    """Proxy a WebSocket terminal session to the user's Open Terminal pod.

    Uses first-message auth: ``{"type": "auth", "token": "<orchestrator_api_key>"}``.
    The ``user_id`` is passed as a query parameter by Open WebUI's WS proxy.
    """
    await ws.accept()

    import json

    # First message: auth from Open WebUI's WS proxy
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=10.0)
        payload = json.loads(raw)
        if payload.get("type") != "auth":
            await ws.close(code=4001, reason="Expected auth message")
            return
        token = payload.get("token", "")
        if API_KEY and token != API_KEY:
            await ws.close(code=4001, reason="Invalid token")
            return
    except (asyncio.TimeoutError, json.JSONDecodeError):
        await ws.close(code=4001, reason="Auth timeout or invalid payload")
        return

    # Get user_id from query params (set by Open WebUI's WS proxy)
    user_id = ws.query_params.get("user_id")
    if not user_id:
        await ws.close(code=4001, reason="user_id query parameter required")
        return

    resolved = await _resolve_terminal(user_id)
    if resolved is None:
        await ws.close(code=4003, reason="Terminal not ready")
        return

    service_url, api_key = resolved

    # Touch activity
    asyncio.create_task(asyncio.to_thread(touch_activity, user_id))

    # Connect to the upstream Open Terminal WebSocket
    ws_url = service_url.replace("http://", "ws://").replace("https://", "wss://")
    upstream_url = f"{ws_url.rstrip('/')}/api/terminals/{session_id}"

    session = aiohttp.ClientSession()
    try:
        async with session.ws_connect(upstream_url) as upstream:
            # Auth to upstream Open Terminal
            await upstream.send_str(json.dumps({"type": "auth", "token": api_key}))

            async def client_to_upstream():
                try:
                    while True:
                        msg = await ws.receive()
                        if msg["type"] == "websocket.disconnect":
                            break
                        elif "bytes" in msg and msg["bytes"]:
                            await upstream.send_bytes(msg["bytes"])
                        elif "text" in msg and msg["text"]:
                            await upstream.send_str(msg["text"])
                except Exception:
                    pass

            async def upstream_to_client():
                try:
                    async for msg in upstream:
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            await ws.send_bytes(msg.data)
                        elif msg.type == aiohttp.WSMsgType.TEXT:
                            await ws.send_text(msg.data)
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
                except Exception:
                    pass

            await asyncio.gather(
                client_to_upstream(), upstream_to_client(), return_exceptions=True
            )
    except Exception as e:
        log.exception("WebSocket proxy error for user %s: %s", user_id, e)
    finally:
        await session.close()
        try:
            await ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
