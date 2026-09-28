import re
from urllib.parse import urlsplit

import aiohttp


class BridgeUnavailable(Exception):
    """Intentionally contains no request bodies, credentials or upstream error text."""


class BridgeClient:
    def __init__(self, settings):
        self.settings = settings
        parsed = urlsplit(settings.dsh_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("DSH 地址必须是无用户信息、查询参数的 HTTP(S) 服务地址")
        self.url = settings.dsh_url.rstrip("/")
        self.session = None

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def run(self, scope, request_id, prompt, system, history):
        if not self.settings.dsh_token:
            raise BridgeUnavailable("DSH token missing")
        if not re.fullmatch(r"[0-9a-f]{64}", scope):
            raise BridgeUnavailable("Invalid scope")
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.settings.dsh_timeout))
        payload = {"scope": scope, "request_id": request_id, "prompt": prompt[:6000],
                   "system": system, "history": history}
        try:
            async with self.session.post(self.url + "/v1/chat", json=payload,
                                         headers={"Authorization": "Bearer " + self.settings.dsh_token},
                                         allow_redirects=False) as response:
                if response.status != 200 or response.content_length and response.content_length > 65536:
                    raise BridgeUnavailable("DSH unavailable")
                raw = await response.content.read(65537)
                if len(raw) > 65536:
                    raise BridgeUnavailable("DSH response too large")
                import json
                data = json.loads(raw)
                if data.get("request_id") != request_id or not isinstance(data.get("text"), str):
                    raise BridgeUnavailable("Invalid DSH response")
                if not data["text"].strip():
                    raise BridgeUnavailable("Empty DSH response")
                return data["text"]
        except BridgeUnavailable:
            raise
        except Exception:
            raise BridgeUnavailable("DSH unavailable") from None
