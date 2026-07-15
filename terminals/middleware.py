"""Request middleware for correlation IDs."""

import uuid


class RequestIdMiddleware:
    """Attach a unique ``request_id`` to every request.

    The ID is stored in ``request.state.request_id`` for downstream use
    and returned as an ``X-Request-Id`` response header.

    Implemented as pure ASGI: Starlette's ``BaseHTTPMiddleware`` wraps every
    request in extra task machinery, which is measurable overhead on the
    proxy hot path.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = ""
        for key, value in scope.get("headers") or []:
            if key == b"x-request-id":
                request_id = value.decode("latin-1")
                break
        if not request_id:
            request_id = str(uuid.uuid4())

        scope.setdefault("state", {})["request_id"] = request_id
        request_id_bytes = request_id.encode("latin-1")

        async def send_with_header(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.append((b"x-request-id", request_id_bytes))
            await send(message)

        await self.app(scope, receive, send_with_header)
