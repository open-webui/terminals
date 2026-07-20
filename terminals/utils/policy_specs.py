"""Shared helpers for resolved terminal policy specs."""

from terminals.config import settings
from terminals.db.session import async_session


class PolicyNotFoundError(Exception):
    """Raised when a named policy does not exist."""


def merge_policy_defaults(policy_data: dict) -> dict:
    """Merge env var defaults with policy overrides."""
    defaults = {}
    if settings.image:
        defaults["image"] = settings.image
    return {**defaults, **{k: v for k, v in policy_data.items() if v is not None}}


async def resolve_policy_spec(policy_id: str) -> tuple[str, dict | None]:
    """Look up a policy by ID. Returns (policy_id, merged spec)."""
    if async_session is None:
        return policy_id, None

    from sqlalchemy import select

    from terminals.models.policy import Policy

    async with async_session() as session:
        row = await session.execute(select(Policy).where(Policy.id == policy_id))
        policy = row.scalar_one_or_none()
        if policy is None:
            raise PolicyNotFoundError(policy_id)

        return policy_id, merge_policy_defaults(policy.data or {})
