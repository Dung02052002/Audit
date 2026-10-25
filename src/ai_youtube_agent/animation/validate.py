"""Validation of the finished film: container, decode, motion, scenes, composition.

Everything that can be computed from numbers is a pure function (``check_container``,
``analyze_motion``, ``histogram_distance``, ``CompositionTally``) so that it is
tested without a video; the functions that start ffmpeg/ffprobe take a ``run``
argument for the same reason. The audio checks live in ``audio_qc``.

The thresholds below were set from a measured run (see ``docs/ANIMATION_PIPELINE.md``)
and are the ones written to ``validation.json``.
"""

from __future__ import annotations

import json
import math
import re
import struct
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from ai_youtube_agent.animation.errors import AnimationError

Box = tuple[int, int, int, int]
RunProcess = Callable[..., Any]

DURATION_CAP_S: Final[float] = 60.0
AV_SYNC_TOLERANCE_S: Final[float] = 0.1
# Motion: YAVG of the difference of consecutive 108x192 frames (0..255). The film
# grain, new on every frame, is a floor of about 0.52 in this metric (measured on
# the first live run: the quietest frame was 0.518), so "no motion" is the floor plus
# a margin and a moving second has to be clearly above it.
GRAIN_FLOOR: Final[float] = 0.52
MOTION_SECOND_MIN: Final[float] = 0.8
FROZEN_FRAME_MAX: Final[float] = 0.62
FROZEN_MAX_S: Final[float] = 0.5
SCENE_MOVING_SECONDS_MIN: Final[float] = 0.7
# Scene distinctness (half the L1 distance of 4-bin-per-channel colour histograms).
SCENE_DISTANCE_MIN: Final[float] = 0.12
THUMB_SIZE: Final[tuple[int, int]] = (64, 114)
HISTOGRAM_BINS: Final[int] = 4
# Composition.
FACE_CLEAR_MIN: Final[float] = 0.98
TEXT_AREA_MAX: Final[float] = 0.04
CHARACTER_TO_TEXT_MIN: Final[float] = 5.0


# ---------------------------------------------------------------------------
# ffprobe and the container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExpectedMedia:
    width: int
    height: int
    fps: int
    frames: int
    min_s: float
    max_s: float


def ffprobe_binary(ffmpeg_binary: str) -> str:
    """The ffprobe next to ``ffmpeg_binary`` (or the one on PATH)."""
    import shutil

    path = Path(ffmpeg_binary)
    if path.parent != Path("."):
        sibling = path.with_name(path.name.replace("ffmpeg", "ffprobe"))
        if sibling.exists():
            return str(sibling)
    return shutil.which("ffprobe") or "ffprobe"


def probe_media(
    path: Path, ffprobe: str = "ffprobe", *, run: RunProcess = subprocess.run
) -> dict[str, Any]:
    proc = run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AnimationError(f"ffprobe failed: {str(proc.stderr).strip()[-300:]}")
    return json.loads(proc.stdout)


def mp4_boxes(path: Path) -> list[tuple[str, int]]:
    """Top-level ``(type, offset)`` boxes of an MP4 (to see moov before mdat)."""
    boxes: list[tuple[str, int]] = []
    size_total = path.stat().st_size
    with open(path, "rb") as handle:
        offset = 0
        while offset + 8 <= size_total:
            handle.seek(offset)
            header = handle.read(16)
            size, kind = struct.unpack(">I4s", header[:8])
            if size == 1 and len(header) >= 16:
                size = struct.unpack(">Q", header[8:16])[0]
            elif size == 0:
                size = size_total - offset
            boxes.append((kind.decode("latin-1"), offset))
            if size < 8:
                break
            offset += size
    return boxes


def faststart_ok(path: Path) -> bool:
    """True when the ``moov`` box comes before ``mdat`` (the file streams at once)."""
    order = {kind: offset for kind, offset in reversed(mp4_boxes(path))}
    return "moov" in order and "mdat" in order and order["moov"] < order["mdat"]


