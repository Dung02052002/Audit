"""Video QC checks (Prompt Pack v8, prompts K-117..K-120), context C10 Quality (Test & QC).

Concrete QC check implementations that inspect rendered MP4 video files:
- K-117 Container Test (``video.container``): validates MP4 ISO-BMFF structure,
  major and compatible brands, moov/mvhd headers, timescale and duration.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from ai_youtube_agent.content.qc import QCCheck, QCStatus
from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.providers.video_render import Mp4Info, probe_mp4

CONTAINER_CHECK_NAME: Final[str] = "video.container"

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
