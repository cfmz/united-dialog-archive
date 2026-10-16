"""Premium-эмодзи: pe(text) подменяет обычные эмодзи на <tg-emoji>."""
from __future__ import annotations
import re

EMOJI_IDS: dict[str, str] = {
    "🤯": "5456384164313966322",
    "🥳": "5458806031947671561",
    "🥸": "5332801398109120907",
    "😎": "5289650686319929628",
    "🧐": "5402461597237004802",
    "🫤": "5344061947660748948",
    "☹️": "5436062865855359364",
    "😮": "5220081566268536865",
    "😳": "5438239044179863743",
    "🥺": "5321259601214062502",
    "🥹": "5404737650731007282",
    "😢": "5456218262612223748",
    "😭": "5458490510765206019",
    "😱": "5449701482964198097",
    "😞": "5404808023770152011",
    "🥱": "5404723975555138856",
    "😤": "5379725114113805128",
    "😡": "5314766512605634930",
    "😠": "5334549028891796220",
    "🤬": "5220139887629453156",
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
