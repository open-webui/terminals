"""Abstract base class for terminal backends."""

import asyncio
import logging
import time
from abc import ABC
from datetime import datetime, timezone

from terminals.config import settings
from terminals.utils.template_lifecycle import (
    ensure_terminal_state,
    mark_reset_applied,
    mark_terminal_active,
    reset_due_for,
    terminal_last_active_at,
)
from terminals.utils.template_specs import resolve_template_spec
from terminals.utils.context import DEFAULT_CONTEXT_ID, normalize_context_id, get_terminal_id

log = logging.getLogger(__name__)


def parse_started_at(value) -> float:
    """Epoch seconds for a instance's ISO start time, or now if unknown."""
    if isinstance(value, datetime):
        return value.timestamp()
    try:
        return datetime.fromisoformat(str(value)[:19]).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return time.time()


class ProvisioningError(RuntimeError):
    """Terminal provisioning failed with a reason suitable for the client."""


class Backend(ABC):
    """Backend caches use the same deterministic key as persistent lifecycle state."""

    def __init__(self):
        self._instances = {}
        self._specs = {}
        self._activity_synced_at = {}
        self._running_checked_at = {}
        self._reaper_task = None

    @staticmethod
    def _key(template_id, context_id=DEFAULT_CONTEXT_ID):
        return get_terminal_id(template_id, context_id)


    def invalidate_status(self, template_id, context_id=DEFAULT_CONTEXT_ID):
        self._running_checked_at.pop(self._key(template_id, context_id), None)

    def forget(self, key):
        for cache in (self._instances, self._specs,
                      self._activity_synced_at, self._running_checked_at):
            cache.pop(key, None)

    async def ensure_terminal(self, template_id, context_id=DEFAULT_CONTEXT_ID):
        from terminals.utils.instances import instance_lock
        context_id = normalize_context_id(context_id)
        key = self._key(template_id, context_id)
        async with instance_lock("template:" + template_id, shared=True), instance_lock(key):
            # Read under the template lock: edits/deletion cannot race allocation.
            spec = await resolve_template_spec(template_id)
            scope = (spec or {}).get("scope")
            valid = context_id == "default" if scope is None else context_id.startswith(scope + ":")
            if not valid:
                from fastapi import HTTPException
                raise HTTPException(409, "Template scope changed; reopen the terminal")
            await ensure_terminal_state(template_id, context_id)
            info = await self._ensure_terminal(template_id, context_id, spec)
            if info:
                info.update(template_id=template_id, context_id=context_id)
            return info

    async def _ensure_terminal(self, template_id, context_id=DEFAULT_CONTEXT_ID, spec=None):
        key = self._key(template_id, context_id)
        info = self._instances.get(key)
        if info and await self.status(info["workload_name"]) == "running":
            return info
        self.forget(key)
        await self._apply_due_reset(template_id, context_id, spec)
        info = await self.provision(template_id=template_id, context_id=context_id, spec=spec)
        if info:
            info.setdefault("started_at", time.time())
            self._instances[key] = info
            self._specs[key] = spec or {}
        return info

    async def get_terminal_info(self, template_id, context_id=DEFAULT_CONTEXT_ID):
        info = self._instances.get(self._key(template_id, context_id))
        if info and await self.status(info["workload_name"]) == "running":
            return info
        return None

    async def touch_activity(self, template_id, context_id=DEFAULT_CONTEXT_ID):
        key = self._key(template_id, context_id)
        checked = self._activity_synced_at.get(key)
        interval = max(1, min(settings.status_cache_ttl or 30, 60))
        if checked is None or time.monotonic() - checked >= interval:
            await mark_terminal_active(template_id, context_id)
            self._activity_synced_at[key] = time.monotonic()

    async def _apply_due_reset(self, template_id, context_id, spec):
        if not await reset_due_for(template_id, context_id):
            return False
        from terminals.utils.instances import stop_instance
        await stop_instance(self, template_id, context_id)
        await self.reset(template_id, context_id, spec)
        await mark_reset_applied(template_id, context_id)
        return True

    async def list_terminals(self, template_id=None):
        from sqlalchemy import select
        from terminals.db.session import async_session
        from terminals.models.template import TemplateLifecycleState
        from terminals.utils.instances import describe_instance
        async with async_session() as session:
            query = select(TemplateLifecycleState)
            if template_id is not None:
                query = query.where(TemplateLifecycleState.template_id == template_id)
            rows = list(await session.scalars(query.order_by(TemplateLifecycleState.created_at)))
        return [await describe_instance(self, row) for row in rows]

    def start_reaper(self):
        if self._reaper_task is None:
            self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def stop_reaper(self):
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except asyncio.CancelledError:
                pass
            self._reaper_task = None

    async def _reaper_loop(self):
        while True:
            try:
                await asyncio.sleep(60)
                await self._reap_idle()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Idle reaper error")

    async def _reap_idle(self):
        from sqlalchemy import select
        from terminals.db.session import async_session
        from terminals.models.template import TemplateLifecycleState
        from terminals.utils.instances import instance_lock, stop_instance
        async with async_session() as session:
            rows = list(await session.scalars(select(TemplateLifecycleState)))
        for row in rows:
            try:
                async with instance_lock("template:" + row.template_id, shared=True), instance_lock(row.id):
                    async with async_session() as session:
                        if await session.get(TemplateLifecycleState, row.id) is None:
                            continue
                    current_spec = await resolve_template_spec(row.template_id)
                    spec = self._specs.get(row.id, current_spec) or {}
                    async with asyncio.timeout(settings.idle_cleanup_timeout_seconds):
                        if await self._apply_due_reset(row.template_id, row.context_id, spec):
                            continue
                        info = self._instances.get(row.id)
                        if not info:
                            continue
                        last_active = await terminal_last_active_at(row.template_id, row.context_id)
                        active_at = last_active.replace(tzinfo=timezone.utc).timestamp() if last_active else time.time()
                        idle = spec.get("idle_timeout_minutes", settings.idle_timeout_minutes)
                        lifetime = spec.get("max_lifetime_minutes", settings.max_lifetime_minutes)
                        expired = lifetime and lifetime > 0 and time.time() - info.get("started_at", time.time()) >= lifetime * 60
                        if expired or (idle and idle > 0 and time.time() - active_at >= idle * 60):
                            await stop_instance(self, row.template_id, row.context_id)
            except Exception:
                log.exception("Failed lifecycle sweep for %s", row.id)
