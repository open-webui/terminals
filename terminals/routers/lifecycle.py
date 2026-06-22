"""Administrative terminal lifecycle endpoints."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from terminals.config import settings
from terminals.db.session import async_session
from terminals.routers.auth import verify_admin_api_key
from terminals.routers.proxy import active_ws_connections

router = APIRouter(prefix="/api/v1", tags=["terminals"])


class RefreshRequest(BaseModel):
    user_id: Optional[str] = None
    policy_id: Optional[str] = None
    only_idle: bool = True
    reset: bool = False


class StopRequest(BaseModel):
    user_id: str
    policy_id: str = "default"


async def _policy_count() -> int:
    if async_session is None:
        return 0
    from sqlalchemy import func, select

    from terminals.models.policy import Policy

    async with async_session() as session:
        result = await session.execute(select(func.count()).select_from(Policy))
        return int(result.scalar_one() or 0)


@router.get("/status", dependencies=[Depends(verify_admin_api_key)])
async def admin_status(request: Request):
    """Return control-plane status for the built-in admin UI."""
    terminals = await request.app.state.backend.list_terminals()
    return {
        "status": True,
        "backend": settings.backend,
        "image": settings.image,
        "idle_timeout_minutes": settings.idle_timeout_minutes,
        "max_cpu": settings.max_cpu,
        "max_memory": settings.max_memory,
        "max_storage": settings.max_storage,
        "allowed_images": settings.allowed_images,
        "active_terminals": len(terminals),
        "policy_count": await _policy_count(),
        "active_websocket_connections": active_ws_connections,
    }


@router.get("/terminals", dependencies=[Depends(verify_admin_api_key)])
async def list_terminals(request: Request):
    """List sanitized active terminal instances."""
    return await request.app.state.backend.list_terminals()


@router.post("/terminals/stop", dependencies=[Depends(verify_admin_api_key)])
async def stop_terminal(request: Request, body: StopRequest):
    """Stop one terminal so the next access starts a fresh instance."""
    result = await request.app.state.backend.refresh(
        user_id=body.user_id,
        policy_id=body.policy_id,
        only_idle=False,
        reset=False,
    )
    if result.matched == 0:
        raise HTTPException(status_code=404, detail="Terminal not found")
    return {
        "matched": result.matched,
        "stopped": result.refreshed,
        "skipped_active": result.skipped_active,
    }


@router.post("/terminals/refresh", dependencies=[Depends(verify_admin_api_key)])
async def refresh_terminals(request: Request, body: RefreshRequest):
    """Tear down matching terminals so their next access provisions fresh."""
    result = await request.app.state.backend.refresh(
        user_id=body.user_id,
        policy_id=body.policy_id,
        only_idle=body.only_idle,
        reset=body.reset,
    )
    return {
        "matched": result.matched,
        "refreshed": result.refreshed,
        "reset": result.reset,
        "skipped_active": result.skipped_active,
    }
