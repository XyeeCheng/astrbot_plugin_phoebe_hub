"""A deterministic final gate. Drafts never pass the gate on exceptions."""
import re
from dataclasses import dataclass


FALLBACK = "这句没组织好，先让我捋一下。你可以把最想问的那一点再说一遍。"
URL = re.compile(r"https?://[^\s<>\[\]“”\"，。！？]+")
TAG = re.compile(r"&&[^&\n]{1,40}&&|\[meme:[^\]\n]+\]", re.I)


@dataclass(frozen=True)
class ShortReply:
    body: str
    url: str = ""

    @property
    def text(self):
        return self.body + ("\n" + self.url if self.url else "")


def sentences(text):
    # Decimal points / abbreviations stay intact. A final punctuation-free line is one sentence.
    return [s.strip() for s in re.findall(r".*?(?:[。！？!?]+[’”\"]*|(?<!\d)\.(?!\d)(?:\s+|$)|\n+|$)", text, re.S)
            if s.strip()]


def prepare(text):
    text = str(text or "")[:16000]
    text = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", text, flags=re.I | re.S)
    # An unterminated reasoning tag must not leak the rest of the draft.
    text = re.sub(r"<think(?:ing)?>.*$", "", text, flags=re.I | re.S)
    text = re.sub(r"```.*?(?:```|$)", "", text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r"\1 \2", text)
    urls = URL.findall(text)
    text = URL.sub("", text)
    text = TAG.sub("", text)
    text = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]", "", text)
    text = re.sub(r"(?m)^\s*(?:#{1,6}\s*|[-*•]\s+|\d+[.)、]\s*)", "", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = re.sub(r"[ \t]+", " ", text).strip()
    url = next((u.rstrip(").,;!?:") for u in urls if len(u) <= 600), "")
    return ShortReply(text, url)


def valid(reply, max_chars=100):
    return bool(reply.body) and len(reply.body) <= max_chars and 1 <= len(sentences(reply.body)) <= 2


def fallback(max_chars=100):
    return ShortReply(FALLBACK if len(FALLBACK) <= max_chars else "这句没组织好，你把最想问的那一点再说一遍。")


async def enforce(text, max_chars=100, rewrite=None):
    reply = prepare(text)
    if valid(reply, max_chars):
        return reply
    if rewrite:
        try:
            candidate = prepare(await rewrite(reply.body))
            # Rewriter cannot add new source URLs.
            candidate = ShortReply(candidate.body, reply.url)
            if valid(candidate, max_chars):
                return candidate
        except Exception:
            pass
    # Do not truncate a claim mid-sentence or discard its correction in later sentences.
    # Failed compression uses an honest short error instead of a misleading partial answer.
    return fallback(max_chars)
