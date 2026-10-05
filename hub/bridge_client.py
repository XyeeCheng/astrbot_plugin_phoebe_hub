import asyncio
import json
import re
from urllib.parse import urlsplit

import aiohttp


class BridgeUnavailable(Exception):
    """Intentionally contains no request bodies, credentials or upstream error text."""


class BridgeClient:
    def __init__(self, settings):
        self.settings = settings
        parsed = urlsplit(settings.dsh_url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("DSH 地址必须是无用户信息、查询参数的 HTTP(S) 服务地址")
        self.url = settings.dsh_url.rstrip("/")
        self.session = None

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def run(
        self,
        scope,
        request_id,
        prompt,
        system,
        history,
        *,
        schemas=None,
        tool_executor=None,
    ):
        if not self.settings.dsh_token:
            raise BridgeUnavailable("DSH token missing")
        if not re.fullmatch(r"[0-9a-f]{64}", scope):
            raise BridgeUnavailable("Invalid scope")
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.settings.dsh_timeout)
            )
        payload = {
            "scope": scope,
            "request_id": request_id,
            "prompt": prompt,
            "system": system,
            "history": history,
        }
        headers = {"Authorization": "Bearer " + self.settings.dsh_token}
        if schemas:
            payload["tools"] = schemas
        polling = None
        try:
            if schemas:
                async with self.session.get(
                    self.url + "/health",
                    headers=headers,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=3),
                ) as response:
                    if (
                        response.status != 200
                        or (await response.json()).get("tool_protocol") != 2
                    ):
                        raise BridgeUnavailable("Bridge tool protocol unavailable")
                polling = asyncio.create_task(
                    self._poll(scope, request_id, tool_executor, headers)
                )
            async with self.session.post(
                self.url + "/v1/chat",
                json=payload,
                headers=headers,
                allow_redirects=False,
            ) as response:
                if (
                    response.status != 200
                    or response.content_length
                    and response.content_length > 65536
                ):
                    raise BridgeUnavailable("DSH unavailable")
                raw = await response.content.read(65537)
                if len(raw) > 65536:
                    raise BridgeUnavailable("DSH response too large")
                data = json.loads(raw)
                if data.get("request_id") != request_id or not isinstance(
                    data.get("text"), str
                ):
                    raise BridgeUnavailable("Invalid DSH response")
                if not data["text"].strip():
                    raise BridgeUnavailable("Empty DSH response")
                return data["text"]
        except BridgeUnavailable:
            raise
        except Exception:
            raise BridgeUnavailable("DSH unavailable") from None
        finally:
            if polling:
                polling.cancel()
                await asyncio.gather(polling, return_exceptions=True)

    async def _poll(self, scope, request_id, executor, headers):
        base = self.url + "/v1/tools/" + scope + "/" + request_id
        while True:
            async with self.session.get(
                base, headers=headers, allow_redirects=False
            ) as response:
                if response.status in (204, 404):
                    await asyncio.sleep(0.1)
                    continue
                if response.status != 200:
                    raise BridgeUnavailable("Tool channel unavailable")
                call = await response.json()
            try:
                result = await executor(call["name"], call["arguments"])
                value = {
                    "text": "\n".join(
                        c.text for c in result.content if c.type == "text"
                    ),
                    "isError": bool(result.isError),
                }
            except Exception:
                value = {"text": "查询失败，未得到可核实结果。", "isError": True}
            async with self.session.post(
                base + "/result",
                headers=headers,
                json={"call_id": call["call_id"], **value},
                allow_redirects=False,
            ) as response:
                if response.status not in (200, 404):
                    raise BridgeUnavailable("Tool result channel unavailable")
