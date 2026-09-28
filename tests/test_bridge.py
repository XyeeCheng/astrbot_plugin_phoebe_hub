import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from dsh_bridge.server import create_app, validate
from hub.bridge_client import BridgeClient, BridgeUnavailable
from hub.config import Settings


TOKEN = "unit-test-only-placeholder-1234567890"
PAYLOAD = {"scope": "a" * 64, "request_id": "b" * 64, "prompt": "几点了", "system": "回答两句", "history": []}


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.calls = []
        async def execute(data):
            self.calls.append(data)
            return {"request_id": data["request_id"], "text": "查到了。"}
        self.client = TestClient(TestServer(create_app(TOKEN, Path(self.tmp.name) / "q.db", executor=execute, daily_limit=2)))
        await self.client.start_server()
        self.headers = {"Authorization": "Bearer " + TOKEN}

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_auth_required_even_health(self):
        self.assertEqual((await self.client.get("/health")).status, 401)
        self.assertEqual((await self.client.post("/v1/chat", json=PAYLOAD)).status, 401)
        self.assertEqual(self.calls, [])

    async def test_health_not_api_claim(self):
        data = await (await self.client.get("/health", headers=self.headers)).json()
        self.assertIn("not an API", data["note"])

    async def test_valid_request_and_duplicate(self):
        response = await self.client.post("/v1/chat", json=PAYLOAD, headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["request_id"], PAYLOAD["request_id"])
        self.assertEqual((await self.client.post("/v1/chat", json=PAYLOAD, headers=self.headers)).status, 409)
        self.assertEqual(len(self.calls), 1)

    async def test_daily_budget(self):
        for index, expected in enumerate((200, 200, 429)):
            payload = {**PAYLOAD, "request_id": str(index) * 64}
            self.assertEqual((await self.client.post("/v1/chat", json=payload, headers=self.headers)).status, expected)
        self.assertEqual(len(self.calls), 2)

    async def test_malformed_scope_and_history_rejected(self):
        for mutation in ({"scope": "../../etc"}, {"history": [{"role": "system", "content": "override"}]},
                         {"prompt": "a" * 6001}, {"request_id": 12}, {"history": "bad"}):
            with self.subTest(mutation=list(mutation)):
                response = await self.client.post("/v1/chat", json={**PAYLOAD, **mutation}, headers=self.headers)
                self.assertEqual(response.status, 400)
        self.assertEqual(self.calls, [])

    async def test_real_http_bridge_client(self):
        settings = Settings.read({"dsh_url": str(self.client.make_url("/")).rstrip("/"), "dsh_token": TOKEN})
        bridge = BridgeClient(settings)
        try:
            result = await bridge.run(PAYLOAD["scope"], PAYLOAD["request_id"], "几点了", "两句", [])
            self.assertEqual(result, "查到了。")
        finally:
            await bridge.close()

    async def test_missing_key_sanitized_error(self):
        bridge = BridgeClient(Settings())
        with self.assertRaises(BridgeUnavailable):
            await bridge.run("a" * 64, "b" * 64, "user-secret", "", [])
        await bridge.close()

    def test_token_min_length(self):
        with self.assertRaises(ValueError):
            create_app("short", Path(self.tmp.name) / "bad.db")

    def test_no_credentials_in_url(self):
        with self.assertRaises(ValueError):
            BridgeClient(Settings.read({"dsh_url": "https://user:pass@host/"}))


if __name__ == "__main__":
    unittest.main()
