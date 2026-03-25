"""Terminal instance listing and management API."""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from terminals.config import settings
from terminals.routers.auth import verify_api_key

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["instances"])


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class InstanceResponse(BaseModel):
    user_id: str
    policy_id: str
    instance_id: str
    instance_name: str
    status: str
    host: str = ""
    port: int = 0
    image: str = ""
    cpu_limit: str = ""
    memory_limit: str = ""
    storage: str = ""
    idle_timeout_minutes: int = 0
    last_activity: str = ""
    created_at: str = ""
    message: str = ""


class InfoResponse(BaseModel):
    backend: str
    version: str = "0.1.0"
    max_cpu: str = ""
    max_memory: str = ""
    max_storage: str = ""
    allowed_images: str = ""
    idle_timeout_minutes: int = 0


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/instances", dependencies=[Depends(verify_api_key)])
async def list_instances(request: Request) -> list[InstanceResponse]:
    """List all active terminal instances."""
    backend = request.app.state.backend
    instances = await backend.list_instances()
    return [InstanceResponse(**inst) for inst in instances]


@router.delete(
    "/instances/{instance_id}",
    dependencies=[Depends(verify_api_key)],
)
async def teardown_instance(instance_id: str, request: Request):
    """Force-teardown a specific terminal instance."""
    backend = request.app.state.backend
    try:
        await backend.teardown(instance_id)
    except Exception:
        log.exception("Failed to teardown instance %s", instance_id)
        raise HTTPException(status_code=500, detail="Failed to teardown instance")
    return {"ok": True}


@router.get("/info", dependencies=[Depends(verify_api_key)])
async def get_info() -> InfoResponse:
    """Return server info including backend type and resource caps."""
    return InfoResponse(
        backend=settings.backend,
        max_cpu=settings.max_cpu,
        max_memory=settings.max_memory,
        max_storage=settings.max_storage,
        allowed_images=settings.allowed_images,
        idle_timeout_minutes=settings.idle_timeout_minutes,
    )
