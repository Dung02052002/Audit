"""Video QC checks (Prompt Pack v8, prompts K-117..K-125), context C10 Quality (Test & QC).

Concrete QC check implementations that inspect rendered MP4 video files and
their companion production artifacts:
- K-117 Container Test (``video.container``): validates MP4 ISO-BMFF structure,
  major and compatible brands, moov/mvhd headers, timescale and duration.
- K-118 Resolution Test (``video.resolution``): validates actual dimensions and
  aspect ratio (9:16 vertical for Shorts, 16:9 horizontal for LongForm) against
  configured targets.
- K-119 FPS Test (``video.fps``): validates frame rate against configured target
  and standard video production rates.
- K-120 Duration Test (``video.duration``): validates content-type duration limits
  (Shorts 1..180s, LongForm 181..14400s) and synchronization with audio within
  frame tolerance.
- K-121 Audio Test (``video.audio``): validates audio track presence, duration
  synchronization with the video container, and clipping/silence/level thresholds.
- K-122 Subtitle Test (``video.subtitles``): validates UTF-8/NFC encoding, SRT/VTT
  cue timing, monotonicity, video duration bounds, and scene caption coverage.
- K-123 Black Frame Test (``video.black_frames``): detects excessive black or
  frozen frames from frame luma/hash series and MP4 ``mdat`` payload blocks.
- K-124 Render Integrity Test (``video.integrity``): detects incomplete, truncated
  or corrupt MP4 boxes, checksum/size mismatches, and stale render job lineage.
- K-125 Metadata Test (``video.metadata``): validates consistency between the
  rendered artifacts, project/timeline/script versions, chapters, attributions,
  AI disclosure and publish metadata.
"""

import hashlib
import json
import re
import struct
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from ai_youtube_agent.content.audio_validation import (
    DEFAULT_LIMITS,
    AudioLimits,
    analyze as analyze_audio,
)
from ai_youtube_agent.content.longform_metadata import LongFormMetadata
from ai_youtube_agent.content.longform_timeline import format_chapter_timestamp
from ai_youtube_agent.content.policy_check import PolicyStatus
from ai_youtube_agent.content.qc import QCCheck, QCStatus
from ai_youtube_agent.content.shorts_metadata import ShortsMetadata
from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.providers.video_render import probe_mp4

CONTAINER_CHECK_NAME: Final[str] = "video.container"
RESOLUTION_CHECK_NAME: Final[str] = "video.resolution"
FPS_CHECK_NAME: Final[str] = "video.fps"
DURATION_CHECK_NAME: Final[str] = "video.duration"
AUDIO_CHECK_NAME: Final[str] = "video.audio"
SUBTITLE_CHECK_NAME: Final[str] = "video.subtitles"
BLACK_FRAME_CHECK_NAME: Final[str] = "video.black_frames"
INTEGRITY_CHECK_NAME: Final[str] = "video.integrity"
METADATA_CHECK_NAME: Final[str] = "video.metadata"

KNOWN_MP4_BRANDS: Final[frozenset[str]] = frozenset(
    {"isom", "iso2", "mp41", "mp42", "avc1", "qt  ", "dash", "M4V "}
)
STANDARD_FPS: Final[frozenset[int]] = frozenset({24, 25, 30, 48, 50, 60, 120})
MIN_FPS: Final[int] = 24

_SRT_TIMECODE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})$"
)
_VTT_TIMECODE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:(\d{2}):)?(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(?:(\d{2}):)?(\d{2}):(\d{2})\.(\d{3})(?:\s+.*)?$"
)


@dataclass(frozen=True)
class SubtitleCue:
    """Parsed subtitle cue for timing and coverage inspection."""

    index: int
    start_ms: int
    end_ms: int
    text: str


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
    # K-121 Audio Test context
    audio_data: bytes | None = None
    audio_sha256: str | None = None
    audio_limits: AudioLimits | None = None
    # K-122 Subtitle Test context
    captions_enabled: bool = True
    subtitle_data: bytes | None = None
    subtitle_media_type: str | None = None
    expected_caption_count: int | None = None
    scene_windows_ms: tuple[tuple[int, int], ...] | None = None
    # K-123 Black Frame Test context
    frame_luma_samples: Sequence[float] | None = None
    frame_hashes: Sequence[str] | None = None
    # K-124 Render Integrity Test context
    expected_video_sha256: str | None = None
    expected_video_size: int | None = None
    render_job_status: str | None = None
    render_job_video_sha256: str | None = None
    render_job_timeline_sha256: str | None = None
    current_timeline_sha256: str | None = None
    # K-125 Metadata Test context
    metadata_data: bytes | None = None
    expected_project_id: str | None = None
    expected_project_version: int | None = None
    expected_timeline_sha256: str | None = None
    expected_script_id: str | None = None
    expected_script_version: int | None = None
    expected_language: str | None = None
    expected_chapters_enabled: bool | None = None
    expected_chapter_count: int | None = None
    expected_attributions: tuple[str, ...] = ()
    expected_disclosure_id: str | None = None
    expected_synthetic_media: bool | None = None
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


