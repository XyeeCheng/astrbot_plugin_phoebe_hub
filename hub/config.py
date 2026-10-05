from dataclasses import dataclass, fields


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    allowed_sessions: tuple = ()
    persona_id: str = "phoebe_tsundere"
    bot_name: str = "菲比"
    tsundere_level: int = 3
    initial_affinity: int = 20
    daily_gain: int = 5
    gain_cooldown: int = 600
    anger_seconds: int = 120
    anger_replies: int = 2
    trigger_cooldown: int = 60
    mother_aliases: tuple = ("妈妈", "妈咪", "麻麻")
    max_chars: int = 100
    history_turns: int = 8
    group_history_messages: int = 60
    context_chars: int = 12000
    topic_ttl: int = 1200
    auto_memory: bool = True
    retention_days: int = 7
    provider_id: str = ""
    engine: str = "native"
    dsh_url: str = "http://phoebe-hub-dsh:8099"
    dsh_token: str = ""
    dsh_timeout: int = 55
    native_timeout: int = 60
    native_fallback: bool = True
    rewrite_enabled: bool = True
    tool_allowlist: tuple = (
        "web_search_tavily",
        "tavily_extract_web_page",
        "query_hltv",
    )
    proactive_adapter: bool = True
    meme_probability: int = 15
    meme_directory: str = ""
    max_concurrent: int = 2
    extra_persona: str = ""

    @classmethod
    def read(cls, raw):
        values = {}
        for f in fields(cls):
            default = f.default
            value = raw.get(f.name, default)
            if isinstance(default, bool):
                value = value if isinstance(value, bool) else default
            elif isinstance(default, int):
                value = (
                    value
                    if isinstance(value, int) and not isinstance(value, bool)
                    else default
                )
            elif isinstance(default, tuple):
                value = (
                    tuple(str(v) for v in value)
                    if isinstance(value, (list, tuple))
                    else default
                )
            else:
                value = str(value) if value is not None else default
            values[f.name] = value
        bounds = {
            "tsundere_level": (1, 3),
            "initial_affinity": (0, 100),
            "daily_gain": (0, 20),
            "gain_cooldown": (60, 86400),
            "anger_seconds": (10, 600),
            "anger_replies": (1, 4),
            "trigger_cooldown": (10, 600),
            "max_chars": (30, 100),
            "history_turns": (0, 16),
            "retention_days": (1, 30),
            "dsh_timeout": (10, 120),
            "native_timeout": (10, 120),
            "meme_probability": (0, 100),
            "max_concurrent": (1, 4),
        }
        bounds.update(
            group_history_messages=(0, 100),
            context_chars=(2000, 30000),
            topic_ttl=(60, 3600),
        )
        for key, (lo, hi) in bounds.items():
            values[key] = min(hi, max(lo, values[key]))
        if values["engine"] not in ("native", "dsh"):
            values["engine"] = "native"
        values["bot_name"] = values["bot_name"].strip()[:20] or "菲比"
        values["persona_id"] = values["persona_id"].strip()[:80] or "phoebe_tsundere"
        return cls(**values)

    def manages(self, umo):
        return self.enabled and (
            not self.allowed_sessions or umo in self.allowed_sessions
        )
