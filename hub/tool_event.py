from astrbot.api.event import AstrMessageEvent


class ReadOnlyToolEvent(AstrMessageEvent):
    """Retain platform identity for read tools, but block their direct chat sends.

    The caller still must only allow read-only tools: arbitrary Python plugins
    are host code, not a sandbox, and could access Context independently.
    """
    def __init__(self, original):
        self.original = original
        self.result = None

    def __getattr__(self, name):
        return getattr(self.original, name)

    async def send(self, *args, **kwargs):
        raise RuntimeError("Direct sends are disabled for Hub read tools")

    async def send_streaming(self, *args, **kwargs):
        raise RuntimeError("Direct streaming is disabled for Hub read tools")

    def set_result(self, result):
        self.result = result

    def get_result(self):
        return self.result

    def clear_result(self):
        self.result = None
