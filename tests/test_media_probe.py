"""Tests for the stdlib image and MP3 probes (PROV-FIX-01)."""

from __future__ import annotations

import struct

import pytest

from ai_youtube_agent.providers.media_probe import (
    MediaProbeError,
    probe_image_bytes,
    probe_mp3_bytes,
)
from media_fixtures import (
    make_jpeg_header,
    make_mp3,
    make_mp3_with_id3,
    make_png,
    make_webp_vp8x,
)

# -----------------------------------------------------------------------------
# Images
# -----------------------------------------------------------------------------


def test_probe_png_reports_decoded_dimensions() -> None:
    probe = probe_image_bytes(make_png(640, 704))
    assert (probe.media_type, probe.width, probe.height) == ("image/png", 640, 704)


def test_probe_jpeg_reports_dimensions_from_sof() -> None:
    probe = probe_image_bytes(make_jpeg_header(1080, 1920))
    assert (probe.media_type, probe.width, probe.height) == (
        "image/jpeg",
        1080,
        1920,
    )


def test_probe_jpeg_tolerates_trailing_zero_bytes() -> None:
    probe = probe_image_bytes(make_jpeg_header(800, 600) + b"\x00\x00")
    assert (probe.width, probe.height) == (800, 600)


def test_probe_webp_vp8x_reports_dimensions() -> None:
    probe = probe_image_bytes(make_webp_vp8x(1920, 1080))
    assert (probe.media_type, probe.width, probe.height) == (
        "image/webp",
        1920,
        1080,
    )


def test_probe_webp_lossy_and_lossless_headers() -> None:
    vp8 = b"VP8 " + struct.pack("<I", 10) + b"\x00\x00\x00\x9d\x01\x2a"
    vp8 += struct.pack("<HH", 640, 480)
    body = b"WEBP" + vp8
    lossy = b"RIFF" + struct.pack("<I", len(body)) + body
    assert (probe_image_bytes(lossy).width, probe_image_bytes(lossy).height) == (
        640,
        480,
    )

    bits = (700 - 1) | ((500 - 1) << 14)
    vp8l = b"VP8L" + struct.pack("<I", 5) + b"\x2f" + struct.pack("<I", bits)
    body = b"WEBP" + vp8l + b"\x00"  # chunk padded to an even size
    lossless = b"RIFF" + struct.pack("<I", len(body)) + body
    probe = probe_image_bytes(lossless)
    assert (probe.width, probe.height) == (700, 500)


def test_truncated_png_is_rejected() -> None:
    png = make_png(640, 640)
    with pytest.raises(MediaProbeError):
        probe_image_bytes(png[:-20])
    with pytest.raises(MediaProbeError):
        probe_image_bytes(png[:30])


def test_png_with_corrupt_chunk_checksum_is_rejected() -> None:
    png = bytearray(make_png(640, 640))
    png[20] ^= 0xFF  # inside the IHDR body
    with pytest.raises(MediaProbeError):
        probe_image_bytes(bytes(png))


def test_jpeg_without_end_marker_is_rejected() -> None:
    with pytest.raises(MediaProbeError):
        probe_image_bytes(make_jpeg_header(800, 600)[:-2])


def test_webp_with_wrong_riff_size_is_rejected() -> None:
    webp = bytearray(make_webp_vp8x(800, 600))
    webp[4:8] = struct.pack("<I", len(webp))  # should be len - 8
    with pytest.raises(MediaProbeError):
        probe_image_bytes(bytes(webp))


@pytest.mark.parametrize(
    "data",
    [b"", b"not an image at all", bytes(range(64)), b"\x89PNG\r\n\x1a\n"],
    ids=["empty", "text", "random", "signature-only"],
)
def test_empty_or_unknown_image_bytes_are_rejected(data: bytes) -> None:
    with pytest.raises(MediaProbeError):
        probe_image_bytes(data)


def test_absurd_image_dimensions_are_rejected() -> None:
    with pytest.raises(MediaProbeError):
        probe_image_bytes(make_jpeg_header(20_000, 600))
    with pytest.raises(MediaProbeError):
        probe_image_bytes(make_jpeg_header(0, 600))


# -----------------------------------------------------------------------------
# MP3
# -----------------------------------------------------------------------------


def test_probe_mp3_duration_from_frames() -> None:
    probe = probe_mp3_bytes(make_mp3(40))
    assert probe.duration_ms == 960
    assert probe.frames == 40
    assert probe.sample_rate == 24_000
    assert probe.channels == 1
    assert probe.bitrate_kbps == 48


def test_probe_mp3_skips_id3v2_prefix_and_id3v1_trailer() -> None:
    assert probe_mp3_bytes(make_mp3_with_id3(40)).duration_ms == 960
    assert probe_mp3_bytes(make_mp3_with_id3(40, v1=False)).duration_ms == 960
    assert probe_mp3_bytes(make_mp3_with_id3(40, v2=False)).duration_ms == 960


def test_probe_mp3_with_truncated_last_frame_is_rejected() -> None:
    with pytest.raises(MediaProbeError):
        probe_mp3_bytes(make_mp3(40)[:-10])


def test_probe_mp3_with_garbage_in_the_middle_is_rejected() -> None:
    data = bytearray(make_mp3(40))
    data[144 * 10 : 144 * 10 + 4] = b"\x00\x01\x02\x03"
    with pytest.raises(MediaProbeError):
        probe_mp3_bytes(bytes(data))


@pytest.mark.parametrize(
    "data",
    [b"", b"hello world, not audio", bytes(range(1, 256))],
    ids=["empty", "text", "random"],
)
def test_empty_or_non_mp3_bytes_are_rejected(data: bytes) -> None:
    with pytest.raises(MediaProbeError):
        probe_mp3_bytes(data)
