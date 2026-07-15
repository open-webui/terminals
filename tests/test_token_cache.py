import unittest

from terminals.config import settings
from terminals.routers import auth


class _FakeResponse:
    status_code = 200

    @staticmethod
    def json():
        return {"id": "user-1"}


class _FakeClient:
    def __init__(self) -> None:
        self.calls = 0

    async def get(self, url, headers=None):
        self.calls += 1
        return _FakeResponse()


class TokenCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._orig_ttl = settings.token_cache_ttl
        self._orig_client = auth._owui_client
        self.fake = _FakeClient()
        auth._owui_client = self.fake
        auth._token_cache.clear()

    def tearDown(self) -> None:
        settings.token_cache_ttl = self._orig_ttl
        auth._owui_client = self._orig_client
        auth._token_cache.clear()

    async def test_validation_is_cached(self) -> None:
        settings.token_cache_ttl = 60
        self.assertEqual(await auth.validate_token("tok"), "user-1")
        self.assertEqual(await auth.validate_token("tok"), "user-1")
        self.assertEqual(self.fake.calls, 1)

    async def test_distinct_tokens_validate_separately(self) -> None:
        settings.token_cache_ttl = 60
        await auth.validate_token("tok-a")
        await auth.validate_token("tok-b")
        self.assertEqual(self.fake.calls, 2)

    async def test_ttl_zero_disables_cache(self) -> None:
        settings.token_cache_ttl = 0
        await auth.validate_token("tok")
        await auth.validate_token("tok")
        self.assertEqual(self.fake.calls, 2)
        self.assertEqual(len(auth._token_cache), 0)


if __name__ == "__main__":
    unittest.main()
