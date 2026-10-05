"""Pinned real DSH runtime, local model and authenticated host-tool mock."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dsh_bridge.worker import run


def smoke(bridge_mode=False):
    model_calls, tool_calls = [], []
    token = "ephemeral-local-test-capability"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/host-tools":
                assert self.headers.get("Authorization") == "Bearer " + token
                assert data == {
                    "name": "web_search_tavily",
                    "arguments": {"query": "VALORANT 下一场"},
                }
                tool_calls.append(data)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {
                            "text": "Verified VALORANT match https://example.org/match",
                            "isError": False,
                        }
                    ).encode()
                )
                return
            assert self.path == "/v1/chat/completions", self.path
            model_calls.append(data)
            names = {t["function"]["name"] for t in data.get("tools", [])}
            assert names == {"phoebe_time", "web_search_tavily"}, names
            has_result = any(m["role"] == "tool" for m in data["messages"])
            if has_result:
                assert any(
                    "Verified VALORANT" in str(m.get("content", ""))
                    for m in data["messages"]
                    if m["role"] == "tool"
                )
                delta, finish = (
                    {
                        "role": "assistant",
                        "content": "下一场已经核实。看这里：https://example.org/match",
                    },
                    "stop",
                )
            else:
                delta = {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "search1",
                            "type": "function",
                            "function": {
                                "name": "web_search_tavily",
                                "arguments": json.dumps(
                                    {"query": "VALORANT 下一场"}, ensure_ascii=False
                                ),
                            },
                        }
                    ],
                }
                finish = "tool_calls"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for choice in (
                {"index": 0, "delta": delta, "finish_reason": None},
                {"index": 0, "delta": {}, "finish_reason": finish},
            ):
                chunk = {
                    "id": "mock",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "test-model",
                    "choices": [choice],
                }
                self.wfile.write(
                    ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode()
                )
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    class Server(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            import sys

            if not isinstance(sys.exc_info()[1], ConnectionResetError):
                super().handle_error(request, client_address)

    server = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        payload = {
            "scope": "a" * 64,
            "request_id": "b" * 64,
            "prompt": "查询VALORANT下一场",
            "system": "只使用工具核实，回答两句。",
            "history": [],
            "tools": [
                {
                    "name": "web_search_tavily",
                    "description": "Search current facts, read only.",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                }
            ],
            "tool_channel": {"url": base + "/host-tools", "token": token},
        }
        if bridge_mode:
            import asyncio
            import os
            import tempfile
            from pathlib import Path
            from types import SimpleNamespace
            from unittest.mock import patch
            from aiohttp.test_utils import TestClient, TestServer
            from dsh_bridge.server import create_app
            from hub.bridge_client import BridgeClient
            from hub.config import Settings

            async def through_bridge():
                def runtime_processes():
                    found = set()
                    for entry in Path("/proc").iterdir():
                        if not entry.name.isdigit():
                            continue
                        try:
                            command = (entry / "cmdline").read_bytes()
                        except (OSError, PermissionError):
                            continue
                        if (
                            b"deepseek_harness" in command
                            or b"dsh_bridge.worker" in command
                        ):
                            found.add(entry.name)
                    return found

                before = runtime_processes()
                with tempfile.TemporaryDirectory() as directory:
                    bridge_token = "local-test-bridge-token-12345678901234567890"
                    client = TestClient(
                        TestServer(
                            create_app(bridge_token, Path(directory) / "quota.db")
                        )
                    )
                    await client.start_server()
                    bridge = BridgeClient(
                        Settings.read(
                            {
                                "dsh_url": str(client.make_url("/")).rstrip("/"),
                                "dsh_token": bridge_token,
                            }
                        )
                    )

                    async def execute(name, args):
                        assert name == "web_search_tavily"
                        assert args == {"query": "VALORANT 下一场"}
                        tool_calls.append({"name": name, "arguments": args})
                        return SimpleNamespace(
                            content=[
                                SimpleNamespace(
                                    type="text",
                                    text="Verified VALORANT match https://example.org/match",
                                )
                            ],
                            isError=False,
                        )

                    try:
                        return {
                            "text": await bridge.run(
                                payload["scope"],
                                payload["request_id"],
                                payload["prompt"],
                                payload["system"],
                                [],
                                schemas=payload["tools"],
                                tool_executor=execute,
                            )
                        }
                    finally:
                        await bridge.close()
                        await client.close()
                        assert runtime_processes() <= before, (
                            "Orphan DSH worker/runtime"
                        )

            with patch.dict(
                os.environ,
                {
                    "DEEPSEEK_API_KEY": "local-test-placeholder",
                    "DSH_MODEL": "test-model",
                    "DEEPSEEK_BASE_URL": base + "/v1",
                },
            ):
                result = asyncio.run(through_bridge())
        else:
            result = run(
                payload,
                base_url=base + "/v1",
                api_key="local-test-placeholder",
                model="test-model",
            )
        assert "已经核实" in result["text"], result
        assert len(model_calls) == 2, len(model_calls)
        assert len(tool_calls) == 1, len(tool_calls)
        print(
            "DSH tools smoke PASS: real SDK/runtime, host query once, two local model calls, source returned, no shell tools"
        )
        if bridge_mode:
            print(
                "Linux production bridge PASS: subprocess worker + real SDK + authenticated host tool queue + process-group cleanup"
            )
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    import sys

    smoke("--bridge" in sys.argv)