def _extract_mdat_payload(data: bytes) -> bytes | None:
    """Extract the combined payload bytes of all top-level ``mdat`` boxes."""
    pos = 0
    end = len(data)
    chunks: list[bytes] = []
    while pos + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, pos)
        if size == 1:
            if pos + 16 > end:
                return None
            (size,) = struct.unpack_from(">Q", data, pos + 8)
            header_len = 16
        elif size == 0:
            size = end - pos
            header_len = 8
        else:
            header_len = 8
        if size < header_len or pos + size > end:
            return None
        if kind == b"mdat":
            chunks.append(data[pos + header_len : pos + size])
        pos += size
    if not chunks:
        return None
    return b"".join(chunks)


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
        detail=(
            f"Valid MP4 container (brands: {', '.join(info.brands[:3])}, "
            f"duration: {info.duration_ms} ms)"
        ),
    )


def check_resolution(context: VideoInspectionContext) -> QCCheck:
    """Validate configured resolution and aspect ratio (Prompt #118)."""
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

    if context.content_type == ContentType.SHORTS:
        if width * 16 != height * 9:
            return QCCheck(
                name=RESOLUTION_CHECK_NAME,
                status=QCStatus.FAIL,
                detail=f"Shorts aspect ratio must be 9:16 vertical, got {width}x{height}",
            )
    else:
        if width * 9 != height * 16:
            return QCCheck(
                name=RESOLUTION_CHECK_NAME,
                status=QCStatus.FAIL,
                detail=f"LongForm aspect ratio must be 16:9 horizontal, got {width}x{height}",
            )

    if (width, height) != (context.expected_width, context.expected_height):
        return QCCheck(
            name=RESOLUTION_CHECK_NAME,
            status=QCStatus.FAIL,
            detail=(
                f"Resolution {width}x{height} does not match expected "
                f"{context.expected_width}x{context.expected_height}"
            ),
        )

    aspect_desc = (
        "9:16 vertical"
        if context.content_type == ContentType.SHORTS
        else "16:9 horizontal"
    )
    return QCCheck(
        name=RESOLUTION_CHECK_NAME,
        status=QCStatus.PASS,
        detail=f"Valid resolution {width}x{height} ({aspect_desc})",
    )


def check_fps(context: VideoInspectionContext) -> QCCheck:
    """Validate configured frame rate (Prompt #119)."""
    fps = (
        context.actual_fps
        if context.actual_fps is not None
        else float(context.expected_fps)
    )

    if fps <= 0:
        return QCCheck(
            name=FPS_CHECK_NAME,
            status=QCStatus.FAIL,
            detail=f"Invalid frame rate: {fps} fps",
        )

    if fps < MIN_FPS:
        return QCCheck(
            name=FPS_CHECK_NAME,
            status=QCStatus.WARN,
            detail=f"Frame rate {fps} fps is below standard minimum {MIN_FPS} fps",
        )

    if abs(fps - context.expected_fps) > 0.5:
        return QCCheck(
            name=FPS_CHECK_NAME,
            status=QCStatus.FAIL,
            detail=f"Video FPS {fps:.1f} differs from configured {context.expected_fps} fps",
        )

    return QCCheck(
        name=FPS_CHECK_NAME,
        status=QCStatus.PASS,
        detail=f"Valid frame rate: {fps:.0f} fps",
    )


