"""Abstract base class for terminal backends."""

from abc import ABC, abstractmethod
from typing import Optional


class Backend(ABC):
    """Lifecycle interface for provisioning and managing terminal instances."""

    @abstractmethod
    async def provision(self, user_id: str) -> dict:
        """Create a new terminal instance for *user_id*.

        Returns a dict with at least:
        ``instance_id``, ``instance_name``, ``api_key``, ``host``, ``port``.
        """

    @abstractmethod
    async def start(self, instance_id: str) -> bool:
        """Idempotent start — no-op if already running.

        Returns ``True`` if the instance is now running.
        """

    @abstractmethod
    async def teardown(self, instance_id: str) -> None:
        """Stop and remove the instance."""

    @abstractmethod
    async def status(self, instance_id: str) -> str:
        """Return ``'running'``, ``'stopped'``, or ``'missing'``."""

    @abstractmethod
    async def close(self) -> None:
        """Release resources on shutdown."""

    # ------------------------------------------------------------------
    # Optional methods for DB-free operation
    # ------------------------------------------------------------------

    async def ensure_terminal(self, user_id: str) -> Optional[dict]:
        """Get-or-create a terminal for *user_id*.

        Returns a dict with ``api_key``, ``host``, ``port`` (and optionally
        ``instance_id``, ``instance_name``), or ``None`` if the terminal
        could not be resolved.

        The default implementation delegates to :meth:`provision`, which is
        already idempotent for most backends.  Backends with their own
        state store (e.g. Kubernetes CRDs) can override this to avoid
        requiring a database.
        """
        return await self.provision(user_id)

    async def get_terminal_info(self, user_id: str) -> Optional[dict]:
        """Look up an existing terminal without creating one.

        Returns the same dict shape as :meth:`ensure_terminal`, or ``None``
        if no terminal exists for *user_id*.  The default returns ``None``;
        backends that maintain their own state should override.
        """
        return None

    async def touch_activity(self, user_id: str) -> None:
        """Record that *user_id*'s terminal is actively being used.

        Backends that track idle time should override this to update the
        last-activity timestamp.  The default is a no-op.
        """
