"""Abstract base class for terminal backends."""

from abc import ABC, abstractmethod
from typing import Optional


class Backend(ABC):
    """Lifecycle interface for provisioning and managing terminal instances."""

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

    async def ensure_terminal(
        self,
        user_id: str,
        policy_id: str = "default",
        spec: Optional[dict] = None,
    ) -> Optional[dict]:
        """Get-or-create a terminal for *user_id*.

        Returns a dict with ``api_key``, ``host``, ``port``, or ``None``.
        Default delegates to :meth:`provision` (already idempotent).
        """
        return await self.provision(user_id, policy_id=policy_id, spec=spec)

    async def get_terminal_info(self, user_id: str) -> Optional[dict]:
        """Look up an existing terminal without creating one."""
        return None

    async def touch_activity(self, user_id: str) -> None:
        """Record that *user_id*'s terminal is actively being used."""
