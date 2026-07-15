import unittest

from terminals.routers import proxy


class PerOriginClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await proxy.close_proxy_client()

    async def asyncTearDown(self) -> None:
        await proxy.close_proxy_client()

    async def test_same_origin_reuses_client(self) -> None:
        a = await proxy._get_proxy_client("10.0.0.1", 8000)
        b = await proxy._get_proxy_client("10.0.0.1", 8000)
        self.assertIs(a, b)
        self.assertEqual(len(proxy._proxy_clients), 1)

    async def test_distinct_origins_get_distinct_clients(self) -> None:
        a = await proxy._get_proxy_client("10.0.0.1", 8000)
        b = await proxy._get_proxy_client("10.0.0.2", 8000)
        c = await proxy._get_proxy_client("10.0.0.1", 8001)
        self.assertNotEqual(id(a), id(b))
        self.assertNotEqual(id(a), id(c))
        self.assertEqual(len(proxy._proxy_clients), 3)

    async def test_lru_eviction_closes_oldest(self) -> None:
        original_max = proxy._PROXY_CLIENTS_MAX
        proxy._PROXY_CLIENTS_MAX = 2
        try:
            a = await proxy._get_proxy_client("10.0.0.1", 8000)
            await proxy._get_proxy_client("10.0.0.2", 8000)
            # Touch origin 1 so origin 2 becomes least-recently-used.
            await proxy._get_proxy_client("10.0.0.1", 8000)
            await proxy._get_proxy_client("10.0.0.3", 8000)

            self.assertEqual(len(proxy._proxy_clients), 2)
            self.assertIn(("10.0.0.1", 8000), proxy._proxy_clients)
            self.assertIn(("10.0.0.3", 8000), proxy._proxy_clients)
            self.assertNotIn(("10.0.0.2", 8000), proxy._proxy_clients)
            # The surviving client for a touched origin is the original object.
            self.assertIs(proxy._proxy_clients[("10.0.0.1", 8000)], a)
        finally:
            proxy._PROXY_CLIENTS_MAX = original_max


if __name__ == "__main__":
    unittest.main()
