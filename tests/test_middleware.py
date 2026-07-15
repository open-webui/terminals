import unittest

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from terminals.middleware import RequestIdMiddleware


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/ping")
    async def ping(request: Request):
        return {"request_id": request.state.request_id}

    return app


class RequestIdMiddlewareTests(unittest.TestCase):
    def test_generates_request_id(self) -> None:
        client = TestClient(_make_app())
        resp = client.get("/ping")
        self.assertEqual(resp.status_code, 200)
        rid = resp.json()["request_id"]
        self.assertTrue(rid)
        self.assertEqual(resp.headers["x-request-id"], rid)

    def test_preserves_incoming_request_id(self) -> None:
        client = TestClient(_make_app())
        resp = client.get("/ping", headers={"X-Request-Id": "abc-123"})
        self.assertEqual(resp.json()["request_id"], "abc-123")
        self.assertEqual(resp.headers["x-request-id"], "abc-123")


if __name__ == "__main__":
    unittest.main()
