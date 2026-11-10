"""Template CRUD API."""

from datetime import datetime, timezone
from typing import Any, Optional, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, field_validator

from terminals.config import settings
from terminals.backends.base import ProvisioningError
from terminals.db.session import async_session
from terminals.routers.auth import verify_admin_api_key
from terminals.utils.template_lifecycle import (
    get_lifecycle_data,
    upsert_lifecycle_data,
    validate_lifecycle_data,
)
from terminals.utils.kubernetes_security import (
    restricted_enabled,
    restricted_env_errors,
)
from terminals.utils.parsing import parse_size
from terminals.utils.network_profiles import resolve_network_profile

router = APIRouter(tags=["templates"], dependencies=[Depends(verify_admin_api_key)])


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class TemplateData(BaseModel):
    """Template data. All fields are optional and merged with defaults."""

    model_config = ConfigDict(extra="allow")

    name: str | None = None
    scope: Literal["user", "chat", "model"] | None = None
    image: Optional[str] = None
    network_profile: Optional[str] = None
    env: Optional[dict] = None
    cpu_limit: Optional[str] = None
    memory_limit: Optional[str] = None
    storage: Optional[str] = None
    storage_mode: Optional[str] = None
    idle_timeout_minutes: Optional[int] = None
    max_lifetime_minutes: Optional[int] = None
    restricted: Optional[bool] = None
    pod_security_context: Optional[dict[str, Any]] = None
    container_security_context: Optional[dict[str, Any]] = None


class TemplateResponse(BaseModel):
    id: str
    data: dict
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class TemplateCreate(BaseModel):
    id: str
    data: TemplateData = TemplateData()

    @field_validator("id")
    @classmethod
    def valid_id(cls, value):
        from terminals.utils.context import validate_identifier
        validate_identifier(value)
        if "/" in value:
            raise ValueError("Template IDs cannot contain slashes")
        return value


class TemplateLifecycleData(BaseModel):
    """Template lifecycle config, stored separately from instance configuration."""
    model_config = ConfigDict(extra="allow")

    reset: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clamp_template(data: dict) -> dict:
    """Clamp template values against env var hard caps."""
    removed = {"per_chat", "per_automation", "max_instances"}.intersection(data)
    if removed:
        raise HTTPException(422, "Use scope instead of legacy allocation fields")
    result = {k: v for k, v in data.items() if v is not None}
    try:
        resolve_network_profile(result, settings.backend)
    except ProvisioningError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    lifecycle_keys = {"reset", "reset_schedule", "reset_timezone", "lifecycle"}
    unexpected = sorted(lifecycle_keys.intersection(result))
    if unexpected:
        raise HTTPException(
            status_code=400,
            detail=f"Lifecycle keys are not valid template fields: {', '.join(unexpected)}",
        )
    if restricted_enabled(result):
        errors = restricted_env_errors(result.get("env") or {})
        if errors:
            raise HTTPException(status_code=400, detail="; ".join(errors))

    # Clamp CPU
    if settings.max_cpu and "cpu_limit" in result:
        try:
            if float(result["cpu_limit"]) > float(settings.max_cpu):
                result["cpu_limit"] = settings.max_cpu
        except (ValueError, TypeError):
            pass

    # Clamp memory
    if settings.max_memory and "memory_limit" in result:
        try:
            if parse_size(result["memory_limit"]) > parse_size(settings.max_memory):
                result["memory_limit"] = settings.max_memory
        except Exception:
            pass

    # Clamp storage
    if settings.max_storage and "storage" in result:
        try:
            if parse_size(result["storage"]) > parse_size(settings.max_storage):
                result["storage"] = settings.max_storage
        except Exception:
            pass

    # Validate image against allowlist
    if settings.allowed_images and "image" in result:
        import fnmatch
        patterns = [p.strip() for p in settings.allowed_images.split(",")]
        if not any(fnmatch.fnmatch(result["image"], p) for p in patterns):
            raise HTTPException(
                status_code=400,
                detail=f"Image '{result['image']}' not in allowed list",
            )

    return result


def _validate_lifecycle(data: dict) -> dict:
    result = {k: v for k, v in data.items() if v is not None}
    if not validate_lifecycle_data(result):
        raise HTTPException(status_code=400, detail="Invalid lifecycle configuration")
    return result


# ---------------------------------------------------------------------------
# Endpoints

from sqlalchemy import select, delete
from sqlalchemy.exc import IntegrityError
from terminals.models.template import Template, TemplateLifecycle, TemplateLifecycleState
from terminals.utils.instances import instance_lock, change_instance, stop_instance, remove_home, describe_instance
from terminals.utils.context import terminal_identity
from terminals.utils.template_specs import resolve_template_spec


def response(row):
    return TemplateResponse(id=row.id, data=row.data or {},
                            created_at=str(row.created_at), updated_at=str(row.updated_at))


