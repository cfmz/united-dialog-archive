"""Premium-эмодзи: pe(text) подменяет обычные эмодзи на <tg-emoji>."""
from __future__ import annotations
import re

EMOJI_IDS: dict[str, str] = {}

_TAG_RE = re.compile(r"(<tg-emoji\b[^>]*>.*?</tg-emoji>)", re.DOTALL)


def pe(text: str) -> str:
    if not EMOJI_IDS or not text:
        return text
    parts = _TAG_RE.split(text)
    for i in range(0, len(parts), 2):
        chunk = parts[i]
        for ch, eid in EMOJI_IDS.items():
            if ch in chunk:
                chunk = chunk.replace(ch, f'<tg-emoji emoji-id="{eid}">{ch}</tg-emoji>')
        parts[i] = chunk
    return "".join(parts)
