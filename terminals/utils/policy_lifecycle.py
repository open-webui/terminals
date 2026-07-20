"""Helpers for recurring policy lifecycle work."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from sqlalchemy import select

from terminals.db.session import async_session

RESET_KEY = "reset"
ACTIVITY_KEY = "activity"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _as_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def validate_schedule(schedule: str, tz_name: str | None = None) -> bool:
    try:
        return next_reset_after(schedule, tz_name, _utc_now()) is not None
    except Exception:
        return False


def validate_lifecycle_data(data: dict | None) -> bool:
    """Validate supported lifecycle configuration."""
    if not data:
        return True

    reset = data.get(RESET_KEY)
    if reset is None:
        return True
    if not isinstance(reset, dict):
        return False

    schedule = reset.get("schedule")
    if not schedule:
        return True
    return validate_schedule(str(schedule), reset.get("timezone") or "UTC")


def is_one_shot_schedule(schedule: str) -> bool:
    schedule = (schedule or "").strip()
    if not schedule or schedule.startswith("@") or " " in schedule:
        return False
    try:
        datetime.fromisoformat(schedule.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def next_reset_after(
    schedule: str,
    tz_name: str | None,
    after: datetime,
) -> datetime | None:
    """Return the next reset time as naive UTC."""
    schedule = (schedule or "").strip()
    if not schedule:
        return None

    zone = _zone(tz_name)
    local_after = after.replace(tzinfo=timezone.utc).astimezone(zone)

    if schedule == "@weekly":
        iterator = croniter("0 0 * * 0", local_after)
        return _as_utc_naive(iterator.get_next(datetime))

    if schedule == "@monthly":
        iterator = croniter("0 0 1 * *", local_after)
        return _as_utc_naive(iterator.get_next(datetime))

    if " " in schedule:
        iterator = croniter(schedule, local_after)
        return _as_utc_naive(iterator.get_next(datetime))

    try:
        parsed = datetime.fromisoformat(schedule.replace("Z", "+00:00"))
    except ValueError:
        iterator = croniter(schedule, local_after)
        return _as_utc_naive(iterator.get_next(datetime))

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return _as_utc_naive(parsed)


async def reset_due_for(user_id: str, policy_id: str, spec: dict | None) -> bool:
    """Return True if this user/policy has a reset due."""
    if async_session is None:
        return False

    now = _utc_now()

    from terminals.models.policy import PolicyLifecycle, PolicyLifecycleState

    async with async_session() as session:
        lifecycle_row = await session.execute(
            select(PolicyLifecycle).where(PolicyLifecycle.policy_id == policy_id)
        )
        lifecycle = lifecycle_row.scalar_one_or_none()
        lifecycle_data = dict(lifecycle.data or {}) if lifecycle else {}
        reset_config = dict(lifecycle_data.get(RESET_KEY) or {})
        schedule = reset_config.get("schedule")
        if not schedule:
            return False

        timezone_name = reset_config.get("timezone") or "UTC"

        state_row = await session.execute(
            select(PolicyLifecycleState).where(
                PolicyLifecycleState.user_id == user_id,
                PolicyLifecycleState.policy_id == policy_id,
            )
        )
        state = state_row.scalar_one_or_none()
        if state is None:
            state = PolicyLifecycleState(
                id=f"{user_id}:{policy_id}",
                user_id=user_id,
                policy_id=policy_id,
                data={},
            )
            session.add(state)

        data = dict(state.data or {})
        reset = dict(data.get(RESET_KEY) or {})
        if reset.get("schedule") != schedule or reset.get("timezone") != timezone_name:
            reset.update(
                {
                    "schedule": schedule,
                    "timezone": timezone_name,
                    "last_applied_at": reset.get("last_applied_at"),
                    "next_due_at": _iso(next_reset_after(schedule, timezone_name, now)),
                }
            )
            data[RESET_KEY] = reset
            state.data = data
            await session.commit()

        next_due_at = _parse_iso(reset.get("next_due_at"))
        return bool(next_due_at and next_due_at <= now)


async def mark_reset_applied(user_id: str, policy_id: str, spec: dict | None) -> None:
    if async_session is None:
        return

    now = _utc_now()

    from terminals.models.policy import PolicyLifecycle, PolicyLifecycleState

    async with async_session() as session:
        lifecycle_row = await session.execute(
            select(PolicyLifecycle).where(PolicyLifecycle.policy_id == policy_id)
        )
        lifecycle = lifecycle_row.scalar_one_or_none()
        lifecycle_data = dict(lifecycle.data or {}) if lifecycle else {}
        reset_config = dict(lifecycle_data.get(RESET_KEY) or {})
        schedule = reset_config.get("schedule")
        if not schedule:
            return

        timezone_name = reset_config.get("timezone") or "UTC"

        state_row = await session.execute(
            select(PolicyLifecycleState).where(
                PolicyLifecycleState.user_id == user_id,
                PolicyLifecycleState.policy_id == policy_id,
            )
        )
        state = state_row.scalar_one_or_none()
        if state is None:
            state = PolicyLifecycleState(
                id=f"{user_id}:{policy_id}",
                user_id=user_id,
                policy_id=policy_id,
                data={},
            )
            session.add(state)

        data = dict(state.data or {})
        reset = dict(data.get(RESET_KEY) or {})
        reset.update(
            {
                "schedule": schedule,
                "timezone": timezone_name,
                "last_applied_at": _iso(now),
                "next_due_at": _iso(
                    None
                    if is_one_shot_schedule(schedule)
                    else next_reset_after(schedule, timezone_name, now)
                ),
            }
        )
        data[RESET_KEY] = reset
        state.data = data
        await session.commit()


async def mark_terminal_active(user_id: str, policy_id: str) -> None:
    """Persist a cross-worker activity heartbeat for a terminal."""
    if async_session is None:
        return

    now = _utc_now()

    from terminals.models.policy import PolicyLifecycleState

    async with async_session() as session:
        state_row = await session.execute(
            select(PolicyLifecycleState).where(
                PolicyLifecycleState.user_id == user_id,
                PolicyLifecycleState.policy_id == policy_id,
            )
        )
        state = state_row.scalar_one_or_none()
        if state is None:
            state = PolicyLifecycleState(
                id=f"{user_id}:{policy_id}",
                user_id=user_id,
                policy_id=policy_id,
                data={},
            )
            session.add(state)

        data = dict(state.data or {})
        data[ACTIVITY_KEY] = {"last_seen_at": _iso(now)}
        state.data = data
        await session.commit()


async def terminal_last_active_at(user_id: str, policy_id: str) -> datetime | None:
    """Return the latest persisted activity heartbeat for a terminal."""
    if async_session is None:
        return None

    from terminals.models.policy import PolicyLifecycleState

    async with async_session() as session:
        state_row = await session.execute(
            select(PolicyLifecycleState).where(
                PolicyLifecycleState.user_id == user_id,
                PolicyLifecycleState.policy_id == policy_id,
            )
        )
        state = state_row.scalar_one_or_none()
        data = dict(state.data or {}) if state else {}
        activity = dict(data.get(ACTIVITY_KEY) or {})
        return _parse_iso(activity.get("last_seen_at"))


async def get_lifecycle_data(policy_id: str) -> dict:
    """Return policy-level lifecycle configuration."""
    if async_session is None:
        return {}

    from terminals.models.policy import PolicyLifecycle

    async with async_session() as session:
        row = await session.execute(
            select(PolicyLifecycle).where(PolicyLifecycle.policy_id == policy_id)
        )
        lifecycle = row.scalar_one_or_none()
        return deepcopy(lifecycle.data or {}) if lifecycle else {}


async def upsert_lifecycle_data(policy_id: str, data: dict) -> dict:
    """Create or update policy-level lifecycle configuration."""
    if async_session is None:
        return {}

    from terminals.models.policy import PolicyLifecycle

    async with async_session() as session:
        row = await session.execute(
            select(PolicyLifecycle).where(PolicyLifecycle.policy_id == policy_id)
        )
        lifecycle = row.scalar_one_or_none()
        now = _utc_now()

        if lifecycle:
            lifecycle.data = data
            lifecycle.updated_at = now
        else:
            lifecycle = PolicyLifecycle(
                id=policy_id,
                policy_id=policy_id,
                data=data,
                created_at=now,
                updated_at=now,
            )
            session.add(lifecycle)

        await session.commit()
        return deepcopy(lifecycle.data or {})
