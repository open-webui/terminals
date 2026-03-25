"""Abstract base class for terminal backends."""

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Optional

from terminals.config import settings

log = logging.getLogger(__name__)


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
        self._instances: dict[str, dict] = {}       # → provision result dict
        self._specs: dict[str, dict] = {}           # → resolved policy spec
        self._locks: dict[str, asyncio.Lock] = {}   # → per-key provisioning lock
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

    async def list_instances(self) -> list[dict]:
        """Return all tracked terminal instances with status info.

        Each dict contains at least: ``user_id``, ``policy_id``,
        ``instance_id``, ``status``, ``instance_name``.

        Backends may override for richer information (e.g. querying CRDs).
        The default implementation returns in-memory tracked instances.
        """
        results = []
        for key, info in list(self._instances.items()):
            parts = key.split(":", 1)
            user_id = parts[0]
            policy_id = parts[1] if len(parts) > 1 else "default"
            st = await self.status(info["instance_id"])
            results.append({
                "user_id": user_id,
                "policy_id": policy_id,
                "instance_id": info["instance_id"],
                "instance_name": info.get("instance_name", ""),
                "status": st,
                "host": info.get("host", ""),
                "port": info.get("port", 0),
            })
        return results

    # ------------------------------------------------------------------
    # Instance tracking
    # ------------------------------------------------------------------

    @staticmethod
    def _key(user_id: str, policy_id: str = "default") -> str:
        return f"{user_id}:{policy_id}"

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
            st = await self.status(info["instance_id"])
            if st == "running":
                self._activity[key] = time.monotonic()
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
                    self._activity[key] = time.monotonic()
                    return info
                self._instances.pop(key, None)
                self._specs.pop(key, None)
                self._activity.pop(key, None)

            result = await self.provision(user_id, policy_id=policy_id, spec=spec)
            if result:
                self._instances[key] = result
                self._specs[key] = spec or {}
                self._activity[key] = time.monotonic()
            return result

    async def get_terminal_info(self, user_id: str) -> Optional[dict]:
        """Look up an existing terminal without creating one."""
        return None

    async def touch_activity(
        self, user_id: str, policy_id: str = "default"
    ) -> None:
        """Record that *user_id*'s terminal is actively being used."""
        key = self._key(user_id, policy_id)
        self._activity[key] = time.monotonic()

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
        """Scan tracked instances and tear down any that exceeded their timeout."""
        now = time.monotonic()

        for key in list(self._instances):
            info = self._instances.get(key)
            if info is None:
                continue

            spec = self._specs.get(key, {})
            timeout_min = spec.get(
                "idle_timeout_minutes", settings.idle_timeout_minutes
            )
            if not timeout_min or timeout_min <= 0:
                continue

            last_active = self._activity.get(key, now)
            idle_seconds = now - last_active

            if idle_seconds >= timeout_min * 60:
                parts = key.split(":", 1)
                user_id = parts[0]
                policy_id = parts[1] if len(parts) > 1 else "default"
                log.info(
                    "Reaping idle terminal %s (user=%s, policy=%s, idle=%.0fs, timeout=%dm)",
                    info.get("instance_name", info.get("instance_id")),
                    user_id,
                    policy_id,
                    idle_seconds,
                    timeout_min,
                )
                try:
                    await self.teardown(info["instance_id"])
                except Exception:
                    log.exception("Failed to tear down %s", key)
                self._instances.pop(key, None)
                self._specs.pop(key, None)
                self._activity.pop(key, None)
                self._locks.pop(key, None)