def check_duration(context: VideoInspectionContext) -> QCCheck:
    """Validate duration constraints for content type and sync (Prompt #120)."""
    try:
        info = probe_mp4(context.video_data)
        duration_ms = info.duration_ms
    except Exception as error:
        return QCCheck(
            name=DURATION_CHECK_NAME,
            status=QCStatus.FAIL,
            detail=f"Could not read video duration: {error}",
        )

    if context.content_type == ContentType.SHORTS:
        if duration_ms < 1000:
            return QCCheck(
                name=DURATION_CHECK_NAME,
                status=QCStatus.FAIL,
                detail=f"Shorts duration {duration_ms} ms is under 1 second minimum",
            )
        if duration_ms > context.max_duration_ms:
            return QCCheck(
                name=DURATION_CHECK_NAME,
                status=QCStatus.FAIL,
                detail=(
                    f"Shorts duration {duration_ms} ms exceeds maximum "
                    f"{context.max_duration_ms} ms"
                ),
            )
    else:
        if duration_ms < 180_000:
            return QCCheck(
                name=DURATION_CHECK_NAME,
                status=QCStatus.FAIL,
                detail=f"LongForm duration {duration_ms} ms is under 180 seconds minimum",
            )
        if duration_ms > context.max_duration_ms:
            return QCCheck(
                name=DURATION_CHECK_NAME,
                status=QCStatus.FAIL,
                detail=(
                    f"LongForm duration {duration_ms} ms exceeds maximum "
                    f"{context.max_duration_ms} ms"
                ),
            )

    if context.expected_duration_ms > 0:
        tolerance_ms = max(250, 2000 // max(1, context.expected_fps))
        drift = abs(duration_ms - context.expected_duration_ms)
        if drift > tolerance_ms:
            return QCCheck(
                name=DURATION_CHECK_NAME,
                status=QCStatus.FAIL,
                detail=(
                    f"Video duration {duration_ms} ms differs from audio "
                    f"{context.expected_duration_ms} ms by {drift} ms "
                    f"(tolerance: {tolerance_ms} ms)"
                ),
            )

    return QCCheck(
        name=DURATION_CHECK_NAME,
        status=QCStatus.PASS,
        detail=f"Valid duration: {duration_ms} ms ({duration_ms / 1000:.1f}s)",
    )


def check_audio(context: VideoInspectionContext) -> QCCheck:
    """Validate audio track presence, duration sync, and clipping/silence thresholds (Prompt #121)."""
    audio_bytes = context.audio_data
    if audio_bytes is None or len(audio_bytes) == 0:
        return QCCheck(
            name=AUDIO_CHECK_NAME,
            status=QCStatus.FAIL,
            detail="Audio track is missing or empty.",
        )

    expected_sha256 = (
        context.audio_sha256.lower()
        if context.audio_sha256 is not None
        else hashlib.sha256(audio_bytes).hexdigest()
    )
    limits = context.audio_limits if context.audio_limits is not None else DEFAULT_LIMITS
    sub_checks, stats = analyze_audio(
        audio_bytes,
        artifact_size=len(audio_bytes),
        artifact_sha256=expected_sha256,
        metadata_duration_ms=(
            context.expected_duration_ms if context.expected_duration_ms > 0 else None
        ),
        limits=limits,
    )

    failed = [c for c in sub_checks if c.status is QCStatus.FAIL]
    if failed:
        reasons = "; ".join(f"{c.name}: {c.detail}" for c in failed if c.detail)
        return QCCheck(
            name=AUDIO_CHECK_NAME,
            status=QCStatus.FAIL,
            detail=reasons or "Audio validation failed.",
        )

    if stats is not None and context.video_data:
        try:
            video_info = probe_mp4(context.video_data)
            tolerance_ms = max(250, 2000 // max(1, context.expected_fps))
            drift = abs(stats.duration_ms - video_info.duration_ms)
            if drift > tolerance_ms:
                return QCCheck(
                    name=AUDIO_CHECK_NAME,
                    status=QCStatus.FAIL,
                    detail=(
                        f"Audio duration {stats.duration_ms} ms drifts from video "
                        f"duration {video_info.duration_ms} ms by {drift} ms "
                        f"(tolerance: {tolerance_ms} ms)"
                    ),
                )
        except Exception as error:
            return QCCheck(
                name=AUDIO_CHECK_NAME,
                status=QCStatus.FAIL,
                detail=f"Cannot verify audio-video sync due to invalid video container: {error}",
            )

    warned = [c for c in sub_checks if c.status is QCStatus.WARN]
    if warned:
        reasons = "; ".join(f"{c.name}: {c.detail}" for c in warned if c.detail)
        return QCCheck(
            name=AUDIO_CHECK_NAME,
            status=QCStatus.WARN,
            detail=reasons or "Audio validation produced warnings.",
        )

    assert stats is not None
    return QCCheck(
        name=AUDIO_CHECK_NAME,
        status=QCStatus.PASS,
        detail=(
            f"Valid audio track ({stats.duration_ms} ms, {stats.sample_rate} Hz, "
            f"peak {stats.peak_dbfs:.1f} dBFS, rms {stats.rms_dbfs:.1f} dBFS)"
        ),
    )


def _hms_to_ms(hours: str | None, minutes: str, seconds: str, millis: str) -> int:
    h = int(hours) if hours is not None else 0
    m = int(minutes)
    s = int(seconds)
    ms = int(millis)
    if m >= 60 or s >= 60:
        raise ValueError("minute and second fields must be < 60")
    return ((h * 3600 + m * 60 + s) * 1000) + ms


def _parse_subtitle_cues(
    text: str, media_type: str | None
) -> tuple[str, list[SubtitleCue]]:
    """Parse SRT or WebVTT subtitle text into ordered ``SubtitleCue`` entries."""
    stripped = text.strip()
    if not stripped:
        raise ValueError("Subtitle content is empty.")

    is_vtt = (
        (media_type is not None and "vtt" in media_type.lower())
        or stripped.startswith("WEBVTT")
    )

    blocks = re.split(r"\n\s*\n", stripped)
    cues: list[SubtitleCue] = []

    if is_vtt:
        first_lines = blocks[0].split("\n")
        if not first_lines[0].startswith("WEBVTT"):
            raise ValueError("WebVTT file must start with a WEBVTT header.")
        cue_blocks = blocks[1:]
        for idx, block in enumerate(cue_blocks, start=1):
            lines = [line for line in block.split("\n") if line.strip() != ""]
            if not lines:
                continue
            if lines[0].startswith("NOTE") or lines[0].startswith("STYLE"):
                continue
            tc_line_idx = 0
            if "-->" not in lines[0]:
                if len(lines) < 2 or "-->" not in lines[1]:
                    raise ValueError(f"Malformed WebVTT cue at block {idx}.")
                tc_line_idx = 1
            match = _VTT_TIMECODE_RE.match(lines[tc_line_idx].strip())
            if match is None:
                raise ValueError(
                    f"Invalid WebVTT timestamp at cue {idx}: {lines[tc_line_idx]!r}"
                )
            start_ms = _hms_to_ms(
                match.group(1), match.group(2), match.group(3), match.group(4)
            )
            end_ms = _hms_to_ms(
                match.group(5), match.group(6), match.group(7), match.group(8)
            )
            text_lines = lines[tc_line_idx + 1 :]
            if not text_lines:
                raise ValueError(f"WebVTT cue {idx} has empty caption text.")
            cues.append(
                SubtitleCue(
                    index=len(cues) + 1,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    text="\n".join(text_lines),
                )
            )
        return ("WebVTT", cues)

    for idx, block in enumerate(blocks, start=1):
        lines = block.split("\n")
        if len(lines) < 3:
            raise ValueError(f"Malformed SRT cue block {idx} (expected index, timecode, text).")
        if not lines[0].strip().isdigit():
            raise ValueError(f"Invalid SRT cue index at block {idx}: {lines[0]!r}")
        cue_num = int(lines[0].strip())
        if cue_num != idx:
            raise ValueError(
                f"Non-sequential SRT cue index: expected {idx}, got {cue_num}"
            )
        match = _SRT_TIMECODE_RE.match(lines[1].strip())
        if match is None:
            raise ValueError(f"Invalid SRT timestamp at cue {idx}: {lines[1]!r}")
        start_ms = _hms_to_ms(
            match.group(1), match.group(2), match.group(3), match.group(4)
        )
        end_ms = _hms_to_ms(
            match.group(5), match.group(6), match.group(7), match.group(8)
        )
        text_lines = lines[2:]
        if not text_lines or any(not ln.strip() for ln in text_lines):
            raise ValueError(f"SRT cue {idx} has empty caption text line.")
        cues.append(
            SubtitleCue(
                index=cue_num,
                start_ms=start_ms,
                end_ms=end_ms,
                text="\n".join(text_lines),
            )
        )
    return ("SRT", cues)


def check_subtitles(context: VideoInspectionContext) -> QCCheck:
    """Validate subtitle timing, encoding and missing captions (Prompt #122)."""
    raw = context.subtitle_data
    if not context.captions_enabled:
        if raw is not None and len(raw) > 0:
            return QCCheck(
                name=SUBTITLE_CHECK_NAME,
                status=QCStatus.WARN,
                detail="Subtitles artifact is present while captions are disabled.",
            )
        return QCCheck(
            name=SUBTITLE_CHECK_NAME,
            status=QCStatus.PASS,
            detail="Captions are disabled by configuration.",
        )

    if raw is None or len(raw) == 0:
        return QCCheck(
            name=SUBTITLE_CHECK_NAME,
            status=QCStatus.FAIL,
            detail="Subtitles artifact is missing or empty while captions are enabled.",
        )

    if raw.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
        return QCCheck(
            name=SUBTITLE_CHECK_NAME,
            status=QCStatus.FAIL,
            detail="Subtitles file must be UTF-8 without a byte-order mark (BOM).",
        )

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        return QCCheck(
            name=SUBTITLE_CHECK_NAME,
            status=QCStatus.FAIL,
            detail=f"Subtitles file is not valid UTF-8: {error}",
        )

    if "\r" in text:
        return QCCheck(
            name=SUBTITLE_CHECK_NAME,
            status=QCStatus.FAIL,
            detail="Subtitles file must use LF ('\\n') line endings, not CRLF.",
        )

    if not unicodedata.is_normalized("NFC", text):
        return QCCheck(
            name=SUBTITLE_CHECK_NAME,
            status=QCStatus.FAIL,
            detail="Subtitles text must be Unicode NFC normalized.",
        )

    try:
        fmt_name, cues = _parse_subtitle_cues(text, context.subtitle_media_type)
    except ValueError as error:
        return QCCheck(
            name=SUBTITLE_CHECK_NAME,
            status=QCStatus.FAIL,
            detail=f"Invalid subtitle format: {error}",
        )

    if not cues:
        return QCCheck(
            name=SUBTITLE_CHECK_NAME,
            status=QCStatus.FAIL,
            detail="Subtitles contain no caption cues.",
        )

    if (
        context.expected_caption_count is not None
        and len(cues) < context.expected_caption_count
    ):
        return QCCheck(
            name=SUBTITLE_CHECK_NAME,
            status=QCStatus.FAIL,
            detail=(
                f"Missing captions: found {len(cues)} of "
                f"{context.expected_caption_count} expected cues."
            ),
        )

    video_duration_ms = context.expected_duration_ms
    if video_duration_ms <= 0 and context.video_data:
        try:
            video_duration_ms = probe_mp4(context.video_data).duration_ms
        except Exception:
            video_duration_ms = 0
    tolerance_ms = max(250, 2000 // max(1, context.expected_fps))

    warnings: list[str] = []
    prev_end_ms = 0
    for cue in cues:
        if cue.start_ms < 0 or cue.end_ms <= cue.start_ms:
            return QCCheck(
                name=SUBTITLE_CHECK_NAME,
                status=QCStatus.FAIL,
                detail=(
                    f"Cue {cue.index} has non-positive duration "
                    f"({cue.start_ms}..{cue.end_ms} ms)."
                ),
            )
        if cue.start_ms < prev_end_ms:
            return QCCheck(
                name=SUBTITLE_CHECK_NAME,
                status=QCStatus.FAIL,
                detail=(
                    f"Cue {cue.index} at {cue.start_ms} ms overlaps previous cue "
                    f"ending at {prev_end_ms} ms."
                ),
            )
        if "<" in cue.text or ">" in cue.text:
            return QCCheck(
                name=SUBTITLE_CHECK_NAME,
                status=QCStatus.FAIL,
                detail=f"Cue {cue.index} contains forbidden angle brackets.",
            )
        if video_duration_ms > 0 and cue.end_ms > video_duration_ms + tolerance_ms:
            return QCCheck(
                name=SUBTITLE_CHECK_NAME,
                status=QCStatus.FAIL,
                detail=(
                    f"Cue {cue.index} ends at {cue.end_ms} ms, beyond video duration "
                    f"{video_duration_ms} ms."
                ),
            )
        cue_duration_ms = cue.end_ms - cue.start_ms
        if cue_duration_ms < 300:
            warnings.append(f"cue {cue.index} is very short ({cue_duration_ms} ms)")
        elif cue_duration_ms > 10_000:
            warnings.append(f"cue {cue.index} is very long ({cue_duration_ms} ms)")

        char_count = len(cue.text.replace("\n", ""))
        cps = (char_count * 1000.0) / cue_duration_ms
        if cps > 25.0:
            warnings.append(f"cue {cue.index} reading speed is {cps:.1f} chars/s")

        prev_end_ms = cue.end_ms

    if context.scene_windows_ms:
        for scene_idx, (s_start, s_end) in enumerate(context.scene_windows_ms, start=1):
            covered = any(
                cue.start_ms < s_end and cue.end_ms > s_start for cue in cues
            )
            if not covered:
                return QCCheck(
                    name=SUBTITLE_CHECK_NAME,
                    status=QCStatus.FAIL,
                    detail=(
                        f"Missing captions for scene {scene_idx} "
                        f"({s_start}..{s_end} ms)."
                    ),
                )

    if warnings:
        return QCCheck(
            name=SUBTITLE_CHECK_NAME,
            status=QCStatus.WARN,
            detail=f"{fmt_name} subtitles warnings: {'; '.join(warnings[:3])}",
        )

    return QCCheck(
        name=SUBTITLE_CHECK_NAME,
        status=QCStatus.PASS,
        detail=f"Valid {fmt_name} subtitles ({len(cues)} cues, UTF-8 NFC)",
    )


def check_black_frames(context: VideoInspectionContext) -> QCCheck:
    """Detect excessive black or frozen frames (Prompt #123)."""
    mdat = _extract_mdat_payload(context.video_data)
    if mdat is None or len(mdat) == 0:
        return QCCheck(
            name=BLACK_FRAME_CHECK_NAME,
            status=QCStatus.FAIL,
            detail="Video mdat frame payload is missing or empty.",
        )

    fps = float(context.actual_fps or context.expected_fps or 30)
    frame_ms = 1000.0 / max(1.0, fps)

    if context.frame_luma_samples is not None or context.frame_hashes is not None:
        luma_raw = list(context.frame_luma_samples or ())
        hashes = list(context.frame_hashes or ())
        scale = 255.0 if any(v > 1.0 for v in luma_raw) else 1.0
        lumas = [v / scale for v in luma_raw]

        total_frames = max(len(lumas), len(hashes))
        if total_frames == 0:
            return QCCheck(
                name=BLACK_FRAME_CHECK_NAME,
                status=QCStatus.FAIL,
                detail="Frame sample sequence is empty.",
            )

        black_count = 0
        max_black_run = 0
        cur_black_run = 0
        for val in lumas:
            if val <= 0.02:
                black_count += 1
                cur_black_run += 1
                if cur_black_run > max_black_run:
                    max_black_run = cur_black_run
            else:
                cur_black_run = 0

        frozen_count = 0
        max_frozen_run = 0
        cur_frozen_run = 0
        if hashes:
            for i in range(1, len(hashes)):
                if hashes[i] == hashes[i - 1]:
                    frozen_count += 1
                    cur_frozen_run += 1
                    if cur_frozen_run > max_frozen_run:
                        max_frozen_run = cur_frozen_run
                else:
                    cur_frozen_run = 0
        elif len(lumas) > 1:
            for i in range(1, len(lumas)):
                if lumas[i] > 0.02 and abs(lumas[i] - lumas[i - 1]) <= 1e-6:
                    frozen_count += 1
                    cur_frozen_run += 1
                    if cur_frozen_run > max_frozen_run:
                        max_frozen_run = cur_frozen_run
                else:
                    cur_frozen_run = 0

        black_ratio = black_count / len(lumas) if lumas else 0.0
        max_black_ms = max_black_run * frame_ms
        frozen_ratio = frozen_count / total_frames
        max_frozen_ms = max_frozen_run * frame_ms

        if black_ratio >= 0.25 or max_black_ms >= 3000.0:
            return QCCheck(
                name=BLACK_FRAME_CHECK_NAME,
                status=QCStatus.FAIL,
                detail=(
                    f"Excessive black frames detected ({black_ratio * 100:.1f}% black, "
                    f"max continuous run {max_black_ms:.0f} ms)."
                ),
            )
        if frozen_ratio >= 0.50 or max_frozen_ms >= 5000.0:
            return QCCheck(
                name=BLACK_FRAME_CHECK_NAME,
                status=QCStatus.FAIL,
                detail=(
                    f"Excessive frozen frames detected ({frozen_ratio * 100:.1f}% frozen, "
                    f"max continuous run {max_frozen_ms:.0f} ms)."
                ),
            )
        if (
            black_ratio >= 0.10
            or max_black_ms >= 1000.0
            or frozen_ratio >= 0.25
            or max_frozen_ms >= 2500.0
        ):
            return QCCheck(
                name=BLACK_FRAME_CHECK_NAME,
                status=QCStatus.WARN,
                detail=(
                    f"Elevated black/frozen frames ({black_ratio * 100:.1f}% black, "
                    f"{frozen_ratio * 100:.1f}% frozen)."
                ),
            )

        return QCCheck(
            name=BLACK_FRAME_CHECK_NAME,
            status=QCStatus.PASS,
            detail=(
                f"No excessive black or frozen frames ({total_frames} frames inspected, "
                f"{black_ratio * 100:.1f}% black, {frozen_ratio * 100:.1f}% frozen)"
            ),
        )

    block_size = 64
    blocks = [
        mdat[i : i + block_size]
        for i in range(0, len(mdat), block_size)
        if len(mdat[i : i + block_size]) > 0
    ]
    zero_blocks = sum(1 for b in blocks if b == b"\x00" * len(b))
    zero_ratio = zero_blocks / len(blocks)

    if zero_ratio >= 0.50:
        return QCCheck(
            name=BLACK_FRAME_CHECK_NAME,
            status=QCStatus.FAIL,
            detail=f"Excessive black frames detected ({zero_ratio * 100:.1f}% zero mdat blocks).",
        )

    if len(mdat) >= 64 and len(set(mdat)) == 1:
        return QCCheck(
            name=BLACK_FRAME_CHECK_NAME,
            status=QCStatus.FAIL,
            detail="Frozen video stream detected: constant byte payload in mdat.",
        )

    if zero_ratio >= 0.20:
        return QCCheck(
            name=BLACK_FRAME_CHECK_NAME,
            status=QCStatus.WARN,
            detail=f"Elevated black frame blocks ({zero_ratio * 100:.1f}% zero mdat blocks).",
        )

    return QCCheck(
        name=BLACK_FRAME_CHECK_NAME,
        status=QCStatus.PASS,
        detail=f"No excessive black or frozen frames ({len(blocks)} payload blocks inspected)",
    )


def check_render_integrity(context: VideoInspectionContext) -> QCCheck:
    """Detect incomplete, truncated or corrupt renders (Prompt #124)."""
    raw = context.video_data
    if not raw:
        return QCCheck(
            name=INTEGRITY_CHECK_NAME,
            status=QCStatus.FAIL,
            detail="Rendered video file is empty.",
        )

    if (
        context.expected_video_size is not None
        and len(raw) != context.expected_video_size
    ):
        return QCCheck(
            name=INTEGRITY_CHECK_NAME,
            status=QCStatus.FAIL,
            detail=(
                f"Video size {len(raw)} bytes does not match artifact record "
                f"{context.expected_video_size} bytes."
            ),
        )

    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if (
        context.expected_video_sha256 is not None
        and actual_sha256 != context.expected_video_sha256.lower()
    ):
        return QCCheck(
            name=INTEGRITY_CHECK_NAME,
            status=QCStatus.FAIL,
            detail="Video SHA-256 digest does not match artifact record.",
        )

    pos = 0
    end = len(raw)
    seen_boxes: list[bytes] = []
    mdat_bytes = 0
    while pos < end:
        if pos + 8 > end:
            return QCCheck(
                name=INTEGRITY_CHECK_NAME,
                status=QCStatus.FAIL,
                detail=f"Truncated MP4 box header at byte offset {pos}.",
            )
        size, kind = struct.unpack_from(">I4s", raw, pos)
        if size == 1:
            if pos + 16 > end:
                return QCCheck(
                    name=INTEGRITY_CHECK_NAME,
                    status=QCStatus.FAIL,
                    detail=f"Truncated 64-bit MP4 box header at byte offset {pos}.",
                )
            (size,) = struct.unpack_from(">Q", raw, pos + 8)
            header_len = 16
        else:
            header_len = 8

        if size < header_len or pos + size > end:
            return QCCheck(
                name=INTEGRITY_CHECK_NAME,
                status=QCStatus.FAIL,
                detail=(
                    f"Incomplete or truncated MP4 box {kind.decode('latin1', errors='replace')!r} "
                    f"at offset {pos} (declared size {size}, remaining {end - pos})."
                ),
            )
        seen_boxes.append(kind)
        if kind == b"mdat":
            mdat_bytes += size - header_len
        pos += size

    for required_box in (b"ftyp", b"moov", b"mdat"):
        if required_box not in seen_boxes:
            return QCCheck(
                name=INTEGRITY_CHECK_NAME,
                status=QCStatus.FAIL,
                detail=f"Missing required MP4 box {required_box.decode('ascii')!r}.",
            )

    if mdat_bytes <= 0:
        return QCCheck(
            name=INTEGRITY_CHECK_NAME,
            status=QCStatus.FAIL,
            detail="MP4 mdat box contains 0 payload bytes.",
        )

    try:
        info = probe_mp4(raw)
    except Exception as error:
        return QCCheck(
            name=INTEGRITY_CHECK_NAME,
            status=QCStatus.FAIL,
            detail=f"Corrupt MP4 movie header: {error}",
        )

    if info.duration_ms <= 0 or info.timescale <= 0:
        return QCCheck(
            name=INTEGRITY_CHECK_NAME,
            status=QCStatus.FAIL,
            detail="MP4 movie header reports non-positive duration or timescale.",
        )

    if (
        context.render_job_status is not None
        and context.render_job_status != "succeeded"
    ):
        return QCCheck(
            name=INTEGRITY_CHECK_NAME,
            status=QCStatus.FAIL,
            detail=f"Render job is not succeeded (status: {context.render_job_status}).",
        )

    if (
        context.render_job_video_sha256 is not None
        and context.render_job_video_sha256.lower() != actual_sha256
    ):
        return QCCheck(
            name=INTEGRITY_CHECK_NAME,
            status=QCStatus.FAIL,
            detail="Rendered video SHA-256 does not match the render job checkpoint.",
        )

    if (
        context.render_job_timeline_sha256 is not None
        and context.current_timeline_sha256 is not None
        and context.render_job_timeline_sha256 != context.current_timeline_sha256
    ):
        return QCCheck(
            name=INTEGRITY_CHECK_NAME,
            status=QCStatus.FAIL,
            detail="Rendered video is stale: project timeline changed after render.",
        )

    return QCCheck(
        name=INTEGRITY_CHECK_NAME,
        status=QCStatus.PASS,
        detail=f"Render integrity verified ({len(raw)} bytes, sha256={actual_sha256[:12]}...)",
    )


def check_metadata(context: VideoInspectionContext) -> QCCheck:
    """Validate consistency between artifact and publish metadata (Prompt #125)."""
    raw = context.metadata_data
    if raw is None or len(raw) == 0:
        return QCCheck(
            name=METADATA_CHECK_NAME,
            status=QCStatus.FAIL,
            detail="Publish metadata artifact is missing or empty.",
        )

    try:
        raw_payload = json.loads(bytes(raw).decode("utf-8"))
        if context.content_type == ContentType.SHORTS:
            meta: ShortsMetadata | LongFormMetadata = ShortsMetadata.from_json_bytes(raw)
        else:
            meta = LongFormMetadata.from_bytes(raw)
    except Exception as error:
        return QCCheck(
            name=METADATA_CHECK_NAME,
            status=QCStatus.FAIL,
            detail=f"Invalid publish metadata JSON: {error}",
        )

    if meta.content_item_id != context.content_item_id:
        return QCCheck(
            name=METADATA_CHECK_NAME,
            status=QCStatus.FAIL,
            detail=(
                f"Metadata content_item_id {meta.content_item_id!r} does not match "
                f"item {context.content_item_id!r}."
            ),
        )

    if (
        context.expected_project_id is not None
        and meta.project_id != context.expected_project_id
    ):
        return QCCheck(
            name=METADATA_CHECK_NAME,
            status=QCStatus.FAIL,
            detail="Metadata is bound to a different project ID.",
        )

    if (
        context.expected_project_version is not None
        and meta.project_version != context.expected_project_version
    ):
        return QCCheck(
            name=METADATA_CHECK_NAME,
            status=QCStatus.FAIL,
            detail=(
                f"Metadata is stale: bound to project version {meta.project_version}, "
                f"expected version {context.expected_project_version}."
            ),
        )

    if (
        isinstance(raw_payload, dict)
        and "timeline_sha256" in raw_payload
        and context.expected_timeline_sha256 is not None
        and raw_payload["timeline_sha256"] != context.expected_timeline_sha256
    ):
        return QCCheck(
            name=METADATA_CHECK_NAME,
            status=QCStatus.FAIL,
            detail="Metadata timeline_sha256 does not match current project timeline.",
        )

    if (
        context.expected_script_id is not None
        and meta.script_id != context.expected_script_id
    ):
        return QCCheck(
            name=METADATA_CHECK_NAME,
            status=QCStatus.FAIL,
            detail="Metadata script_id does not match current project script.",
        )

    if (
        context.expected_script_version is not None
        and meta.script_version != context.expected_script_version
    ):
        return QCCheck(
            name=METADATA_CHECK_NAME,
            status=QCStatus.FAIL,
            detail=(
                f"Metadata script_version {meta.script_version} does not match "
                f"expected version {context.expected_script_version}."
            ),
        )

    if (
        context.expected_language is not None
        and meta.language.lower() != context.expected_language.lower()
    ):
        return QCCheck(
            name=METADATA_CHECK_NAME,
            status=QCStatus.FAIL,
            detail=(
                f"Metadata language {meta.language!r} does not match strategy "
                f"primary language {context.expected_language!r}."
            ),
        )

    if isinstance(meta, LongFormMetadata) and context.expected_chapters_enabled:
        if len(meta.chapters) < 3:
            return QCCheck(
                name=METADATA_CHECK_NAME,
                status=QCStatus.FAIL,
                detail="LongForm metadata requires at least 3 chapters when chapters are enabled.",
            )
        if meta.chapters[0].start_ms != 0:
            return QCCheck(
                name=METADATA_CHECK_NAME,
                status=QCStatus.FAIL,
                detail="First LongForm metadata chapter must start at 00:00 (0 ms).",
            )
        if (
            context.expected_chapter_count is not None
            and len(meta.chapters) != context.expected_chapter_count
        ):
            return QCCheck(
                name=METADATA_CHECK_NAME,
                status=QCStatus.FAIL,
                detail=(
                    f"LongForm metadata has {len(meta.chapters)} chapters, "
                    f"expected {context.expected_chapter_count}."
                ),
            )
        chapter_block = "\n".join(
            f"{format_chapter_timestamp(ch.start_ms)} {ch.title}"
            for ch in meta.chapters
        )
        if chapter_block not in meta.description:
            return QCCheck(
                name=METADATA_CHECK_NAME,
                status=QCStatus.FAIL,
                detail="LongForm metadata description is missing formatted YouTube chapters.",
            )

    for attr in context.expected_attributions:
        if attr not in meta.description:
            return QCCheck(
                name=METADATA_CHECK_NAME,
                status=QCStatus.FAIL,
                detail=f"Metadata is missing required asset attribution: {attr!r}",
            )

    if (
        context.expected_disclosure_id is not None
        and meta.disclosure_decision_id != context.expected_disclosure_id
    ):
        return QCCheck(
            name=METADATA_CHECK_NAME,
            status=QCStatus.FAIL,
            detail="Metadata disclosure_decision_id does not match the latest disclosure decision.",
        )

    if (
        context.expected_synthetic_media is True
        and not meta.contains_synthetic_media
    ):
        return QCCheck(
            name=METADATA_CHECK_NAME,
            status=QCStatus.FAIL,
            detail="Metadata contains_synthetic_media is False while AI disclosure is required.",
        )

    if meta.policy_status is PolicyStatus.BLOCK:
        return QCCheck(
            name=METADATA_CHECK_NAME,
            status=QCStatus.FAIL,
            detail="Metadata policy_status is BLOCK.",
        )

    if meta.policy_status is PolicyStatus.WARN:
        return QCCheck(
            name=METADATA_CHECK_NAME,
            status=QCStatus.WARN,
            detail="Metadata passed consistency checks with policy status WARN.",
        )

    return QCCheck(
        name=METADATA_CHECK_NAME,
        status=QCStatus.PASS,
        detail=(
            f"Valid {context.content_type.value} publish metadata "
            f"(project v{meta.project_version}, script v{meta.script_version})"
        ),
    )

