"""Programmatic media bytes for provider tests (no binary fixtures are committed)."""

from __future__ import annotations

import struct

from ai_youtube_agent.providers.image_generation import ImageGenerationRequest
from ai_youtube_agent.providers.mock_image_generation import synthesize_scene_png

# MPEG2 Layer III, 48 kbps, 24 kHz, mono: 144 bytes and 24 ms per frame.
MP3_FRAME_HEADER = bytes([0xFF, 0xF3, 0x64, 0xC0])
MP3_FRAME_BYTES = 144
MP3_FRAME_MS = 24


def make_png(width: int, height: int) -> bytes:
    """A complete deterministic PNG of ``width`` x ``height`` (each >= 640)."""
    request = ImageGenerationRequest(
        scene_id="scene_media",
        shot_id="shot_media",
        prompt="media fixture",
        width=width,
        height=height,
    )
    return synthesize_scene_png(request)


def make_jpeg_header(width: int, height: int) -> bytes:
    """SOI, a baseline SOF0 header and EOI: enough for the dimension probe."""
    sof = struct.pack(">BHHB", 8, height, width, 1) + bytes([1, 0x11, 0])
    return (
        b"\xff\xd8" + b"\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof + b"\xff\xd9"
    )


def make_webp_vp8x(width: int, height: int) -> bytes:
    """A RIFF/WebP file with a VP8X chunk (extended format header only)."""
    payload = (
        struct.pack("<I", 0)
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little")
    )
    chunk = b"VP8X" + struct.pack("<I", len(payload)) + payload
    body = b"WEBP" + chunk
    return b"RIFF" + struct.pack("<I", len(body)) + body


def make_mp3(frames: int = 40) -> bytes:
    """``frames`` valid MPEG2 Layer III frames (24 ms each)."""
    frame = MP3_FRAME_HEADER + bytes(MP3_FRAME_BYTES - len(MP3_FRAME_HEADER))
    return frame * frames


def make_mp3_with_id3(frames: int = 40, *, v2: bool = True, v1: bool = True) -> bytes:
    """``make_mp3`` with an optional ID3v2 prefix and ID3v1 trailer."""
    prefix = b""
    if v2:
        tag_body = bytes(300)
        size = len(tag_body)
        syncsafe = bytes(
            [(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F]
        )
        prefix = b"ID3\x04\x00\x00" + syncsafe + tag_body
    trailer = b"TAG" + bytes(125) if v1 else b""
    return prefix + make_mp3(frames) + trailer
