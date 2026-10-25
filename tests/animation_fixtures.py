"""Shared helpers of the animation tests (no binary fixtures are committed)."""

from __future__ import annotations

import copy
import json
import random
import struct
import zlib
from pathlib import Path
from typing import Any

from ai_youtube_agent.animation.storyboard import (
    Storyboard,
    load_storyboard,
    text_tokens,
)
from ai_youtube_agent.animation.subtitles import WordTiming
from ai_youtube_agent.animation.tts import SpokenLine

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = ROOT / "tools" / "storyboards" / "quan_an_nho_shorts.json"
LEAD_MS = 30


def shipped_dict() -> dict[str, Any]:
    """A fresh, mutable copy of the shipped storyboard JSON."""
    return copy.deepcopy(json.loads(SHIPPED.read_text(encoding="utf-8")))


def shipped_storyboard() -> Storyboard:
    return load_storyboard(SHIPPED)


def scene_of(data: dict[str, Any], scene_id: str) -> dict[str, Any]:
    return next(scene for scene in data["scenes"] if scene["id"] == scene_id)


def fake_spoken(
    storyboard: Storyboard, *, ms_per_word: int = 300
) -> dict[str, SpokenLine]:
    """A spoken line for every storyboard line with evenly timed words."""
    spoken: dict[str, SpokenLine] = {}
    for line in storyboard.all_lines():
        tokens = text_tokens(line.text)
        words = tuple(
            WordTiming(
                index,
                token,
                LEAD_MS + index * ms_per_word,
                LEAD_MS + index * ms_per_word + ms_per_word - 30,
            )
            for index, token in enumerate(tokens)
        )
        voice = storyboard.voice(line.voice)
        spoken[line.id] = SpokenLine(
            line_id=line.id,
            text=line.text,
            voice_id=voice.voice_id,
            wav_path=Path(f"{line.id}.wav"),
            duration_ms=words[-1].end_ms + LEAD_MS,
            words=words,
            timing_source="word_boundary",
            rate=voice.rate,
            pitch=voice.pitch,
            syllables_per_second=1000 / ms_per_word,
        )
    return spoken


_PNG_CACHE: dict[tuple[int, int, bool], bytes] = {}


def make_plate_png(width: int, height: int, *, blank: bool = False) -> bytes:
    """A complete RGB PNG: random noise, or one flat colour when ``blank``."""
    key = (width, height, blank)
    if key in _PNG_CACHE:
        return _PNG_CACHE[key]
    rng = random.Random(7)
    rows = []
    for _ in range(height):
        row = bytes([200, 180, 150]) * width if blank else rng.randbytes(width * 3)
        rows.append(b"\x00" + row)
    raw = zlib.compress(b"".join(rows), 1)

    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(body, zlib.crc32(kind)) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", raw)
        + chunk(b"IEND", b"")
    )
    _PNG_CACHE[key] = png
    return png
