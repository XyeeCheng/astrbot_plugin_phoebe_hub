"""Explicit adapters for known plugin interfaces; no AstrBot core monkey patches."""
import functools
import inspect
import random
from pathlib import Path

from .output import enforce
from .persona import persona


class Adapters:
    def __init__(self, plugin):
        self.plugin = plugin
        self.patches = []
        self.proactive_status = "not_found"
        self.meme_status = "not_found"

    def stars(self):
        get = getattr(self.plugin.context, "get_all_stars", None)
        return get() if get else []

    def install(self):
        if not self.plugin.settings.proactive_adapter:
            self.proactive_status = "disabled"
            return
        stars = list(self.stars())
        live = {id(getattr(m, "star_cls", None)) for m in stars if getattr(m, "activated", False)}
        retained = []
        for record in self.patches:
            if id(record[0]) not in live:
                self._restore(record)
            else:
                retained.append(record)
        self.patches = retained
        self.proactive_status = "not_found"
        for meta in stars:
            obj = getattr(meta, "star_cls", None)
            if not getattr(meta, "activated", False) or not obj:
                continue
            if meta.name != "astrbot_plugin_proactive_chat":
                continue
            if any(target is obj for target, *_ in self.patches):
                self.proactive_status = "active"
                continue
            generate = getattr(obj, "_generate_llm_response", None)
            send = getattr(obj, "_send_proactive_message", None)
            if not inspect.iscoroutinefunction(generate) or not inspect.iscoroutinefunction(send):
                self.proactive_status = "incompatible"
                continue
            expected = ["session_id", "session_config", "history_messages", "system_prompt", "unanswered_count"]
            if list(inspect.signature(generate).parameters) != expected or list(inspect.signature(send).parameters) != ["session_id", "text"]:
                self.proactive_status = "incompatible"
                continue
            plugin = self.plugin

            @functools.wraps(generate)
            async def wrapped_generate(session_id, session_config, history_messages, system_prompt, unanswered_count, _original=generate):
                if plugin.stopping:
                    return None, ""
                if not plugin.settings.manages(session_id):
                    return await _original(session_id, session_config, history_messages, system_prompt, unanswered_count)
                system_prompt = persona(plugin.settings, {"score": 20, "mood": "normal"}, proactive=True)
                text, prompt = await _original(session_id, session_config, history_messages, system_prompt, unanswered_count)
                if not text:
                    return text, prompt
                reply = await plugin.shorten(text, session_id)
                if plugin.stopping:
                    return None, prompt
                return reply.text, prompt

            @functools.wraps(send)
            async def wrapped_send(session_id, text, _original=send):
                if plugin.stopping:
                    return
                if not plugin.settings.manages(session_id):
                    return await _original(session_id, text)
                # A single text chain; no TTS/segmentation can leak the unshortened draft.
                from astrbot.api.event import MessageChain
                reply = await enforce(text, plugin.settings.max_chars)
                await plugin.context.send_message(session_id, MessageChain().message(reply.text))

            self._patch(obj, "_generate_llm_response", wrapped_generate)
            self._patch(obj, "_send_proactive_message", wrapped_send)
            self.proactive_status = "active"

    def _patch(self, obj, name, wrapper):
        original = getattr(obj, name)
        self.patches.append((obj, name, original, wrapper, name in obj.__dict__))
        setattr(obj, name, wrapper)

    def close(self):
        for record in reversed(self.patches):
            self._restore(record)
        self.patches.clear()

    @staticmethod
    def _restore(record):
        obj, name, original, wrapper, owned = record
        if getattr(obj, name, None) is wrapper:
            if owned:
                setattr(obj, name, original)
            else:
                delattr(obj, name)

    def meme(self, event, mood):
        s = self.plugin.settings
        if random.randrange(100) >= s.meme_probability or mood == "serious":
            return None
        directory = s.meme_directory
        if not directory:
            for meta in self.stars():
                obj = getattr(meta, "star_cls", None)
                if meta.name == "meme_manager" and getattr(meta, "activated", False) and obj:
                    resolver = getattr(obj, "_get_runtime_memes_dir_for_event", None)
                    if callable(resolver):
                        try:
                            directory = resolver(event)
                        except Exception:
                            self.meme_status = "incompatible"
                        break
        if not directory:
            return None
        root = Path(directory).resolve()
        if not root.is_dir():
            self.meme_status = "missing_directory"
            return None
        # Category names are local pack labels, never arbitrary model-generated paths.
        labels = ("angry", "生气", "愤怒", "炸毛") if mood == "angry" else ("happy", "开心", "傲娇", "得意", "shy")
        choices = []
        for label in labels:
            folder = root / label
            if not folder.is_dir() or folder.is_symlink():
                continue
            for path in folder.iterdir():
                if (path.is_file() and not path.is_symlink() and path.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp")
                        and path.stat().st_size <= 8 * 1024 * 1024 and path.resolve().is_relative_to(root)):
                    choices.append(path)
        self.meme_status = "active" if choices else "no_matching_category"
        return random.choice(choices) if choices else None