async def require_template(template_id):
    async with async_session() as session:
        row = await session.get(Template, template_id)
        if row is None:
            raise HTTPException(404, "Template not found")
        return row


@router.get("")
async def list_templates():
    async with async_session() as session:
        rows = await session.scalars(select(Template).order_by(Template.created_at))
        return [response(row) for row in rows]


@router.post("", status_code=201)
async def create_template(body: TemplateCreate):
    data = _clamp_template(body.data.model_dump(exclude_none=True))
    async with instance_lock("template:" + body.id), async_session() as session:
        row = Template(id=body.id, data=data)
        session.add(row)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(409, "Template already exists") from exc
        await session.refresh(row)
        return response(row)


@router.get("/{template_id}")
async def get_template(template_id: str):
    return response(await require_template(template_id))


@router.get("/{template_id}/instances")
async def list_instances(template_id: str, request: Request):
    await require_template(template_id)
    return await request.app.state.backend.list_terminals(template_id)


@router.patch("/{template_id}")
async def update_template(template_id: str, body: TemplateData):
    async with instance_lock("template:" + template_id), async_session() as session:
        row = await session.get(Template, template_id)
        if row is None:
            raise HTTPException(404, "Template not found")
        old = row.data or {}
        data = _clamp_template({**old, **body.model_dump(exclude_unset=True)})
        protected = ("scope", "storage_mode")
        if any(old.get(k) != data.get(k) for k in protected):
            exists = await session.scalar(select(TemplateLifecycleState.id).where(
                TemplateLifecycleState.template_id == template_id,
            ).limit(1))
            if exists:
                raise HTTPException(409, "Delete existing environments before changing scope or storage mode")
        row.data = data
        row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await session.commit()
        return response(row)


@router.delete("/{template_id}")
async def delete_template(template_id: str, request: Request):
    async with instance_lock("template:" + template_id):
        await require_template(template_id)
        spec = await resolve_template_spec(template_id)
        async with async_session() as session:
            rows = list(await session.scalars(select(TemplateLifecycleState).where(
                TemplateLifecycleState.template_id == template_id).order_by(TemplateLifecycleState.id)))
        for row in rows:
            async with instance_lock(row.id):
                await stop_instance(request.app.state.backend, row.template_id, row.context_id)
                await remove_home(request.app.state.backend, row, spec)
                async with async_session() as session:
                    await session.execute(delete(TemplateLifecycleState).where(TemplateLifecycleState.id == row.id))
                    await session.commit()
        async with async_session() as session:
            await session.execute(delete(TemplateLifecycle).where(TemplateLifecycle.template_id == template_id))
            await session.execute(delete(Template).where(Template.id == template_id))
            await session.commit()
    return {"ok": True}


class InstanceContext(BaseModel):
    user_id: str
    chat_id: str | None = None
    model_id: str | None = None


async def context_for(template_id, form):
    template = await require_template(template_id)
    scope = (template.data or {}).get("scope")
    context, key = terminal_identity(template_id, scope, user_id=form.user_id,
                                     chat_id=form.chat_id, model_id=form.model_id)
    return context, key


@router.get("/{template_id}/instance")
async def get_instance(template_id: str, user_id: str, request: Request,
                      chat_id: str | None = None, model_id: str | None = None):
    context, key = await context_for(template_id, InstanceContext(user_id=user_id, chat_id=chat_id, model_id=model_id))
    async with async_session() as session:
        row = await session.get(TemplateLifecycleState, key)
    if row is None:
        return {"id": key, "template_id": template_id, "context_id": context, "status": "not_created"}
    return await describe_instance(request.app.state.backend, row)


@router.post("/{template_id}/restart")
async def restart_instance(template_id: str, body: InstanceContext, request: Request):
    context, _ = await context_for(template_id, body)
    await change_instance(request.app.state.backend, template_id, context, "restart")
    return {"ok": True}


@router.post("/{template_id}/reset")
async def reset_instance(template_id: str, body: InstanceContext, request: Request):
    context, _ = await context_for(template_id, body)
    await change_instance(request.app.state.backend, template_id, context, "reset")
    return {"ok": True}


@router.delete("/{template_id}/instance")
async def delete_instance(template_id: str, body: InstanceContext, request: Request):
    context, _ = await context_for(template_id, body)
    await change_instance(request.app.state.backend, template_id, context, "delete")
    return {"ok": True}


@router.get("/{template_id}/lifecycle")
async def get_template_lifecycle(template_id: str):
    await require_template(template_id)
    return {"template_id": template_id, "data": await get_lifecycle_data(template_id)}


@router.put("/{template_id}/lifecycle")
async def put_template_lifecycle(template_id: str, body: TemplateLifecycleData):
    async with instance_lock("template:" + template_id):
        await require_template(template_id)
        data = _validate_lifecycle(body.model_dump(exclude_none=True))
        return {"template_id": template_id, "data": await upsert_lifecycle_data(template_id, data)}
