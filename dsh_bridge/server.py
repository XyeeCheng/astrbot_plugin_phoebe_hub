import asyncio
import hmac
import json
import os
import re
import signal
import secrets
import sqlite3
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

from .worker import read_secret


def validate(data):
    if not isinstance(data, dict):
        raise ValueError("request object required")
    for key in ("scope", "request_id"):
        if not isinstance(data.get(key), str) or not re.fullmatch(
            r"[a-f0-9]{64}", data[key]
        ):
            raise ValueError("invalid identity")
    for key, limit in (("prompt", 30000), ("system", 24000)):
        if not isinstance(data.get(key), str) or not 1 <= len(data[key]) <= limit:
            raise ValueError("invalid text")
    history = data.get("history", [])
    if not isinstance(history, list) or len(history) > 128:
        raise ValueError("invalid history")
    for item in history:
        if (
            not isinstance(item, dict)
            or item.get("role") not in ("user", "assistant", "tool")
            or len(json.dumps(item, ensure_ascii=False)) > 30000
        ):
            raise ValueError("invalid history item")
    tools = data.get("tools", [])
    if not isinstance(tools, list) or len(tools) > 16:
        raise ValueError("invalid tools")
    for tool in tools:
        if (
            not isinstance(tool, dict)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(tool.get("name", "")))
            or not isinstance(tool.get("description"), str)
            or len(tool["description"]) > 6000
            or not isinstance(tool.get("parameters"), dict)
        ):
            raise ValueError("invalid tool schema")
    return {k: data[k] for k in ("scope", "request_id", "prompt", "system")} | {
        "history": history,
        "tools": tools,
    }


async def execute(payload, timeout=45):
    if sys.platform != "linux":
        raise RuntimeError("The production bridge requires Linux process-group cleanup")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "dsh_bridge.worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        out, _ = await asyncio.wait_for(
            proc.communicate(json.dumps(payload, ensure_ascii=False).encode()), timeout
        )
        if proc.returncode != 0 or len(out) > 65536:
            raise RuntimeError("DSH worker failed")
        data = json.loads(out)
        if data.get("request_id") != payload["request_id"] or not isinstance(
            data.get("text"), str
        ):
            raise RuntimeError("DSH worker failed")
        return data
    finally:
        # Kill the group even if the worker exited; an interrupted SDK child must not survive.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()


