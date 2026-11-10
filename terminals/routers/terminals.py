"""Terminal administration; all template routes are mounted here."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from terminals.config import settings
from terminals.db.session import async_session
from terminals.models.template import Template
from terminals.routers.auth import verify_admin_api_key
from terminals.routers.templates import router as templates_router

router = APIRouter(prefix="/api/v1/terminals", tags=["terminals"], dependencies=[Depends(verify_admin_api_key)])
router.include_router(templates_router, prefix="/templates")


@router.get("")
async def list_terminals(request: Request, template_id: str | None = None):
    return await request.app.state.backend.list_terminals(template_id)


@router.get("/status")
async def status(request: Request):
    async with async_session() as session:
        count = await session.scalar(select(func.count()).select_from(Template))
    return {"status": True, "version": "0.4.0", "backend": settings.backend,
            "template_count": count, "image": settings.image,
            "max_cpu": settings.max_cpu, "max_memory": settings.max_memory,
            "max_storage": settings.max_storage, "allowed_images": settings.allowed_images,
            "network_profiles": sorted(settings.network_profiles)}
