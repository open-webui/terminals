"""Abstract base class for terminal backends."""

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from terminals.config import settings
from terminals.utils.policy_lifecycle import (
    mark_reset_applied,
    mark_terminal_active,
    reset_due_for,
    terminal_last_active_at,
)

log = logging.getLogger(__name__)


@dataclass
class RefreshResult:
    matched: int = 0
    refreshed: int = 0
    reset: int = 0
    skipped_active: int = 0


class Backend(ABC):
    """Lifecycle interface for provisioning and managing terminal instances.

    Includes an in-memory activity tracker and idle reaper that automatically
    tears down terminals that haven't been accessed within the configured
    timeout (``settings.idle_timeout_minutes`` or per-policy
    ``idle_timeout_minutes``).
    """

    def __init__(self) -> None:
        # key = "{user_id}:{policy_id}"
        self._activity: dict[str, float] = {}      # → last-active unix timestamp
        self._activity_wall: dict[str, float] = {} # → last-active wall-clock timestamp
        self._instances: dict[str, dict] = {}       # → provision result dict
        self._specs: dict[str, dict] = {}           # → resolved policy spec
        self._locks: dict[str, asyncio.Lock] = {}   # → per-key provisioning lock
        self._running_checked_at: dict[str, float] = {}
        self._activity_synced_at: dict[str, float] = {}
        self._reaper_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    async def provision(
        self,
        user_id: str,
        policy_id: str = "default",
        spec: Optional[dict] = None,
    ) -> dict:
        """Create a new terminal instance for *user_id*.

        *policy_id* scopes the container (one per user+policy pair).
        *spec* is the resolved policy spec dict; if ``None``, the backend
        uses ``settings.*`` defaults.

        Returns a dict with at least:
        ``instance_id``, ``instance_name``, ``api_key``, ``host``, ``port``.
        """

    @abstractmethod
    async def start(self, instance_id: str) -> bool:
        """Idempotent start — no-op if already running."""

    @abstractmethod
    async def teardown(self, instance_id: str) -> None:
        """Stop and remove the instance."""

    @abstractmethod
    async def status(self, instance_id: str) -> str:
        """Return ``'running'``, ``'stopped'``, or ``'missing'``."""

    @abstractmethod
    async def close(self) -> None:
        """Release resources on shutdown."""

    async def reset(
        self, user_id: str, policy_id: str, spec: Optional[dict] = None
    ) -> None:
        """Delete persisted files for a user terminal."""
        raise NotImplementedError("Reset is not supported by this backend")

    # ------------------------------------------------------------------
    # Instance tracking
    # ------------------------------------------------------------------

    @staticmethod
    def _key(user_id: str, policy_id: str = "default") -> str:
        return f"{user_id}:{policy_id}"

    def _record_activity(self, key: str) -> None:
        self._activity[key] = time.monotonic()
        self._activity_wall[key] = time.time()

    def invalidate_status(self, user_id: str, policy_id: str = "default") -> None:
        self._running_checked_at.pop(self._key(user_id, policy_id), None)

    async def ensure_terminal(
        self,
        user_id: str,
        policy_id: str = "default",
        spec: Optional[dict] = None,
    ) -> Optional[dict]:
        """Get-or-create a terminal for *user_id*.

        Returns a dict with ``api_key``, ``host``, ``port``, or ``None``.
        Tracks the instance for idle reaping.

        Uses a per-key lock so concurrent requests for the same user+policy
        don't race to provision the same container.
        """
        key = self._key(user_id, policy_id)

        # Fast path — already tracked and running.
        if key in self._instances:
            info = self._instances[key]
            checked = self._running_checked_at.get(key)
            if (
                settings.status_cache_ttl > 0
                and checked is not None
                and time.monotonic() - checked < settings.status_cache_ttl
            ):
                self._record_activity(key)
                return info
            st = await self.status(info["instance_id"])
            if st == "running":
                self._running_checked_at[key] = time.monotonic()
                self._record_activity(key)
                return info

        # Serialise provisioning per key.
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()

        async with self._locks[key]:
            # Re-check after acquiring lock — another request may have
            # already provisioned while we were waiting.
            if key in self._instances:
                info = self._instances[key]
                st = await self.status(info["instance_id"])
                if st == "running":
                    self._running_checked_at[key] = time.monotonic()
                    self._record_activity(key)
                    return info
                self._instances.pop(key, None)
                self._specs.pop(key, None)
                self._activity.pop(key, None)
                self._activity_wall.pop(key, None)
                self._running_checked_at.pop(key, None)
                self._activity_synced_at.pop(key, None)

            await self._apply_due_reset(user_id, policy_id, spec)
            result = await self.provision(user_id, policy_id=policy_id, spec=spec)
            if result:
                self._instances[key] = result
                self._specs[key] = spec or {}
                self._running_checked_at[key] = time.monotonic()
                self._record_activity(key)
            return result

    async def get_terminal_info(self, user_id: str) -> Optional[dict]:
        """Look up an existing terminal without creating one."""
        return None

    async def touch_activity(
        self, user_id: str, policy_id: str = "default"
    ) -> None:
        """Record that *user_id*'s terminal is actively being used."""
        key = self._key(user_id, policy_id)
        self._record_activity(key)
        checked = self._activity_synced_at.get(key)
        interval = max(1, min(settings.status_cache_ttl or 30, 60))
        if checked is not None and time.monotonic() - checked < interval:
            return
        await mark_terminal_active(user_id, policy_id)
        self._activity_synced_at[key] = time.monotonic()

    async def _apply_due_reset(
        self, user_id: str, policy_id: str, spec: Optional[dict]
    ) -> bool:
        if not await reset_due_for(user_id, policy_id, spec):
            return False
        await self.reset(user_id, policy_id, spec)
        await mark_reset_applied(user_id, policy_id, spec)
        log.info("Reset files for user=%s policy=%s", user_id, policy_id)
        return True

    def _tracked_items(
        self,
        *,
        user_id: str | None = None,
        policy_id: str | None = None,
    ) -> list[tuple[str, str, str, dict, dict]]:
        matches = []
        for key, info in list(self._instances.items()):
            item_user, item_policy = key.split(":", 1)
            if user_id and item_user != user_id:
                continue
            if policy_id and item_policy != policy_id:
                continue
            matches.append((key, item_user, item_policy, info, self._specs.get(key, {})))
        return matches

    def _is_idle_by_activity(self, key: str, spec: Optional[dict], now: float) -> bool:
        timeout_min = (spec or {}).get(
            "idle_timeout_minutes", settings.idle_timeout_minutes
        )
        if not timeout_min or timeout_min <= 0:
            return False
        last_active = self._activity.get(key, now)
        return now - last_active >= timeout_min * 60

    async def refresh(
        self,
        *,
        user_id: str | None = None,
        policy_id: str | None = None,
        only_idle: bool = True,
        reset: bool = False,
    ) -> RefreshResult:
        """Tear down matching terminals so the next access provisions fresh."""
        result = RefreshResult()
        now = time.monotonic()

        for key, item_user, item_policy, info, spec in self._tracked_items(
            user_id=user_id, policy_id=policy_id
        ):
            result.matched += 1
            st = await self.status(info["instance_id"])
            idle = st != "running" or self._is_idle_by_activity(key, spec, now)
            if only_idle and not idle:
                result.skipped_active += 1
                continue

            await self.teardown(info["instance_id"])
            self._instances.pop(key, None)
            self._specs.pop(key, None)
            self._activity.pop(key, None)
            self._activity_wall.pop(key, None)
            self._running_checked_at.pop(key, None)
            self._activity_synced_at.pop(key, None)
            self._locks.pop(key, None)
            result.refreshed += 1

            if reset:
                await self.reset(item_user, item_policy, spec)
                result.reset += 1

        return result

    async def list_terminals(self) -> list[dict]:
        """Return sanitized tracked terminal instances for the admin UI."""
        rows = []
        now = time.monotonic()
        for key, user_id, policy_id, info, spec in self._tracked_items():
            status = await self.status(info["instance_id"])
            last_active = self._activity.get(key)
            last_active_wall = self._activity_wall.get(key)
            timeout_min = (spec or {}).get(
                "idle_timeout_minutes", settings.idle_timeout_minutes
            )
            rows.append(
                {
                    "user_id": user_id,
                    "policy_id": policy_id,
                    "status": status,
                    "instance_id": info.get("instance_id", ""),
                    "instance_name": info.get("instance_name", info.get("instance_id", "")),
                    "host": info.get("host", ""),
                    "port": info.get("port"),
                    "last_active_at": (
                        datetime.fromtimestamp(last_active_wall, timezone.utc).isoformat()
                        if last_active_wall
                        else None
                    ),
                    "idle_seconds": int(now - last_active) if last_active else None,
                    "idle_timeout_minutes": timeout_min or 0,
                }
            )
        return rows

    # ------------------------------------------------------------------
    # Idle reaper
    # ------------------------------------------------------------------

    def start_reaper(self) -> None:
        """Start the background idle-reaper task."""
        if self._reaper_task is not None:
            return
        self._reaper_task = asyncio.create_task(self._reaper_loop())
        log.info("Idle reaper started")

    async def stop_reaper(self) -> None:
        """Cancel the reaper and wait for it to finish."""
        if self._reaper_task is None:
            return
        self._reaper_task.cancel()
        try:
            await self._reaper_task
        except asyncio.CancelledError:
            pass
        self._reaper_task = None
        log.info("Idle reaper stopped")

    async def _reaper_loop(self) -> None:
        """Periodically check for idle terminals and tear them down."""
        while True:
            try:
                await asyncio.sleep(60)
                await self._reap_idle()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Idle reaper error")

    async def _reap_idle(self) -> None:
        """Apply due resets and tear down idle instances across the fleet.

        Instances are processed concurrently with per-operation timeouts so
        one slow or hung backend call can't stall the whole sweep.
        """
        keys = list(self._instances)
        if not keys:
            return

        semaphore = asyncio.Semaphore(max(1, settings.reaper_concurrency))

        async def _reap_one(key: str) -> None:
            async with semaphore:
                await self._reap_instance(key)

        results = await asyncio.gather(
            *(_reap_one(key) for key in keys), return_exceptions=True
        )
        for key, result in zip(keys, results):
            if isinstance(result, BaseException):
                log.error("Reaper task for %s failed: %r", key, result)

    async def _reap_instance(self, key: str) -> None:
        """Apply a due reset or idle-timeout teardown for one tracked instance."""
        info = self._instances.get(key)
        if info is None:
            return

        now = time.monotonic()
        op_timeout = settings.reaper_op_timeout_seconds
        spec = self._specs.get(key, {})
        user_id, policy_id = key.split(":", 1)

        try:
            reset_due = await asyncio.wait_for(
                reset_due_for(user_id, policy_id, spec), op_timeout
            )
        except Exception:
            log.exception("Failed to check due reset for %s", key)
            return

        if reset_due:
            log.info(
                "Refreshing terminal %s for due reset (user=%s, policy=%s)",
                info.get("instance_name", info.get("instance_id")),
                user_id,
                policy_id,
            )
            try:
                await asyncio.wait_for(self.teardown(info["instance_id"]), op_timeout)
            except asyncio.TimeoutError:
                # State unknown — keep tracked so the next sweep retries.
                log.error(
                    "Teardown of %s timed out after %.0fs; will retry next sweep",
                    key, op_timeout,
                )
                return
            except Exception:
                log.exception("Failed to tear down %s for scheduled reset", key)
            try:
                await asyncio.wait_for(self.reset(user_id, policy_id, spec), op_timeout)
                await asyncio.wait_for(
                    mark_reset_applied(user_id, policy_id, spec), op_timeout
                )
            except NotImplementedError:
                log.warning("Reset due for %s but backend does not support it", key)
            except asyncio.TimeoutError:
                log.error("Reset for %s timed out after %.0fs", key, op_timeout)
            except Exception:
                log.exception("Failed to reset files for %s", key)
            self._instances.pop(key, None)
            self._specs.pop(key, None)
            self._activity.pop(key, None)
            self._activity_wall.pop(key, None)
            self._running_checked_at.pop(key, None)
            self._activity_synced_at.pop(key, None)
            self._locks.pop(key, None)
            return

        timeout_min = spec.get(
            "idle_timeout_minutes", settings.idle_timeout_minutes
        )
        if not timeout_min or timeout_min <= 0:
            return

        try:
            shared_last_active = await asyncio.wait_for(
                terminal_last_active_at(user_id, policy_id), op_timeout
            )
        except Exception:
            log.exception("Failed to load persisted activity for %s", key)
            shared_last_active = None
        if shared_last_active:
            shared_wall = shared_last_active.replace(tzinfo=timezone.utc).timestamp()
            if shared_wall > self._activity_wall.get(key, 0):
                self._activity_wall[key] = shared_wall
                self._activity[key] = now

        last_active = self._activity.get(key, now)
        idle_seconds = now - last_active
        if idle_seconds < timeout_min * 60:
            return

        log.info(
            "Reaping idle terminal %s (user=%s, policy=%s, idle=%.0fs, timeout=%dm)",
            info.get("instance_name", info.get("instance_id")),
            user_id,
            policy_id,
            idle_seconds,
            timeout_min,
        )
        try:
            await asyncio.wait_for(self.teardown(info["instance_id"]), op_timeout)
        except asyncio.TimeoutError:
            # State unknown — keep tracked so the next sweep retries.
            log.error(
                "Teardown of %s timed out after %.0fs; will retry next sweep",
                key, op_timeout,
            )
            return
        except Exception:
            log.exception("Failed to tear down %s", key)
        try:
            await asyncio.wait_for(
                self._apply_due_reset(user_id, policy_id, spec), op_timeout
            )
        except NotImplementedError:
            log.warning("Reset due for %s but backend does not support it", key)
        except asyncio.TimeoutError:
            log.error("Reset for %s timed out after %.0fs", key, op_timeout)
        except Exception:
            log.exception("Failed to reset files for %s", key)
        self._instances.pop(key, None)
        self._specs.pop(key, None)
        self._activity.pop(key, None)
        self._activity_wall.pop(key, None)
        self._running_checked_at.pop(key, None)
        self._activity_synced_at.pop(key, None)
        self._locks.pop(key, None)