def create_app(token, quota_path, *, executor=execute, daily_limit=200):
    if len(token) < 32:
        raise ValueError("Bridge token must contain at least 32 characters")
    quota_path = Path(quota_path)
    quota_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(quota_path)
    db.execute(
        "CREATE TABLE IF NOT EXISTS quota(date TEXT PRIMARY KEY, used INTEGER NOT NULL)"
    )
    lock = asyncio.Lock()
    cache = OrderedDict()
    jobs = {}

    @web.middleware
    async def auth(request, handler):
        incoming = request.headers.get("Authorization", "")
        if request.method == "POST" and request.path.endswith("/call"):
            job = jobs.get(
                (request.match_info.get("scope"), request.match_info.get("request_id"))
            )
            if job and hmac.compare_digest(
                incoming.encode(), ("Bearer " + job["token"]).encode()
            ):
                return await handler(request)
            return web.json_response({"error": "unauthorized"}, status=401)
        if not hmac.compare_digest(incoming.encode(), ("Bearer " + token).encode()):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    app = web.Application(client_max_size=65536, middlewares=[auth])

    async def health(request):
        return web.json_response(
            {
                "bridge": "ok",
                "sdk": "0.1.5rc1",
                "tool_protocol": 2,
                "tools": ["phoebe_time", "request_scoped_astrbot_tools"],
                "note": "process health; not an API connectivity test",
            }
        )

    async def chat(request):
        try:
            payload = validate(await request.json())
        except (ValueError, TypeError, KeyError):
            return web.json_response({"error": "bad_request"}, status=400)
        key = payload["scope"], payload["request_id"]
        # Only IDs, timestamps and outcome flags are cached; no prompt or answer retention.
        if key in cache:
            return web.json_response({"error": "duplicate_request"}, status=409)
        if lock.locked():
            return web.json_response({"error": "busy"}, status=429)
        async with lock:
            today = datetime.now(timezone.utc).date().isoformat()
            with db:
                db.execute("INSERT OR IGNORE INTO quota VALUES(?,0)", (today,))
                used = db.execute(
                    "SELECT used FROM quota WHERE date=?", (today,)
                ).fetchone()[0]
                if used >= daily_limit:
                    return web.json_response({"error": "daily_limit"}, status=429)
                db.execute("UPDATE quota SET used=used+1 WHERE date=?", (today,))
                db.execute(
                    "DELETE FROM quota WHERE date < date(?, '-30 days')", (today,)
                )
            cache[key] = time.monotonic()
            while len(cache) > 4096:
                cache.popitem(last=False)
            try:
                job = {
                    "token": secrets.token_hex(32),
                    "queue": asyncio.Queue(),
                    "pending": {},
                    "names": {t["name"] for t in payload["tools"]},
                    "used": 0,
                }
                jobs[key] = job
                port = request.transport.get_extra_info("sockname")[1]
                payload["tool_channel"] = {
                    "url": f"http://127.0.0.1:{port}/v1/tools/{key[0]}/{key[1]}/call",
                    "token": job["token"],
                }
                result = await executor(payload)
                return web.json_response(result)
            except asyncio.CancelledError:
                raise
            except Exception:
                return web.json_response({"error": "dsh_unavailable"}, status=503)
            finally:
                job = jobs.pop(key, None)
                if job:
                    for future in job["pending"].values():
                        future.cancel()

    def get_job(request):
        return jobs.get((request.match_info["scope"], request.match_info["request_id"]))

    async def tool_call(request):
        job = get_job(request)
        if not job:
            return web.json_response({"error": "expired"}, status=404)
        try:
            data = await request.json()
            if (
                data.get("name") not in job["names"]
                or not isinstance(data.get("arguments"), dict)
                or job["used"] >= 6
            ):
                raise ValueError("not permitted")
        except (ValueError, TypeError, AttributeError):
            return web.json_response({"error": "bad_request"}, status=400)
        job["used"] += 1
        call_id = secrets.token_hex(16)
        future = asyncio.get_running_loop().create_future()
        job["pending"][call_id] = future
        await job["queue"].put(
            {"call_id": call_id, "name": data["name"], "arguments": data["arguments"]}
        )
        try:
            result = await asyncio.wait_for(future, 20)
            return web.json_response(result)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return web.json_response(
                {"text": "查询超时，未得到结果。", "isError": True}
            )
        finally:
            job["pending"].pop(call_id, None)

    async def tool_poll(request):
        job = get_job(request)
        if not job:
            return web.json_response({"error": "expired"}, status=404)
        try:
            return web.json_response(await asyncio.wait_for(job["queue"].get(), 1))
        except asyncio.TimeoutError:
            return web.Response(status=204)

    async def tool_result(request):
        job = get_job(request)
        if not job:
            return web.json_response({"error": "expired"}, status=404)
        try:
            data = await request.json()
            future = job["pending"].get(data.get("call_id"))
            if (
                not future
                or future.done()
                or not isinstance(data.get("text"), str)
                or len(data["text"]) > 16000
                or not isinstance(data.get("isError"), bool)
            ):
                raise ValueError("invalid result")
            future.set_result({"text": data["text"], "isError": data["isError"]})
            return web.json_response({"ok": True})
        except (ValueError, TypeError, AttributeError):
            return web.json_response({"error": "bad_request"}, status=400)

    async def cleanup(app):
        db.close()

    app.router.add_get("/health", health)
    app.router.add_post("/v1/chat", chat)
    app.router.add_post("/v1/tools/{scope}/{request_id}/call", tool_call)
    app.router.add_get("/v1/tools/{scope}/{request_id}", tool_poll)
    app.router.add_post("/v1/tools/{scope}/{request_id}/result", tool_result)
    app.on_cleanup.append(cleanup)
    return app


def main():
    app = create_app(
        read_secret("HUB_BRIDGE_TOKEN"),
        os.environ.get("HUB_QUOTA_DB", "/state/quota.sqlite3"),
        daily_limit=max(1, int(os.environ.get("HUB_DAILY_LIMIT", "200"))),
    )
    web.run_app(
        app, host=os.environ.get("HUB_LISTEN", "0.0.0.0"), port=8099, access_log=None
    )


if __name__ == "__main__":
    main()
