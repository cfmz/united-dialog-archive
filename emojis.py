"""Premium-эмодзи: pe(text) оборачивает эмодзи в <tg-emoji> для последующего парсинга."""
from __future__ import annotations
import re

EMOJI_IDS: dict[str, str] = {
    "🚀": "5372917041193828849",
    "🔥": "5289722755871162900",
    "🗑": "5258130763148172425",
    "📸": "5258012149036365477",
    "📷": "5944753741512052670",
    "🆕": "5904348939754147741",
    "😇": "5341350410252723241",
    "👤": "5346136537123801643",
    "✏️": "5879841310902324730",
    "💬": "5345811158991389982",
    "⚪️": "5085049546828940221",
    "😎": "5289650686319929628",
    "🤖": "5188678912883827293",
    "🔔": "5346038654819123712",
    "📈": "5345901121376371010",
    "💰": "5348392971207194994",
    "❤️": "5258179403652801593",
    "💎": "5372917041193828849",
    "👥": "5346136537123801643",
    "🎧": "5346136537123801643",
}

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
