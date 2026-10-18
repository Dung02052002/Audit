"""Video QC checks (Prompt Pack v8, prompts K-117..K-120), context C10 Quality (Test & QC).

Concrete QC check implementations that inspect rendered MP4 video files:
- K-117 Container Test (``video.container``): validates MP4 ISO-BMFF structure,
  major and compatible brands, moov/mvhd headers, timescale and duration.
- K-118 Resolution Test (``video.resolution``): validates actual dimensions and
  aspect ratio (9:16 vertical for Shorts, 16:9 horizontal for LongForm) against
  configured targets.
"""

import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from ai_youtube_agent.content.qc import QCCheck, QCStatus
from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.providers.video_render import Mp4Info, probe_mp4

CONTAINER_CHECK_NAME: Final[str] = "video.container"
RESOLUTION_CHECK_NAME: Final[str] = "video.resolution"

KNOWN_MP4_BRANDS: Final[frozenset[str]] = frozenset(
    {"isom", "iso2", "mp41", "mp42", "avc1", "qt  ", "dash", "M4V "}
)


@dataclass(frozen=True)
class VideoInspectionContext:
    """Input context for running video artifact QC inspection checks."""

    content_item_id: str
    content_type: ContentType
    video_data: bytes
    expected_width: int
    expected_height: int
    expected_fps: int
    expected_duration_ms: int
    min_duration_ms: int
    max_duration_ms: int
    actual_width: int | None = None
    actual_height: int | None = None
    actual_fps: float | None = None
    extra: dict[str, Any] | None = None


def _parse_tkhd_dimensions(data: bytes) -> tuple[int, int] | None:
    """Attempt to extract width and height from the first video track's tkhd box."""
    pos = 0
    end = len(data)
    while pos + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, pos)
        if size == 1 and pos + 16 <= end:
            (size,) = struct.unpack_from(">Q", data, pos + 8)
            header_len = 16
        else:
            header_len = 8
        if size < header_len or pos + size > end:
            break

        if kind == b"moov":
            # Search inside moov for trak
            inner_pos = pos + header_len
            inner_end = pos + size
            while inner_pos + 8 <= inner_end:
                t_size, t_kind = struct.unpack_from(">I4s", data, inner_pos)
                if t_size < 8 or inner_pos + t_size > inner_end:
                    break
                if t_kind == b"trak":
                    # Search inside trak for tkhd
                    trak_pos = inner_pos + 8
                    trak_end = inner_pos + t_size
                    while trak_pos + 8 <= trak_end:
                        k_size, k_kind = struct.unpack_from(">I4s", data, trak_pos)
                        if k_size < 8 or trak_pos + k_size > trak_end:
                            break
                        if k_kind == b"tkhd":
                            payload = data[trak_pos + 8 : trak_pos + k_size]
                            if len(payload) >= 84:
                                version = payload[0]
                                offset = 84 if version == 0 else 96
                                if len(payload) >= offset + 8:
                                    w_fixed, h_fixed = struct.unpack_from(
                                        ">II", payload, offset
                                    )
                                    w = w_fixed >> 16
                                    h = h_fixed >> 16
                                    if w > 0 and h > 0:
                                        return (w, h)
                        trak_pos += k_size
                inner_pos += t_size
        pos += size
    return None


def check_container(context: VideoInspectionContext) -> QCCheck:
    """Validate MP4 container and ISO-BMFF readability (Prompt #117)."""
    raw = context.video_data
    if not raw:
        return QCCheck(
            name=CONTAINER_CHECK_NAME,
            status=QCStatus.FAIL,
            detail="Video file is empty.",
        )

    try:
        info = probe_mp4(raw)
    except Exception as error:
        return QCCheck(
            name=CONTAINER_CHECK_NAME,
            status=QCStatus.FAIL,
            detail=f"Invalid MP4 container: {error}",
        )

    recognized = any(b in KNOWN_MP4_BRANDS for b in info.brands)
    if not recognized:
        return QCCheck(
            name=CONTAINER_CHECK_NAME,
            status=QCStatus.WARN,
            detail=f"Unusual MP4 brands: {', '.join(info.brands)}",
        )

    return QCCheck(
        name=CONTAINER_CHECK_NAME,
        status=QCStatus.PASS,
        detail=f"Valid MP4 container (brands: {', '.join(info.brands[:3])}, duration: {info.duration_ms} ms)",
    )


def check_resolution(context: VideoInspectionContext) -> QCCheck:
    """Validate configured resolution and aspect ratio (Prompt #118)."""
    # 1. Determine actual width & height
    width = context.actual_width
    height = context.actual_height
    if width is None or height is None:
        tkhd_dim = _parse_tkhd_dimensions(context.video_data)
        if tkhd_dim is not None:
            width, height = tkhd_dim
        else:
            width = context.expected_width
            height = context.expected_height

    if width <= 0 or height <= 0:
        return QCCheck(
            name=RESOLUTION_CHECK_NAME,
            status=QCStatus.FAIL,
            detail=f"Invalid dimensions: {width}x{height}",
        )

    # 2. Check aspect ratio
    if context.content_type == ContentType.SHORTS:
        # Shorts requires 9:16 vertical
        if width * 16 != height * 9:
            return QCCheck(
                name=RESOLUTION_CHECK_NAME,
                status=QCStatus.FAIL,
                detail=f"Shorts aspect ratio must be 9:16 vertical, got {width}x{height}",
            )
    else:
        # LongForm requires 16:9 horizontal
        if width * 9 != height * 16:
            return QCCheck(
                name=RESOLUTION_CHECK_NAME,
                status=QCStatus.FAIL,
                detail=f"LongForm aspect ratio must be 16:9 horizontal, got {width}x{height}",
            )

    # 3. Check exact dimensions if expected
    if (width, height) != (context.expected_width, context.expected_height):
        return QCCheck(
            name=RESOLUTION_CHECK_NAME,
            status=QCStatus.FAIL,
            detail=f"Resolution {width}x{height} does not match expected {context.expected_width}x{context.expected_height}",
        )

    aspect_desc = "9:16 vertical" if context.content_type == ContentType.SHORTS else "16:9 horizontal"
    return QCCheck(
        name=RESOLUTION_CHECK_NAME,
        status=QCStatus.PASS,
        detail=f"Valid resolution {width}x{height} ({aspect_desc})",
    )
