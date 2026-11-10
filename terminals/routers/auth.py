"""Authentication dependencies for the proxy and API routes."""

import hmac
from typing import Optional

from fastapi import Header, HTTPException, Request

from terminals.config import settings


async def verify_admin_api_key(
    authorization: Optional[str] = Header(None),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
) -> None:
    """Validate server-key access to template and lifecycle administration."""
    # Trusted proxies set X-User-Id themselves when delegating user requests
    # with the shared key. Those requests must not inherit admin privileges.
    if x_user_id is not None:
        raise HTTPException(status_code=403, detail="User-scoped requests cannot access admin APIs")

    if settings.api_key:
        if not authorization:
            raise HTTPException(status_code=401, detail="Missing Authorization header")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or token != settings.api_key:
            raise HTTPException(status_code=401, detail="Invalid admin API key")
        return None

    # Open access remains available for local administration without a key.
    return None


def require_server_key(token: str) -> None:
    if not settings.api_key:
        raise HTTPException(503, "TERMINALS_API_KEY is required for managed terminals")
    if not isinstance(token, str) or not hmac.compare_digest(token, settings.api_key):
        raise HTTPException(401, "Invalid server key")


async def verify_proxy_credentials(authorization, user_id):
    scheme, _, token = (authorization or "").partition(" ")
    require_server_key(token if scheme.lower() == "bearer" else "")
    if not user_id:
        raise HTTPException(400, "Missing X-User-Id")
    from terminals.utils.context import validate_identifier
    try:
        return validate_identifier(user_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


async def verify_proxy_user(request: Request, authorization: Optional[str] = Header(None), x_user_id: str = Header(..., alias="X-User-Id")):
    return await verify_proxy_credentials(authorization, x_user_id)