def _fraction(text: Any) -> float:
    try:
        numerator, _, denominator = str(text).partition("/")
        return float(numerator) / float(denominator or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def check_container(
    probe: dict[str, Any], expected: ExpectedMedia, *, faststart: bool
) -> tuple[list[str], dict[str, Any]]:
    """``(problems, facts)`` of the probed MP4 against what the tool promised."""
    problems: list[str] = []
    streams = probe.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float(probe.get("format", {}).get("duration", 0.0) or 0.0)
    facts: dict[str, Any] = {"duration_s": round(duration, 3), "faststart": faststart}
    if video is None:
        problems.append("no video stream")
    else:
        facts["video"] = {
            key: video.get(key)
            for key in (
                "codec_name",
                "profile",
                "level",
                "width",
                "height",
                "pix_fmt",
                "sample_aspect_ratio",
                "r_frame_rate",
                "avg_frame_rate",
                "nb_frames",
                "duration",
                "color_space",
                "color_primaries",
                "color_transfer",
            )
        }
        if video.get("codec_name") != "h264":
            problems.append(f"video codec is {video.get('codec_name')}, not h264")
        if video.get("profile") != "High":
            problems.append(f"video profile is {video.get('profile')}, not High")
        if (video.get("width"), video.get("height")) != (
            expected.width,
            expected.height,
        ):
            problems.append(
                f"video is {video.get('width')}x{video.get('height')}, expected "
                f"{expected.width}x{expected.height}"
            )
        if video.get("sample_aspect_ratio") not in (None, "1:1"):
            problems.append(
                f"sample aspect ratio is {video.get('sample_aspect_ratio')}"
            )
        if abs(_fraction(video.get("r_frame_rate")) - expected.fps) > 1e-6:
            problems.append(
                f"frame rate is {video.get('r_frame_rate')}, not {expected.fps}/1"
            )
        if str(video.get("nb_frames")) != str(expected.frames):
            problems.append(
                f"video has {video.get('nb_frames')} frames, expected {expected.frames}"
            )
        if video.get("pix_fmt") != "yuv420p":
            problems.append(f"pixel format is {video.get('pix_fmt')}, not yuv420p")
    if audio is None:
        problems.append("no audio stream")
    else:
        facts["audio"] = {
            key: audio.get(key)
            for key in ("codec_name", "sample_rate", "channels", "bit_rate", "duration")
        }
        if audio.get("codec_name") != "aac":
            problems.append(f"audio codec is {audio.get('codec_name')}, not aac")
        if str(audio.get("sample_rate")) != "48000":
            problems.append(
                f"audio sample rate is {audio.get('sample_rate')}, not 48000"
            )
        if audio.get("channels") != 2:
            problems.append(f"audio has {audio.get('channels')} channels, not 2")
    if video is not None and audio is not None:
        try:
            difference = abs(float(video["duration"]) - float(audio["duration"]))
        except (KeyError, TypeError, ValueError):
            difference = math.inf
        facts["av_difference_s"] = (
            None if math.isinf(difference) else round(difference, 3)
        )
        if difference > AV_SYNC_TOLERANCE_S:
            problems.append(
                f"audio and video differ by {difference:.3f} s "
                f"(limit {AV_SYNC_TOLERANCE_S})"
            )
    if not expected.min_s <= duration <= expected.max_s:
        problems.append(
            f"duration {duration:.2f} s is outside "
            f"{expected.min_s:g}-{expected.max_s:g} s"
        )
    if duration > DURATION_CAP_S:
        problems.append(
            f"duration {duration:.2f} s is above the {DURATION_CAP_S:g} s cap"
        )
    if not faststart:
        problems.append("the moov box is not before mdat (no faststart)")
    return problems, facts


def decode_errors(
    path: Path, ffmpeg_binary: str = "ffmpeg", *, run: RunProcess = subprocess.run
) -> list[str]:
    """Full decode of video and audio; each line ffmpeg prints is a problem."""
    proc = run(
        [ffmpeg_binary, "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    lines = [line.strip() for line in str(proc.stderr).splitlines() if line.strip()]
    if proc.returncode != 0 and not lines:
        lines = [f"ffmpeg exited with {proc.returncode}"]
    return lines


# ---------------------------------------------------------------------------
# Motion
# ---------------------------------------------------------------------------

_PTS_TIME = re.compile(r"pts_time:(-?\d+(?:\.\d+)?)")
_YAVG = re.compile(r"lavfi\.signalstats\.YAVG=(-?\d+(?:\.\d+)?)")
MOTION_FILE: Final[str] = "motion.txt"


def motion_command(video: Path, ffmpeg_binary: str = "ffmpeg") -> list[str]:
    """Per-frame mean luma difference of consecutive frames, written to
    ``motion.txt`` in the working directory (a relative name: no path in a filter)."""
    return [
        ffmpeg_binary,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video),
        "-an",
        "-vf",
        "scale=108:192,tblend=all_mode=difference,signalstats,"
        f"metadata=print:key=lavfi.signalstats.YAVG:file={MOTION_FILE}",
        "-f",
        "null",
        "-",
    ]


def parse_motion(text: str) -> list[tuple[float, float]]:
    """``(time in s, YAVG)`` per frame from the ``metadata=print`` file."""
    series: list[tuple[float, float]] = []
    pending: float | None = None
    for line in text.splitlines():
        match = _PTS_TIME.search(line)
        if match:
            pending = float(match.group(1))
            continue
        match = _YAVG.search(line)
        if match and pending is not None:
            series.append((pending, float(match.group(1))))
            pending = None
    return series


def motion_series(
    video: Path,
    work_dir: Path,
    ffmpeg_binary: str = "ffmpeg",
    *,
    run: RunProcess = subprocess.run,
) -> list[tuple[float, float]]:
    target = work_dir / MOTION_FILE
    target.unlink(missing_ok=True)
    proc = run(
        motion_command(video.resolve(), ffmpeg_binary),
        capture_output=True,
        text=True,
        check=False,
        cwd=str(work_dir),
    )
    if proc.returncode != 0 or not target.exists():
        raise AnimationError(
            f"ffmpeg motion analysis failed: {str(proc.stderr)[-300:]}"
        )
    return parse_motion(target.read_text(encoding="utf-8", errors="replace"))


def frozen_stretches(
    series: Sequence[tuple[float, float]],
    holds: Sequence[tuple[float, float]] = (),
    *,
    floor: float = FROZEN_FRAME_MAX,
    max_s: float = FROZEN_MAX_S,
) -> list[tuple[float, float]]:
    """Runs of near-identical frames longer than ``max_s`` that are not in ``holds``
    (``(start_s, end_s)`` of deliberate holds)."""
    stretches: list[tuple[float, float]] = []
    run_start: float | None = None
    last = 0.0
    for t, value in series:
        if value < floor:
            if run_start is None:
                run_start = t
            last = t
        else:
            if run_start is not None and last - run_start > max_s:
                stretches.append((run_start, last))
            run_start = None
    if run_start is not None and last - run_start > max_s:
        stretches.append((run_start, last))
    return [
        (a, b) for a, b in stretches if not any(h0 <= a and b <= h1 for h0, h1 in holds)
    ]


def second_means(
    series: Sequence[tuple[float, float]], start_s: float, end_s: float
) -> list[float]:
    """Mean YAVG of each whole second in ``[start_s, end_s)``."""
    means: list[float] = []
    second = start_s
    while second + 1.0 <= end_s + 1e-9:
        values = [v for t, v in series if second <= t < second + 1.0]
        if values:
            means.append(sum(values) / len(values))
        second += 1.0
    return means


def analyze_motion(
    series: Sequence[tuple[float, float]],
    scenes: Sequence[tuple[str, float, float]],
    holds: Sequence[tuple[float, float]] = (),
) -> tuple[list[str], dict[str, Any]]:
    """Problems and facts: no frozen stretch, and enough moving seconds per scene."""
    problems: list[str] = []
    frozen = frozen_stretches(series, holds)
    for a, b in frozen:
        problems.append(f"the picture is frozen from {a:.2f} s to {b:.2f} s")
    per_scene: dict[str, Any] = {}
    for scene_id, start_s, end_s in scenes:
        means = second_means(series, start_s, end_s)
        moving = sum(1 for m in means if m >= MOTION_SECOND_MIN)
        share = moving / len(means) if means else 0.0
        per_scene[scene_id] = {
            "seconds": len(means),
            "moving_seconds": moving,
            "moving_share": round(share, 3),
            "mean_yavg": round(sum(means) / len(means), 3) if means else 0.0,
        }
        if share < SCENE_MOVING_SECONDS_MIN:
            problems.append(
                f"scene '{scene_id}' moves in only {share:.0%} of its seconds "
                f"(need {SCENE_MOVING_SECONDS_MIN:.0%})"
            )
    values = [v for _, v in series]
    facts = {
        "frames_measured": len(series),
        "mean_yavg": round(sum(values) / len(values), 3) if values else 0.0,
        "frozen_stretches": [[round(a, 2), round(b, 2)] for a, b in frozen],
        "scenes": per_scene,
        "thresholds": {
            "second_min_yavg": MOTION_SECOND_MIN,
            "frozen_frame_max_yavg": FROZEN_FRAME_MAX,
            "frozen_max_s": FROZEN_MAX_S,
            "scene_moving_share_min": SCENE_MOVING_SECONDS_MIN,
        },
    }
    return problems, facts


# ---------------------------------------------------------------------------
# Scene distinctness
# ---------------------------------------------------------------------------


def thumbnail_command(
    video: Path, t_s: float, ffmpeg_binary: str = "ffmpeg"
) -> list[str]:
    width, height = THUMB_SIZE
    return [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{t_s:.3f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-vf",
        f"scale={width}:{height}:flags=area",
        "-pix_fmt",
        "rgb24",
        "-f",
        "rawvideo",
        "-",
    ]


def thumbnail(
    video: Path,
    t_s: float,
    ffmpeg_binary: str = "ffmpeg",
    *,
    run: RunProcess = subprocess.run,
) -> bytes:
    proc = run(
        thumbnail_command(video, t_s, ffmpeg_binary), capture_output=True, check=False
    )
    expected = THUMB_SIZE[0] * THUMB_SIZE[1] * 3
    data = proc.stdout
    if proc.returncode != 0 or len(data) != expected:
        raise AnimationError(f"could not read a thumbnail at {t_s:.2f} s")
    return bytes(data)


def colour_histogram(rgb: bytes, bins: int = HISTOGRAM_BINS) -> list[float]:
    """Normalised ``bins``^3 colour histogram of raw RGB bytes."""
    counts = [0] * bins**3
    step = 256 // bins
    for i in range(0, len(rgb) - 2, 3):
        counts[
            (rgb[i] // step * bins + rgb[i + 1] // step) * bins + rgb[i + 2] // step
        ] += 1
    total = sum(counts) or 1
    return [c / total for c in counts]


def histogram_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """Half the L1 distance of two normalised histograms: 0 identical, 1 disjoint."""
    return 0.5 * sum(abs(x - y) for x, y in zip(a, b, strict=True))


def analyze_scene_distinctness(
    histograms: dict[str, Sequence[float]], order: Sequence[str]
) -> tuple[list[str], dict[str, Any]]:
    """Adjacent scenes must look different; all pairwise distances are reported."""
    problems: list[str] = []
    pairs: dict[str, float] = {}
    for i, first in enumerate(order):
        for second in order[i + 1 :]:
            pairs[f"{first}|{second}"] = round(
                histogram_distance(histograms[first], histograms[second]), 3
            )
    for first, second in zip(order, order[1:], strict=False):
        distance = pairs[f"{first}|{second}"]
        if distance < SCENE_DISTANCE_MIN:
            problems.append(
                f"scenes '{first}' and '{second}' look alike "
                f"(distance {distance:.2f}, need {SCENE_DISTANCE_MIN})"
            )
    return problems, {"distances": pairs, "min_adjacent": SCENE_DISTANCE_MIN}


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def _intersects(a: Box, b: Box) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _contains(outer: Box, inner: Box) -> bool:
    return (
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and inner[2] <= outer[2]
        and inner[3] <= outer[3]
    )


def _area(box: Box) -> int:
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


@dataclass
class CompositionTally:
    """Streaming composition checks over the ``FrameMeta`` of every frame."""

    size: tuple[int, int]
    safe_area: Box
    frames: int = 0
    caption_frames: int = 0
    face_caption_frames: int = 0
    face_clear_frames: int = 0
    text_frames: int = 0
    max_text_share: float = 0.0
    min_character_to_text: float = math.inf
    text_ratio_violations: int = 0
    caption_outside_safe: int = 0
    first_bad: list[str] = field(default_factory=list)

    def add(self, meta: Any) -> None:
        self.frames += 1
        frame_area = self.size[0] * self.size[1]
        caption: Box | None = tuple(meta.caption) if meta.caption else None  # type: ignore[assignment]
        if caption is not None:
            self.caption_frames += 1
            if not _contains(self.safe_area, caption):
                self.caption_outside_safe += 1
                self._note(f"frame {meta.frame}: the caption leaves the safe area")
            if meta.faces:
                self.face_caption_frames += 1
                hit = [
                    f.name
                    for f in meta.faces
                    if _intersects((f.x0, f.y0, f.x1, f.y1), caption)
                ]
                if hit:
                    self._note(f"frame {meta.frame}: face {hit} under the caption")
                else:
                    self.face_clear_frames += 1
        text_area = sum(_area((b.x0, b.y0, b.x1, b.y1)) for b in meta.text_boxes)
        if text_area:
            self.text_frames += 1
            self.max_text_share = max(self.max_text_share, text_area / frame_area)
            character_area = sum(
                _area((b.x0, b.y0, b.x1, b.y1)) for b in meta.characters
            )
            ratio = character_area / text_area
            self.min_character_to_text = min(self.min_character_to_text, ratio)
            if ratio < CHARACTER_TO_TEXT_MIN:
                self.text_ratio_violations += 1
                self._note(
                    f"frame {meta.frame}: characters are only {ratio:.1f}x "
                    "the in-world text"
                )

    def _note(self, message: str) -> None:
        if len(self.first_bad) < 8:
            self.first_bad.append(message)

    def report(self) -> tuple[list[str], dict[str, Any]]:
        problems: list[str] = []
        clear_share = (
            self.face_clear_frames / self.face_caption_frames
            if self.face_caption_frames
            else 1.0
        )
        if clear_share < FACE_CLEAR_MIN:
            problems.append(
                f"a face is under the caption in {1 - clear_share:.1%} of the frames "
                f"with both (limit {1 - FACE_CLEAR_MIN:.0%})"
            )
        if self.max_text_share > TEXT_AREA_MAX:
            problems.append(
                f"in-world text covers {self.max_text_share:.1%} of a frame "
                f"(limit {TEXT_AREA_MAX:.0%})"
            )
        if self.text_ratio_violations:
            problems.append(
                f"in {self.text_ratio_violations} frames the characters are less than "
                f"{CHARACTER_TO_TEXT_MIN:g}x the in-world text area"
            )
        if self.caption_outside_safe:
            problems.append(
                f"the caption leaves the safe area in "
                f"{self.caption_outside_safe} frames"
            )
        facts = {
            "frames": self.frames,
            "caption_frames": self.caption_frames,
            "frames_with_face_and_caption": self.face_caption_frames,
            "face_clear_share": round(clear_share, 4),
            "text_frames": self.text_frames,
            "max_text_share": round(self.max_text_share, 4),
            "min_character_to_text_area": (
                None
                if math.isinf(self.min_character_to_text)
                else round(self.min_character_to_text, 1)
            ),
            "caption_outside_safe_frames": self.caption_outside_safe,
            "examples": list(self.first_bad),
            "thresholds": {
                "face_clear_min": FACE_CLEAR_MIN,
                "text_area_max": TEXT_AREA_MAX,
                "character_to_text_min": CHARACTER_TO_TEXT_MIN,
            },
        }
        return problems, facts


# ---------------------------------------------------------------------------
# The whole report
# ---------------------------------------------------------------------------


@dataclass
class ValidationReport:
    """Named checks, each with its problems and facts; ``strict`` decides what fails."""

    checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    hard: tuple[str, ...] = ("container", "decode")

    def add(self, name: str, problems: Sequence[str], facts: dict[str, Any]) -> None:
        self.checks[name] = {
            "passed": not problems,
            "problems": list(problems),
            **facts,
        }

    @property
    def problems(self) -> list[str]:
        return [
            f"{name}: {p}" for name, c in self.checks.items() for p in c["problems"]
        ]

    def failed(self, strict: bool) -> list[str]:
        """Problems that fail the step: hard checks, or every check if ``strict``."""
        return [
            f"{name}: {p}"
            for name, c in self.checks.items()
            if strict or name in self.hard
            for p in c["problems"]
        ]

    def as_dict(self, strict: bool) -> dict[str, Any]:
        failed = self.failed(strict)
        return {
            "passed": not failed,
            "strict": strict,
            "all_checks_passed": not self.problems,
            "warnings": [p for p in self.problems if p not in failed],
            "failed": failed,
            "checks": self.checks,
        }
