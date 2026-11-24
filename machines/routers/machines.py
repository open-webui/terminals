"""Machine administration; all template routes are mounted here."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from machines.config import settings
from machines.db.session import async_session
from machines.models.template import Template
from machines.routers.auth import verify_admin_api_key
from machines.routers.templates import router as templates_router

router = APIRouter(tags=["machines"], dependencies=[Depends(verify_admin_api_key)])
router.include_router(templates_router, prefix="/templates")


@router.get("")
async def list_terminals(request: Request, template_id: str | None = None):
    return await request.app.state.backend.list_terminals(template_id)


@router.get("/status")
async def status(request: Request):
    async with async_session() as session:
        count = await session.scalar(select(func.count()).select_from(Template))
    return {"status": True, "version": "0.5.1", "backend": settings.backend,
            "template_count": count, "image": settings.image,
            "max_cpu": settings.max_cpu, "max_memory": settings.max_memory,
            "max_storage": settings.max_storage, "allowed_images": settings.allowed_images,
            "network_profiles": sorted(settings.network_profiles)}
