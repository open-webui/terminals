import time
import unittest

from terminals.backends.base import Backend
from terminals.config import settings


class FakeBackend(Backend):
    """In-memory backend for exercising the status-cache behaviour."""

    def __init__(self) -> None:
        super().__init__()
        self.status_calls = 0
        self.provision_calls = 0
        self.status_result = "running"

    async def provision(self, user_id, policy_id="default", spec=None):
        self.provision_calls += 1
        return {
            "instance_id": f"i-{user_id}-{policy_id}",
            "instance_name": f"n-{user_id}-{policy_id}",
            "api_key": "key",
            "host": "127.0.0.1",
            "port": 9999,
        }

    async def start(self, instance_id):
        return True

    async def teardown(self, instance_id):
        pass

    async def status(self, instance_id):
        self.status_calls += 1
        return self.status_result

    async def close(self):
        pass

    async def _apply_due_reset(self, user_id, policy_id, spec):
        return False


class StatusCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._orig_ttl = settings.status_cache_ttl
        self.backend = FakeBackend()

    def tearDown(self) -> None:
        settings.status_cache_ttl = self._orig_ttl

    async def test_fresh_status_skips_backend_inspection(self) -> None:
        settings.status_cache_ttl = 30
        await self.backend.ensure_terminal("u1")
        self.assertEqual(self.backend.provision_calls, 1)

        for _ in range(5):
            info = await self.backend.ensure_terminal("u1")
        self.assertIsNotNone(info)
        self.assertEqual(self.backend.status_calls, 0)
        self.assertEqual(self.backend.provision_calls, 1)

    async def test_ttl_zero_checks_every_request(self) -> None:
        settings.status_cache_ttl = 0
        await self.backend.ensure_terminal("u1")
        await self.backend.ensure_terminal("u1")
        await self.backend.ensure_terminal("u1")
        self.assertEqual(self.backend.status_calls, 2)

    async def test_expired_status_rechecks(self) -> None:
        settings.status_cache_ttl = 30
        await self.backend.ensure_terminal("u1")
        # Age the cached check past the TTL.
        key = self.backend._key("u1", "default")
        self.backend._status_ok_at[key] = time.monotonic() - 31
        await self.backend.ensure_terminal("u1")
        self.assertEqual(self.backend.status_calls, 1)

    async def test_invalidate_status_forces_recheck(self) -> None:
        settings.status_cache_ttl = 30
        await self.backend.ensure_terminal("u1")
        self.backend.invalidate_status("u1")
        await self.backend.ensure_terminal("u1")
        self.assertEqual(self.backend.status_calls, 1)

    async def test_dead_instance_reprovisions_after_invalidation(self) -> None:
        settings.status_cache_ttl = 30
        await self.backend.ensure_terminal("u1")
        self.backend.status_result = "missing"
        self.backend.invalidate_status("u1")
        await self.backend.ensure_terminal("u1")
        self.assertEqual(self.backend.provision_calls, 2)


if __name__ == "__main__":
    unittest.main()
