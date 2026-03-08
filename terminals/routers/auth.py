"""Authentication dependencies for the proxy and API routes."""

from typing import Optional

import httpx
from fastapi import Depends, Header, HTTPException

from terminals.config import settings

_owui_client: Optional[httpx.AsyncClient] = None


async def _get_owui_client() -> httpx.AsyncClient:
    global _owui_client
    if _owui_client is None:
        _owui_client = httpx.AsyncClient(timeout=10.0)
    return _owui_client


async def validate_token(token: str) -> Optional[str]:
    """Validate a bearer token against the Open WebUI instance.

    Returns the verified user ID on success.
    Raises ``HTTPException`` on failure.

    Only call when ``settings.open_webui_url`` is set.
    """
    client = await _get_owui_client()
    url = settings.open_webui_url.rstrip("/")
    try:
        resp = await client.get(
            f"{url}/api/v1/auths/",
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=401, detail="Invalid token")
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Failed to reach Open WebUI")

    data = resp.json()
    verified_user_id = data.get("id")
    if not verified_user_id:
        raise HTTPException(status_code=401, detail="Token response missing user ID")
    return verified_user_id


async def verify_api_key(
    authorization: Optional[str] = Header(None),
) -> Optional[str]:
    """Validate the caller's token.

    Supports three modes:
      1. Open WebUI - validates the JWT against the configured Open WebUI instance
      2. API Key    - checks against TERMINALS_API_KEY
      3. Open       - no auth when neither is configured

    Returns the verified user ID when using Open WebUI JWT validation,
    or ``None`` for API-key / open modes (where ``X-User-Id`` is trusted).
    """
    # Mode 1: Open WebUI JWT validation
    if settings.open_webui_url:
        if not authorization:
            raise HTTPException(status_code=401, detail="Missing Authorization header")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(status_code=401, detail="Invalid Authorization header")
        return await validate_token(token)

    # Mode 2: Static API key
    if settings.api_key:
        if not authorization:
            raise HTTPException(status_code=401, detail="Missing Authorization header")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or token != settings.api_key:
            raise HTTPException(status_code=401, detail="Invalid API key")
        return None

    # Mode 3: Open access (no key configured)
    return None


async def verify_user_id(
    verified_id: Optional[str] = Depends(verify_api_key),
    x_user_id: str = Header(..., alias="X-User-Id"),
) -> str:
    """Return the effective user ID after verifying against JWT identity.

    In JWT mode (``open_webui_url`` configured), the user ID extracted from
    the validated token **must** match ``X-User-Id``.  In API-key or open
    modes the header is trusted as-is.
    """
    if verified_id is not None and verified_id != x_user_id:
        raise HTTPException(
            status_code=403,
            detail="X-User-Id does not match authenticated identity",
        )
    return x_user_id
