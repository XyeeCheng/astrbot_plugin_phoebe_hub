"""Real DSH runtime + first-party plugin loader + local mock model API.

No real API key, QQ connection or external model request. Verifies actual tool
execution and model-facing schemas; must not be presented as a live model test.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dsh_bridge.worker import run


def smoke():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(data)
            names = {tool["function"]["name"] for tool in data.get("tools", [])}
            if names != {"phoebe_time"}:
                self.send_error(400, "unexpected tools")
                return
            has_result = any(m["role"] == "tool" for m in data["messages"])
            if has_result:
                assert any("UTC+8" in str(m.get("content", "")) for m in data["messages"] if m["role"] == "tool")
                delta = {"role": "assistant", "content": "北京时间已经查到了。你看，我可没瞎猜。"}
                reason = "stop"
            else:
                delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_clock", "type": "function",
                          "function": {"name": "phoebe_time", "arguments": "{}"}}]}
                reason = "tool_calls"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            chunks = [{"id": "mock", "object": "chat.completion.chunk", "created": 1, "model": "test-model",
                       "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                      {"id": "mock", "object": "chat.completion.chunk", "created": 1, "model": "test-model",
                       "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}]
            for chunk in chunks:
                self.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    class Server(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            import sys
            if not isinstance(sys.exc_info()[1], ConnectionResetError):
                super().handle_error(request, client_address)

    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = run({"scope": "a" * 64, "request_id": "b" * 64, "prompt": "用工具查询北京时间。", "history": [],
                      "system": "你是菲比，只回答一到两句。"}, base_url=f"http://127.0.0.1:{server.server_port}/v1",
                     api_key="local-test-placeholder", model="test-model")
        assert "没瞎猜" in result["text"], result
        assert len(requests) == 2, len(requests)
        assert all("dsh_session_log" not in request and "dsh_plugin_packages" not in request for request in requests)
        print("DSH runtime smoke PASS: 2 local model requests, phoebe_time executed, no shell tool, isolated temporary sessions")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    smoke()
