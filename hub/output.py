"""A deterministic final gate. Drafts never pass the gate on exceptions."""

import re
from dataclasses import dataclass


LEGACY_FALLBACK = "这句没组织好，先让我捋一下。你可以把最想问的那一点再说一遍。"
FALLBACK = "这次回答整理失败了，问题我已经收到了。"
URL = re.compile(r"https?://[^\s<>\[\]“”\"，。！？]+")
TAG = re.compile(r"&&[^&\n]{1,40}&&|\[meme:[^\]\n]+\]", re.I)


@dataclass(frozen=True)
class ShortReply:
    body: str
    url: str = ""
    outcome: str = "original"
    reason: str = ""

    @property
    def text(self):
        return self.body + ("\n" + self.url if self.url else "")


def sentences(text):
    # Decimal points / abbreviations stay intact. A final punctuation-free line is one sentence.
    return [
        s.strip()
        for s in re.findall(
            r".*?(?:[。！？!?]+[’”\"]*|(?<!\d)\.(?!\d)(?:\s+|$)|\n+|$)", text, re.S
        )
        if s.strip()
    ]


def prepare(text):
    text = str(text or "")[:128000]
    text = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", text, flags=re.I | re.S)
    # An unterminated reasoning tag must not leak the rest of the draft.
    text = re.sub(r"<think(?:ing)?>.*$", "", text, flags=re.I | re.S)
    text = text[:16000]
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
    return (
        bool(reply.body)
        and len(reply.body) <= max_chars
        and 1 <= len(sentences(reply.body)) <= 2
    )


def fallback(max_chars=100):
    return ShortReply(
        FALLBACK if len(FALLBACK) <= max_chars else "这次回答整理失败了。"
    )


def invalid_reason(reply, max_chars):
    if not reply.body:
        return "empty"
    if reply.body == LEGACY_FALLBACK:
        return "model_fallback"
    if len(reply.body) > max_chars:
        return "too_long"
    if len(sentences(reply.body)) > 2:
        return "too_many_sentences"
    return ""


async def enforce(text, max_chars=100, rewrite=None, fallback_reply=None):
    reply = prepare(text)
    reason = invalid_reason(reply, max_chars)
    if not reason:
        return reply
    # Joining layout-only newlines keeps every word and correction intact. Never
    # select only the first two sentences or cut a claim to fit the limit.
    compact = ShortReply(" ".join(reply.body.splitlines()), reply.url)
    if not invalid_reason(compact, max_chars):
        return ShortReply(compact.body, reply.url, "compacted", reason)
    if rewrite:
        try:
            candidate = prepare(await rewrite(reply.body))
            # Rewriter cannot add new source URLs.
            candidate = ShortReply(" ".join(candidate.body.splitlines()), reply.url)
            rejected = invalid_reason(candidate, max_chars)
            if not rejected:
                return ShortReply(candidate.body, reply.url, "rewritten", reason)
            reason += ":rewrite_" + rejected
        except TimeoutError:
            reason += ":rewrite_timeout"
        except Exception as exc:
            reason += ":rewrite_error_" + type(exc).__name__
    else:
        reason += ":rewrite_disabled"
    # Do not truncate a claim mid-sentence or discard its correction in later sentences.
    # Failed compression uses an honest short error instead of a misleading partial answer.
    failed = (
        fallback_reply
        if fallback_reply and valid(fallback_reply, max_chars)
        else fallback(max_chars)
    )
    return ShortReply(failed.body, failed.url, "fallback", reason)
