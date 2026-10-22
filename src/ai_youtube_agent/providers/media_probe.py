"""Stdlib-only probes that read real media bytes (PROV-FIX-01).

Real providers return bytes that must be checked before they are stored:

- ``probe_image_bytes`` recognises PNG, JPEG and WebP by magic number, reads the
  decoded width and height from the container header and rejects truncated
  files (a PNG without its ``IEND`` chunk, a JPEG without ``EOI``, a WebP whose
  RIFF size does not match).
- ``probe_mp3_bytes`` walks the MPEG Layer III frames (after an optional ID3v2
  tag, before an optional ID3v1 trailer) and returns the real duration. A broken
  or truncated frame chain is corrupt audio.

Both raise ``MediaProbeError`` (a ``ValueError``) with a short reason that never
contains the media bytes. Pillow is not a dependency, so nothing here decodes
pixels or samples.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from typing import Final

MAX_IMAGE_BYTES: Final[int] = 64 * 1024 * 1024
MAX_IMAGE_DIMENSION: Final[int] = 16_384

_PNG_SIGNATURE: Final[bytes] = b"\x89PNG\r\n\x1a\n"
_JPEG_STANDALONE_MARKERS: Final[frozenset[int]] = frozenset(
    {0x01, 0xD8, *range(0xD0, 0xD8)}
)
_JPEG_NON_SOF_C_MARKERS: Final[frozenset[int]] = frozenset({0xC4, 0xC8, 0xCC})

# MPEG audio Layer III tables: index 1..14 are valid bitrate indexes.
_MPEG1_BITRATES_KBPS: Final[tuple[int, ...]] = (
    0,
    32,
    40,
    48,
    56,
    64,
    80,
    96,
    112,
    128,
    160,
    192,
    224,
    256,
    320,
)
_MPEG2_BITRATES_KBPS: Final[tuple[int, ...]] = (
    0,
    8,
    16,
    24,
    32,
    40,
    48,
    56,
    64,
    80,
    96,
    112,
    128,
    144,
    160,
)
_SAMPLE_RATES: Final[dict[int, tuple[int, int, int]]] = {
    3: (44_100, 48_000, 32_000),  # MPEG1
    2: (22_050, 24_000, 16_000),  # MPEG2
    0: (11_025, 12_000, 8_000),  # MPEG2.5
}
_ID3V1_SIZE: Final[int] = 128


class MediaProbeError(ValueError):
    """The bytes are not a complete, well-formed image or MP3 file."""


@dataclass(frozen=True)
class ImageProbe:
    media_type: str
    width: int
    height: int


@dataclass(frozen=True)
class Mp3Probe:
    duration_ms: int
    sample_rate: int
    channels: int
    frames: int
    bitrate_kbps: int


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


def probe_image_bytes(data: bytes) -> ImageProbe:
    """Return the media type and decoded size of a complete PNG, JPEG or WebP."""
    if not isinstance(data, bytes | bytearray) or not data:
        raise MediaProbeError("image data is empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise MediaProbeError("image data is larger than 64 MiB")
    data = bytes(data)
    if data.startswith(_PNG_SIGNATURE):
        media_type, width, height = "image/png", *_probe_png(data)
    elif data.startswith(b"\xff\xd8"):
        media_type, width, height = "image/jpeg", *_probe_jpeg(data)
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        media_type, width, height = "image/webp", *_probe_webp(data)
    else:
        raise MediaProbeError("image format is not PNG, JPEG or WebP")
    if not (1 <= width <= MAX_IMAGE_DIMENSION and 1 <= height <= MAX_IMAGE_DIMENSION):
        raise MediaProbeError(f"image size {width}x{height} is out of range")
    return ImageProbe(media_type=media_type, width=width, height=height)


def _probe_png(data: bytes) -> tuple[int, int]:
    pos = len(_PNG_SIGNATURE)
    width = height = 0
    first = True
    while True:
        if pos + 12 > len(data):
            raise MediaProbeError("PNG is truncated before its IEND chunk")
        (length,) = struct.unpack_from(">I", data, pos)
        chunk_type = data[pos + 4 : pos + 8]
        body_end = pos + 8 + length
        if body_end + 4 > len(data):
            raise MediaProbeError("PNG is truncated inside a chunk")
        body = data[pos + 8 : body_end]
        (crc,) = struct.unpack_from(">I", data, body_end)
        if zlib.crc32(body, zlib.crc32(chunk_type)) & 0xFFFFFFFF != crc:
            raise MediaProbeError("PNG chunk checksum does not match")
        if first:
            if chunk_type != b"IHDR" or length != 13:
                raise MediaProbeError("PNG does not start with an IHDR chunk")
            width, height = struct.unpack_from(">II", body)
            first = False
        pos = body_end + 4
        if chunk_type == b"IEND":
            break
    if pos != len(data):
        raise MediaProbeError("PNG has data after its IEND chunk")
    return width, height


def _probe_jpeg(data: bytes) -> tuple[int, int]:
    if not data.rstrip(b"\x00").endswith(b"\xff\xd9"):
        raise MediaProbeError("JPEG is truncated (no end-of-image marker)")
    pos = 2
    size = len(data)
    while pos < size:
        if data[pos] != 0xFF:
            raise MediaProbeError("JPEG marker segment is malformed")
        while pos < size and data[pos] == 0xFF:
            pos += 1
        if pos >= size:
            break
        marker = data[pos]
        pos += 1
        if marker in _JPEG_STANDALONE_MARKERS:
            continue
        if marker == 0xD9:
            break
        if pos + 2 > size:
            raise MediaProbeError("JPEG is truncated inside a segment")
        (length,) = struct.unpack_from(">H", data, pos)
        if length < 2 or pos + length > size:
            raise MediaProbeError("JPEG segment length is invalid")
        is_sof = 0xC0 <= marker <= 0xCF and marker not in _JPEG_NON_SOF_C_MARKERS
        if is_sof:
            if length < 7:
                raise MediaProbeError("JPEG frame header is too short")
            height, width = struct.unpack_from(">HH", data, pos + 3)
            return width, height
        if marker == 0xDA:
            break
        pos += length
    raise MediaProbeError("JPEG has no frame header")


def _probe_webp(data: bytes) -> tuple[int, int]:
    (riff_size,) = struct.unpack_from("<I", data, 4)
    if riff_size != len(data) - 8:
        raise MediaProbeError("WebP RIFF size does not match the data length")
    fourcc = data[12:16]
    needed = {b"VP8 ": 30, b"VP8L": 25, b"VP8X": 30}.get(fourcc)
    if needed is None:
        raise MediaProbeError("WebP chunk type is not VP8, VP8L or VP8X")
    if len(data) < needed:
        raise MediaProbeError("WebP is too short")
    if fourcc == b"VP8 ":
        if data[23:26] != b"\x9d\x01\x2a":
            raise MediaProbeError("WebP VP8 start code is missing")
        width, height = struct.unpack_from("<HH", data, 26)
        return width & 0x3FFF, height & 0x3FFF
    if fourcc == b"VP8L":
        if data[20] != 0x2F:
            raise MediaProbeError("WebP VP8L signature is missing")
        (bits,) = struct.unpack_from("<I", data, 21)
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    width = int.from_bytes(data[24:27], "little") + 1
    height = int.from_bytes(data[27:30], "little") + 1
    return width, height


# ---------------------------------------------------------------------------
# MP3
# ---------------------------------------------------------------------------


def probe_mp3_bytes(data: bytes) -> Mp3Probe:
    """Return the duration and stream facts of a complete MPEG Layer III file."""
    if not isinstance(data, bytes | bytearray) or not data:
        raise MediaProbeError("audio data is empty")
    data = bytes(data)
    size = len(data)
    pos = _skip_id3v2(data)
    first_rate = 0
    first_version = -1
    channels = 0
    bitrate_kbps = 0
    frames = 0
    total_samples = 0
    while pos < size:
        if size - pos == _ID3V1_SIZE and data[pos : pos + 3] == b"TAG":
            break
        header = _parse_mp3_header(data, pos)
        if header is None:
            raise MediaProbeError(f"MP3 frame chain is broken at byte {pos}")
        version, sample_rate, bitrate, padding, mono = header
        if frames == 0:
            first_rate, first_version = sample_rate, version
            channels = 1 if mono else 2
            bitrate_kbps = bitrate
        elif sample_rate != first_rate or version != first_version:
            raise MediaProbeError("MP3 changes its sample rate inside the stream")
        coefficient = 144 if version == 3 else 72
        frame_length = coefficient * bitrate * 1000 // sample_rate + padding
        if pos + frame_length > size:
            raise MediaProbeError("MP3 is truncated inside a frame")
        total_samples += 1152 if version == 3 else 576
        frames += 1
        pos += frame_length
    if frames == 0:
        raise MediaProbeError("MP3 has no audio frames")
    duration_ms = max(1, round(total_samples * 1000 / first_rate))
    return Mp3Probe(
        duration_ms=duration_ms,
        sample_rate=first_rate,
        channels=channels,
        frames=frames,
        bitrate_kbps=bitrate_kbps,
    )


def _skip_id3v2(data: bytes) -> int:
    if len(data) >= 10 and data[:3] == b"ID3":
        size_bytes = data[6:10]
        if any(byte & 0x80 for byte in size_bytes):
            raise MediaProbeError("ID3v2 tag size is malformed")
        tag_size = (
            (size_bytes[0] << 21)
            | (size_bytes[1] << 14)
            | (size_bytes[2] << 7)
            | size_bytes[3]
        )
        footer = 10 if data[5] & 0x10 else 0
        end = 10 + tag_size + footer
        if end > len(data):
            raise MediaProbeError("ID3v2 tag is truncated")
        return end
    return 0


def _parse_mp3_header(data: bytes, pos: int) -> tuple[int, int, int, int, bool] | None:
    """Return (version, sample_rate, bitrate_kbps, padding, mono) or None."""
    if pos + 4 > len(data):
        return None
    b0, b1, b2, b3 = data[pos : pos + 4]
    if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
        return None
    version = (b1 >> 3) & 0x03
    layer = (b1 >> 1) & 0x03
    if version == 1 or layer != 1:  # reserved version, or not Layer III
        return None
    bitrate_index = b2 >> 4
    rate_index = (b2 >> 2) & 0x03
    if bitrate_index in (0, 15) or rate_index == 3:
        return None
    table = _MPEG1_BITRATES_KBPS if version == 3 else _MPEG2_BITRATES_KBPS
    sample_rate = _SAMPLE_RATES[version][rate_index]
    return (
        version,
        sample_rate,
        table[bitrate_index],
        (b2 >> 1) & 0x01,
        (b3 >> 6) == 3,
    )
