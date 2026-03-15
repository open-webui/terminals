"""Policy CRUD API."""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from terminals.config import settings
from terminals.db.session import async_session
from terminals.routers.auth import verify_api_key
from terminals.utils.parsing import parse_size

router = APIRouter(prefix="/api/v1", tags=["policies"])


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class PolicyData(BaseModel):
    """Policy data — all fields optional, merged with defaults."""
    image: Optional[str] = None
    env: Optional[dict] = None
    cpu_limit: Optional[str] = None
    memory_limit: Optional[str] = None
    storage: Optional[str] = None        # e.g. "5Gi" — absent = ephemeral
    storage_mode: Optional[str] = None   # per-user, shared, shared-rwo
    idle_timeout_minutes: Optional[int] = None


class PolicyResponse(BaseModel):
    id: str
    data: dict
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class PolicyCreate(BaseModel):
    id: str
    data: PolicyData = PolicyData()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------



def _clamp_policy(data: dict) -> dict:
    """Clamp policy values against env var hard caps."""
    result = {k: v for k, v in data.items() if v is not None}

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


def _merge_defaults(policy_data: dict) -> dict:
    """Merge env var defaults with policy overrides."""
    defaults = {}
    if settings.image:
        defaults["image"] = settings.image
    return {**defaults, **{k: v for k, v in policy_data.items() if v is not None}}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/policies", dependencies=[Depends(verify_api_key)])
async def list_policies():
    """List all policies."""
    if async_session is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    from terminals.models.policy import Policy

    async with async_session() as session:
        from sqlalchemy import select
        result = await session.execute(select(Policy).order_by(Policy.created_at))
        policies = result.scalars().all()
        return [
            PolicyResponse(
                id=p.id,
                data=p.data or {},
                created_at=str(p.created_at) if p.created_at else None,
                updated_at=str(p.updated_at) if p.updated_at else None,
            )
            for p in policies
        ]


@router.post("/policies", dependencies=[Depends(verify_api_key)], status_code=201)
async def create_policy(body: PolicyCreate):
    """Create a new policy."""
    if async_session is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    from terminals.models.policy import Policy

    clamped = _clamp_policy(body.data.model_dump(exclude_none=True))

    async with async_session() as session:
        from sqlalchemy import select
        existing = await session.execute(select(Policy).where(Policy.id == body.id))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail=f"Policy '{body.id}' already exists")

        policy = Policy(
            id=body.id,
            data=clamped,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add(policy)
        await session.commit()

        return PolicyResponse(
            id=policy.id,
            data=policy.data or {},
            created_at=str(policy.created_at),
            updated_at=str(policy.updated_at),
        )


@router.get("/policies/{policy_id}", dependencies=[Depends(verify_api_key)])
async def get_policy(policy_id: str):
    """Get a single policy."""
    if async_session is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    from terminals.models.policy import Policy

    async with async_session() as session:
        from sqlalchemy import select
        result = await session.execute(select(Policy).where(Policy.id == policy_id))
        policy = result.scalar_one_or_none()
        if not policy:
            raise HTTPException(status_code=404, detail=f"Policy '{policy_id}' not found")

        return PolicyResponse(
            id=policy.id,
            data=policy.data or {},
            created_at=str(policy.created_at) if policy.created_at else None,
            updated_at=str(policy.updated_at) if policy.updated_at else None,
        )


@router.put("/policies/{policy_id}", dependencies=[Depends(verify_api_key)])
async def upsert_policy(policy_id: str, body: PolicyData):
    """Create or update a policy."""
    if async_session is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    from terminals.models.policy import Policy

    clamped = _clamp_policy(body.model_dump(exclude_none=True))

    async with async_session() as session:
        from sqlalchemy import select
        result = await session.execute(select(Policy).where(Policy.id == policy_id))
        policy = result.scalar_one_or_none()

        if policy:
            policy.data = clamped
            policy.updated_at = datetime.now(timezone.utc)
        else:
            policy = Policy(
                id=policy_id,
                data=clamped,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            session.add(policy)

        await session.commit()

        return PolicyResponse(
            id=policy.id,
            data=policy.data or {},
            created_at=str(policy.created_at),
            updated_at=str(policy.updated_at),
        )


@router.delete("/policies/{policy_id}", dependencies=[Depends(verify_api_key)])
async def delete_policy(policy_id: str):
    """Delete a policy."""
    if async_session is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    from terminals.models.policy import Policy

    async with async_session() as session:
        from sqlalchemy import select
        result = await session.execute(select(Policy).where(Policy.id == policy_id))
        policy = result.scalar_one_or_none()
        if not policy:
            raise HTTPException(status_code=404, detail=f"Policy '{policy_id}' not found")

        await session.delete(policy)
        await session.commit()
        return {"ok": True}
