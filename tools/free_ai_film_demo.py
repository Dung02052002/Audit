"""Free AI film demo: one short Vietnamese cartoon clip made with free tools only.

Development tool, not part of the package (BUDGET-01 Goal B). It shows the
whole free path end to end and never calls a paid API:

1. ``budget_check``: the paid-API budget layer (``PaidRequestGuard``) must say
   that every provider of the demo is free (status ``free``).
2. ``background_image``: one AI background from a local ComfyUI server
   (Stable Diffusion 1.5). When ComfyUI is unreachable the step fails; there is
   never a placeholder image.
3. ``voice``: Vietnamese speech from Microsoft Edge TTS (a free online
   service, not local), checked as MP3 and transcoded to PCM WAV.
4. ``envelope_subtitles``: the loudness envelope of the voice (it drives the
   mouth) and the subtitle cues.
5. ``animate``: software 2D animation drawn frame by frame with Pillow (blink,
   waving arm, body bob, head tilt, mouth, steam, swaying sign, slow pan) and
   streamed to ffmpeg. This is NOT an AI video model.
6. ``validate``: the MP4 is checked with the package MP4 probe and ffprobe.
7. ``manifest``: ``manifest.json`` with the methods, the cost (0), the budget
   status and every step.

Run (the main session runs it against a live ComfyUI)::

    uv run --extra tts --extra demo python tools/free_ai_film_demo.py \
        [--out-root artifacts/free_ai_film_demo] [--comfy-url http://127.0.0.1:8188] \
        [--voice vi-VN-HoaiMyNeural] [--size 720x1280] [--fps 24]

Every step prints PENDING/RUNNING/SUCCESS/FAILED/BLOCKED as it goes and the
lines are also written to ``run.log`` in the run directory. The overall result
is SUCCESS only when every step is SUCCESS.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import wave
from array import array
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

DEFAULT_OUT_ROOT = Path("artifacts/free_ai_film_demo")
DEFAULT_COMFY_URL = "http://127.0.0.1:8188"
DEFAULT_VOICE = "vi-VN-HoaiMyNeural"
DEFAULT_CHECKPOINT = "v1-5-pruned-emaonly.safetensors"
DEFAULT_SIZE = (720, 1280)
DEFAULT_FPS = 24
# SD 1.5 portrait background (the package refuses sides below 640 px).
BACKGROUND_SIZE = (640, 960)
SAMPLE_RATE = 24000
TAIL_S = 0.6
VOICE_MIN_S, VOICE_MAX_S = 10.0, 20.0
VIDEO_MIN_S, VIDEO_MAX_S = 10.0, 20.0
EDGE_ATTEMPTS = 3

SCRIPT_TEXT = (
    "Xin chào các bạn! Mình là Mai, chủ của Quán Ăn Nhỏ ở góc phố này. "
    "Mười năm trước, mẹ mình mở quán chỉ với một nồi phở và chiếc bàn gỗ cũ. "
    "Đến hôm nay, mỗi bát phở vẫn nấu theo công thức của mẹ. "
    "Ghé quán ăn thử nhé!"
)
SIGN_TEXT = "Quán Ăn Nhỏ"
PANEL_TEXT = "PHỞ BÒ · PHỞ GÀ"
BACKGROUND_PROMPT = (
    "cute cartoon illustration background of a small Vietnamese street food "
    "eatery, empty shop front, wooden tables and red plastic stools, warm paper "
    "lanterns, potted plants, old Hanoi street, golden hour light, soft pastel "
    "colors, flat shading, children's picture book style, no people, vertical "
    "composition"
)
BACKGROUND_STYLE = "cartoon illustration"
BACKGROUND_SEED_KEY = "2026"

MOTION_METHOD = (
    "Hoạt hình 2D bằng phần mềm (Pillow, từng khung hình) — KHÔNG phải mô hình video AI"
)
SUBTITLE_METHOD = "vẽ bằng Pillow"
PREVIOUS_STEP_FAILED = "bước trước thất bại"

STEP_NAMES = (
    "budget_check",
    "background_image",
    "voice",
    "envelope_subtitles",
    "animate",
    "validate",
    "manifest",
)

WINDOWS_FONTS = Path("C:/Windows/Fonts")
# (regular, bold) candidates; both have Vietnamese glyphs.
FONT_CANDIDATES = (
    ("arial.ttf", "arialbd.ttf"),
    ("segoeui.ttf", "segoeuib.ttf"),
)
VIETNAMESE_PROBE = "ăâđêôơưạảấầệợữựỹ"


def background_method(checkpoint: str) -> str:
    return f"Ảnh AI — ComfyUI cục bộ, Stable Diffusion 1.5 ({checkpoint})"


def voice_method(voice: str) -> str:
    return (
        f"Microsoft Edge TTS {voice} (dịch vụ trực tuyến miễn phí, không phải cục bộ)"
    )


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


class StepStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class StepBlocked(Exception):
    """A step cannot run for a reason that is not a failure of its own work."""


@dataclass
class StepRecord:
    name: str
    status: StepStatus = StepStatus.PENDING
    started_at: str | None = None
    finished_at: str | None = None
    output: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "output": self.output,
            "detail": self.detail,
        }


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class RunLog:
    """Prints status lines and appends them to the run log file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path

    def __call__(self, message: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        print(line, flush=True)
        if self.path is not None:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")


class StepRunner:
    """Runs named steps in order; a failed or blocked step blocks the rest.

    A step is SUCCESS only when its function returned an existing path and
    ``validate(path)`` passed (did not raise and did not return False).
    """

    def __init__(
        self, names: Sequence[str], *, log: Callable[[str], None] = print
    ) -> None:
        self.records = {name: StepRecord(name) for name in names}
        self._log = log
        for name in names:
            self._emit(self.records[name])

    def _emit(self, record: StepRecord) -> None:
        text = f"{record.status.value:<8} {record.name}"
        if record.output:
            text += f" -> {record.output}"
        if record.detail:
            text += f" ({record.detail})"
        self._log(text)

    def _upstream_failure(self, name: str) -> str | None:
        for other in self.records.values():
            if other.name == name:
                return None
            if other.status in (StepStatus.FAILED, StepStatus.BLOCKED):
                return other.name
        return None

    def run(
        self,
        name: str,
        fn: Callable[[], Path],
        validate: Callable[[Path], object] | None = None,
        *,
        force: bool = False,
    ) -> Path | None:
        record = self.records[name]
        failed = None if force else self._upstream_failure(name)
        if failed is not None:
            record.status = StepStatus.BLOCKED
            record.detail = f"{PREVIOUS_STEP_FAILED}: {failed}"
            record.started_at = record.finished_at = _now()
            self._emit(record)
            return None
        record.status = StepStatus.RUNNING
        record.started_at = _now()
        self._emit(record)
        try:
            output = fn()
            if not isinstance(output, Path) or not output.exists():
                raise RuntimeError(f"the step produced no file ({output!r})")
            record.output = str(output)
            if validate is not None and validate(output) is False:
                raise RuntimeError("the output did not pass validation")
        except StepBlocked as exc:
            record.status = StepStatus.BLOCKED
            record.detail = str(exc)
        except Exception as exc:
            record.status = StepStatus.FAILED
            record.detail = f"{type(exc).__name__}: {exc}"
        else:
            record.status = StepStatus.SUCCESS
        record.finished_at = _now()
        self._emit(record)
        return output if record.status is StepStatus.SUCCESS else None

    @property
    def ok(self) -> bool:
        return all(r.status is StepStatus.SUCCESS for r in self.records.values())

    def as_list(self) -> list[dict[str, Any]]:
        return [record.as_dict() for record in self.records.values()]


@dataclass
class DemoState:
    """What the steps found out, for the manifest."""

    budget: dict[str, Any] = field(default_factory=dict)
    comfy: dict[str, Any] = field(default_factory=dict)
    background_path: Path | None = None
    voice_mp3: Path | None = None
    voice_wav: Path | None = None
    voice_duration_ms: int | None = None
    envelope: list[float] = field(default_factory=list)
    cues: tuple[SubtitleCue, ...] = ()
    font_files: FontFiles | None = None
    frames: int | None = None
    mp4_path: Path | None = None
    mp4_summary: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Budget check (Goal A layer, imported lazily so a missing layer is a FAILED
# step, never a silent skip)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetLayer:
    build_container: Callable[..., Any]
    prepare_database: Callable[..., Any]
    guard_type: type
    decision_error: type[BaseException]


def import_budget_layer() -> BudgetLayer:
    # The guard first: without the budget layer this import fails on its own.
    from ai_youtube_agent.pipeline.paid_request_guard import PaidRequestGuard

    from ai_youtube_agent.bootstrap import build_container, prepare_database
    from ai_youtube_agent.content.paid_api_budget import PaidRequestDecisionError

    return BudgetLayer(
        build_container, prepare_database, PaidRequestGuard, PaidRequestDecisionError
    )


def _plain(value: Any) -> Any:
    """JSON-friendly copy of enums, decimals, dates and dataclasses."""
    if value is None or isinstance(value, bool | int | float | str):
        return getattr(value, "value", value)
    if hasattr(value, "value") and not callable(value.value):
        return _plain(value.value)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_plain(v) for v in value]
    if hasattr(value, "__dataclass_fields__"):
        return {
            name: _plain(getattr(value, name)) for name in value.__dataclass_fields__
        }
    return str(value)


def run_budget_check(
    settings: Any, providers: Sequence[Any], run_dir: Path, state: DemoState
) -> Path:
    """Ask the paid-API guard about the demo providers; only ``free`` passes."""
    layer = import_budget_layer()
    container = layer.build_container(settings)
    layer.prepare_database(container)
    guard = container.resolve(layer.guard_type)
    try:
        result = guard.preflight(list(providers))
    except layer.decision_error as exc:
        message = getattr(exc, "user_message", None) or str(exc)
        raise StepBlocked(f"{getattr(exc, 'code', 'budget')}: {message}") from exc
    state.budget = {
        "mode": _plain(result.mode),
        "status": _plain(result.status),
        "declarations": _plain(list(result.declarations)),
    }
    path = run_dir / "budget_check.json"
    path.write_text(
        json.dumps(state.budget, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if state.budget["status"] != "free":
        raise StepBlocked(
            f"ngân sách trả về '{state.budget['status']}', không phải 'free'"
        )
    return path


def validate_budget(path: Path) -> bool:
    return json.loads(path.read_text(encoding="utf-8")).get("status") == "free"


# ---------------------------------------------------------------------------
# Background image (ComfyUI)
# ---------------------------------------------------------------------------


def run_background(comfy: Any, run_dir: Path, state: DemoState) -> Path:
    from ai_youtube_agent.providers.image_generation import ImageGenerationRequest

    request = ImageGenerationRequest(
        scene_id="free_ai_film_demo",
        shot_id="background",
        prompt=BACKGROUND_PROMPT,
        width=BACKGROUND_SIZE[0],
        height=BACKGROUND_SIZE[1],
        style=BACKGROUND_STYLE,
        seed_key=BACKGROUND_SEED_KEY,
    )
    _image, _record, img_path, meta_path = comfy.save_to_directory(
        request, run_dir / "background"
    )
    job = comfy.last_job
    state.comfy = {
        "seed": job.seed,
        "prompt_id": job.prompt_id,
        "checkpoint": job.checkpoint,
        "metadata": str(meta_path),
    }
    state.background_path = img_path
    return img_path


def validate_image(path: Path) -> None:
    from ai_youtube_agent.providers.media_probe import probe_image_bytes

    probe_image_bytes(path.read_bytes())


# ---------------------------------------------------------------------------
# Voice (Edge TTS)
# ---------------------------------------------------------------------------


def wav_duration_ms(path: Path) -> int:
    with wave.open(str(path), "rb") as reader:
        frames, rate = reader.getnframes(), reader.getframerate()
    if frames <= 0 or rate <= 0:
        raise ValueError(f"{path.name} has no PCM frames")
    return round(frames * 1000 / rate)


def transcode_to_wav(ffmpeg_binary: str, mp3: Path, wav_path: Path) -> Path:
    proc = subprocess.run(
        [
            ffmpeg_binary,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(mp3),
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(wav_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not wav_path.exists():
        raise RuntimeError(f"ffmpeg could not make the WAV: {proc.stderr.strip()}")
    return wav_path


def run_voice(
    edge: Any,
    voice: str,
    ffmpeg_binary: str,
    run_dir: Path,
    state: DemoState,
    *,
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> Path:
    """One Edge TTS request (MP3), then a local ffmpeg transcode to WAV."""
    from ai_youtube_agent.providers.media_probe import probe_mp3_bytes
    from ai_youtube_agent.providers.speech_synthesis import (
        SpeechRequest,
        SpeechSynthesisError,
    )

    request = SpeechRequest(text=SCRIPT_TEXT, voice_id=voice, language="vi-VN")
    mp3_path = run_dir / "voice.mp3"
    for attempt in range(1, EDGE_ATTEMPTS + 1):
        try:
            _speech, mp3_path = edge.synthesize_to_file(request, mp3_path)
            break
        except SpeechSynthesisError as exc:
            if not exc.retryable or attempt == EDGE_ATTEMPTS:
                raise
            log(f"         voice: Edge TTS attempt {attempt} failed ({exc}); retry")
            sleep(2.0 * attempt)
    probe_mp3_bytes(mp3_path.read_bytes())
    state.voice_mp3 = mp3_path
    wav_path = transcode_to_wav(ffmpeg_binary, mp3_path, run_dir / "voice.wav")
    duration_ms = wav_duration_ms(wav_path)
    state.voice_wav = wav_path
    state.voice_duration_ms = duration_ms
    seconds = duration_ms / 1000
    if not VOICE_MIN_S <= seconds <= VOICE_MAX_S:
        raise RuntimeError(
            f"the voice lasts {seconds:.2f} s, "
            f"outside {VOICE_MIN_S:g}-{VOICE_MAX_S:g} s"
        )
    return wav_path


# ---------------------------------------------------------------------------
# Envelope and subtitles (pure)
# ---------------------------------------------------------------------------


def rms_envelope(
    wav_path: Path, fps: int, *, attack: float = 0.6, release: float = 0.22
) -> list[float]:
    """Per-video-frame loudness in [0, 1] of a 16-bit PCM WAV.

    RMS per frame window, normalised by the 95th percentile (so a few loud
    peaks do not flatten the rest), then smoothed with a fast attack and a
    slower release so the mouth opens quickly and closes softly.
    """
    with wave.open(str(wav_path), "rb") as reader:
        channels = reader.getnchannels()
        width = reader.getsampwidth()
        rate = reader.getframerate()
        raw = reader.readframes(reader.getnframes())
    if width != 2:
        raise ValueError("rms_envelope needs 16-bit PCM")
    samples = array("h")
    samples.frombytes(raw[: len(raw) - len(raw) % 2])
    if sys.byteorder == "big":
        samples.byteswap()
    if channels > 1:
        samples = samples[::channels]
    if not samples:
        return []
    per_frame = rate / fps
    count = math.ceil(len(samples) / per_frame)
    rms: list[float] = []
    for index in range(count):
        window = samples[int(index * per_frame) : int((index + 1) * per_frame)]
        if not window:
            rms.append(0.0)
            continue
        rms.append(math.sqrt(sum(v * v for v in window) / len(window)))
    reference = sorted(rms)[int(0.95 * (len(rms) - 1))]
    if reference <= 0:
        return [0.0] * count
    smoothed: list[float] = []
    level = 0.0
    for value in rms:
        target = min(1.0, value / reference)
        rate_ = attack if target > level else release
        level += (target - level) * rate_
        smoothed.append(round(level, 4))
    return smoothed


def mouth_openness(
    envelope: Sequence[float], index: int, *, gate: float = 0.08
) -> float:
    """How open the mouth is (0 closed .. 1 wide) on video frame ``index``."""
    if not 0 <= index < len(envelope):
        return 0.0
    value = envelope[index]
    if value <= gate:
        return 0.0
    return min(1.0, ((value - gate) / (1.0 - gate)) ** 0.8)


@dataclass(frozen=True)
class SubtitleCue:
    start_ms: int
    end_ms: int
    text: str

    def as_dict(self) -> dict[str, Any]:
        return {"start_ms": self.start_ms, "end_ms": self.end_ms, "text": self.text}


def split_sentences(text: str) -> list[str]:
    return [part for part in re.split(r"(?<=[.!?…])\s+", text.strip()) if part]


def subtitle_cues(text: str, total_ms: int) -> tuple[SubtitleCue, ...]:
    """One cue per sentence; each lasts in proportion to its characters."""
    sentences = split_sentences(text)
    if not sentences or total_ms <= 0:
        return ()
    weights = [len(sentence.replace(" ", "")) for sentence in sentences]
    total_weight = sum(weights)
    cues: list[SubtitleCue] = []
    start = 0
    running = 0
    for sentence, weight in zip(sentences, weights, strict=True):
        running += weight
        end = round(total_ms * running / total_weight)
        cues.append(SubtitleCue(start, end, sentence))
        start = end
    return tuple(cues)


def cue_at(cues: Sequence[SubtitleCue], t_ms: float) -> str | None:
    for cue in cues:
        if cue.start_ms <= t_ms < cue.end_ms:
            return cue.text
    return None


def _srt_time(ms: int) -> str:
    return (
        f"{ms // 3_600_000:02d}:{(ms // 60_000) % 60:02d}:"
        f"{(ms // 1000) % 60:02d},{ms % 1000:03d}"
    )


def write_srt(cues: Sequence[SubtitleCue], path: Path) -> Path:
    blocks = [
        f"{index}\n{_srt_time(cue.start_ms)} --> {_srt_time(cue.end_ms)}\n{cue.text}\n"
        for index, cue in enumerate(cues, 1)
    ]
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


def frame_count(duration_ms: int, fps: int) -> int:
    return math.ceil((duration_ms / 1000 + TAIL_S) * fps)


def run_envelope_subtitles(fps: int, run_dir: Path, state: DemoState) -> Path:
    if state.voice_wav is None or state.voice_duration_ms is None:
        raise RuntimeError("no voice WAV")
    state.envelope = rms_envelope(state.voice_wav, fps)
    state.cues = subtitle_cues(SCRIPT_TEXT, state.voice_duration_ms)
    state.frames = frame_count(state.voice_duration_ms, fps)
    write_srt(state.cues, run_dir / "subtitles.vi.srt")
    path = run_dir / "timeline.json"
    path.write_text(
        json.dumps(
            {
                "fps": fps,
                "frames": state.frames,
                "voice_duration_ms": state.voice_duration_ms,
                "cues": [cue.as_dict() for cue in state.cues],
                "envelope": state.envelope,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    return path


def validate_timeline(path: Path) -> bool:
    data = json.loads(path.read_text(encoding="utf-8"))
    return bool(data["envelope"]) and bool(data["cues"]) and data["frames"] > 0


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FontFiles:
    regular: Path
    bold: Path


def font_supports_vietnamese(path: Path) -> bool:
    """True when the font has real glyphs (not .notdef) for Vietnamese letters."""
    from PIL import ImageFont

    font = ImageFont.truetype(str(path), 32)
    missing = font.getmask("\u0378")  # unassigned code point: always .notdef
    missing_key = (missing.size, bytes(missing))
    for char in VIETNAMESE_PROBE:
        mask = font.getmask(char)
        if (mask.size, bytes(mask)) == missing_key:
            return False
    return True


def resolve_font_files(
    configured: Path | None, *, fonts_dir: Path = WINDOWS_FONTS
) -> FontFiles:
    """The configured font, else a Windows font; StepBlocked when none fits.

    ``configured`` is ``Settings.render_font_file``
    (``AI_YOUTUBE_AGENT_RENDER_FONT_FILE``). The Pillow default bitmap font is
    never used: it has no Vietnamese glyphs.
    """
    if configured is not None:
        path = Path(configured)
        if not path.is_file():
            raise StepBlocked(f"không tìm thấy phông chữ đã cấu hình: {path}")
        candidates = [(path, path)]
    else:
        candidates = [
            (
                fonts_dir / regular,
                fonts_dir / bold
                if (fonts_dir / bold).is_file()
                else fonts_dir / regular,
            )
            for regular, bold in FONT_CANDIDATES
            if (fonts_dir / regular).is_file()
        ]
    for regular, bold in candidates:
        try:
            if font_supports_vietnamese(regular) and font_supports_vietnamese(bold):
                return FontFiles(regular, bold)
        except OSError:
            continue
    raise StepBlocked(
        "không có phông chữ TrueType hỗ trợ tiếng Việt; hãy đặt "
        "AI_YOUTUBE_AGENT_RENDER_FONT_FILE"
    )


# ---------------------------------------------------------------------------
# Drawing (Pillow, imported lazily)
# ---------------------------------------------------------------------------

REF_W, REF_H = 720, 1280
SS = 2  # supersampling of the drawn layers (anti-aliasing)
OUTLINE = (59, 36, 24, 255)
OW = 4.5  # outline width in reference pixels
SKIN = (251, 217, 185, 255)
SKIN_SHADE = (232, 170, 140, 255)
HAIR = (43, 27, 23, 255)
WHITE = (255, 255, 255, 255)
SHIRT = (240, 128, 74, 255)
APRON = (255, 250, 240, 255)
CHEEK = (252, 170, 165, 255)
EYE = (42, 26, 20, 255)
MOUTH = (142, 42, 42, 255)
TONGUE = (242, 124, 134, 255)
WOOD = (217, 160, 102, 255)
WOOD_DARK = (176, 106, 52, 255)
WOOD_LIGHT = (232, 176, 112, 255)
PANEL = (46, 125, 116, 255)
PANEL_LIGHT = (78, 160, 148, 255)
CREAM = (255, 243, 214, 255)
AWNING_RED = (217, 67, 59, 255)
AWNING_CREAM = (255, 244, 224, 255)
BOWL_BLUE = (61, 111, 182, 255)
BROTH = (233, 176, 75, 255)
BEEF = (160, 82, 45, 255)
HERB = (76, 175, 80, 255)

# Layer boxes in reference coordinates (x0, y0, x1, y1).
CHAR_BOX = (90, 330, 570, 920)
HEAD_BOX = (190, 330, 470, 720)
SIGN_BOX = (130, 100, 590, 330)
SIGN_PIVOT = (360, 128)
ROPE_TOPS = ((200, 118), (520, 118))
ROPE_HOOKS = ((200, 196), (520, 196))
COUNTER_BOX = (0, 780, 720, 1280)
AWNING_BOX = (0, 0, 720, 190)
STEAM_BOX = (500, 600, 700, 850)
SUBTITLE_CENTER_Y = 1150
SUBTITLE_MAX_W = 620


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _smoothstep(edge0: float, edge1: float, x: float) -> float:
    if edge1 == edge0:
        return 1.0 if x >= edge1 else 0.0
    t = min(1.0, max(0.0, (x - edge0) / (edge1 - edge0)))
    return t * t * (3 - 2 * t)


class _Pen:
    """Draws in reference coordinates on a layer with origin and scale."""

    def __init__(
        self, image: Any, origin: tuple[float, float], k: float, dy: float = 0.0
    ) -> None:
        from PIL import ImageDraw

        self.draw = ImageDraw.Draw(image)
        self.x0, self.y0 = origin
        self.k = k
        self.dy = dy

    def xy(self, x: float, y: float) -> tuple[float, float]:
        return ((x - self.x0) * self.k, (y + self.dy - self.y0) * self.k)

    def box(self, x0: float, y0: float, x1: float, y1: float) -> list[float]:
        a, b = self.xy(x0, y0), self.xy(x1, y1)
        return [a[0], a[1], b[0], b[1]]

    def w(self, value: float) -> int:
        return max(1, round(value * self.k))

    def ellipse(self, cx, cy, rx, ry, fill, outline=OUTLINE, width=OW) -> None:
        self.draw.ellipse(
            self.box(cx - rx, cy - ry, cx + rx, cy + ry),
            fill=fill,
            outline=outline,
            width=self.w(width),
        )

    def rounded(self, x0, y0, x1, y1, radius, fill, outline=OUTLINE, width=OW) -> None:
        self.draw.rounded_rectangle(
            self.box(x0, y0, x1, y1),
            radius=self.w(radius),
            fill=fill,
            outline=outline,
            width=self.w(width),
        )

    def chord(self, bbox, start, end, fill, outline=OUTLINE, width=OW) -> None:
        self.draw.chord(
            self.box(*bbox), start, end, fill=fill, outline=outline, width=self.w(width)
        )

    def arc(self, bbox, start, end, fill=OUTLINE, width=OW) -> None:
        self.draw.arc(self.box(*bbox), start, end, fill=fill, width=self.w(width))

    def line(self, points, fill, width) -> None:
        self.draw.line(
            [self.xy(x, y) for x, y in points],
            fill=fill,
            width=self.w(width),
            joint="curve",
        )

    def limb(self, points, width, fill) -> None:
        """A thick stroke with round caps and a dark outline."""
        self.line(points, OUTLINE, width + 2 * OW)
        for x, y in points:
            self.ellipse(x, y, width / 2 + OW, width / 2 + OW, OUTLINE, None)
        self.line(points, fill, width)
        for x, y in points:
            self.ellipse(x, y, width / 2, width / 2, fill, None)

    def blob(self, circles, fill) -> None:
        """The union of circles with one outline around it."""
        for x, y, r in circles:
            self.ellipse(x, y, r + OW, r + OW, OUTLINE, None)
        for x, y, r in circles:
            self.ellipse(x, y, r, r, fill, None)


@dataclass(frozen=True)
class CharacterPose:
    bob: float
    head_tilt: float
    eye_open: float
    mouth: float
    wave: float
    wave_swing: float
    present: float


def eye_openness(t: float, fps: int, *, period_s: float = 3.1) -> float:
    """1 open; a 2-frame blink (half, closed) about every ``period_s``."""
    frame = round(t * fps)
    period = max(3, round(period_s * fps))
    phase = (frame - round(0.8 * fps)) % period
    if frame < round(0.8 * fps):
        return 1.0
    return {0: 0.3, 1: 0.0}.get(phase, 1.0)


def wave_amount(t: float, duration_s: float) -> float:
    """How raised the waving arm is: hello at the start, goodbye at the end."""
    hello = 1.0 - _smoothstep(2.4, 3.2, t)
    goodbye = _smoothstep(duration_s - 2.6, duration_s - 1.8, t) if duration_s else 0
    return max(hello, goodbye)


def character_pose(
    t: float, mouth: float, *, duration_s: float, fps: int
) -> CharacterPose:
    tau = 2 * math.pi
    return CharacterPose(
        bob=5.0 * math.sin(tau * 0.8 * t) - 3.0 * mouth,
        head_tilt=3.5 * math.sin(tau * 0.33 * t + 0.6) + 1.5 * mouth,
        eye_open=eye_openness(t, fps),
        mouth=mouth,
        wave=wave_amount(t, duration_s),
        wave_swing=22.0 * math.sin(tau * 1.7 * t),
        present=7.0 * math.sin(tau * 0.5 * t + 1.0) + 10.0 * mouth,
    )


def _arm(shoulder, upper_deg, fore_deg, direction, upper=92.0, fore=84.0):
    """Elbow and hand from angles measured from straight down, outward > 0."""
    sx, sy = shoulder
    a1, a2 = math.radians(upper_deg), math.radians(fore_deg)
    elbow = (sx + direction * math.sin(a1) * upper, sy + math.cos(a1) * upper)
    hand = (
        elbow[0] + direction * math.sin(a2) * fore,
        elbow[1] + math.cos(a2) * fore,
    )
    return elbow, hand


def _draw_arm(pen: _Pen, shoulder, upper_deg, fore_deg, direction) -> None:
    elbow, hand = _arm(shoulder, upper_deg, fore_deg, direction)
    pen.limb([shoulder, elbow], 42, SHIRT)
    pen.limb([elbow, hand], 32, SKIN)
    pen.ellipse(hand[0], hand[1], 21, 21, SKIN)


def _draw_head(pen: _Pen, pose: CharacterPose) -> None:
    cx = 330
    # Hair behind the face, shoulder length.
    pen.ellipse(cx, 560, 140 + OW, 135 + OW, OUTLINE, None)
    pen.rounded(196 - OW, 560 - OW, 464 + OW, 700 + OW, 50 + OW, OUTLINE, None)
    pen.ellipse(cx, 560, 140, 135, HAIR, None)
    pen.rounded(196, 560, 464, 700, 50, HAIR, None)
    pen.ellipse(cx, 572, 116, 108, SKIN)
    # Bangs.
    pen.chord((208, 440, 452, 610), 180, 360, HAIR)
    for bx in (246, 288, 330, 372, 414):
        pen.ellipse(bx, 528, 22, 18, HAIR, None)
    # Chef hat.
    pen.blob([(276, 418, 44), (330, 392, 52), (384, 418, 44)], WHITE)
    pen.rounded(246, 430, 414, 472, 12, WHITE)
    # Brows rise a little with the voice.
    brow_y = 558 - 4 * pose.mouth
    for ex in (290, 370):
        pen.arc((ex - 18, brow_y - 8, ex + 18, brow_y + 8), 200, 340, width=5)
    # Eyes.
    for ex in (290, 370):
        if pose.eye_open >= 0.6:
            pen.ellipse(ex, 592, 13, 17, EYE, None)
            pen.ellipse(ex + 4, 585, 5, 5, WHITE, None)
            pen.ellipse(ex - 4, 598, 2.5, 2.5, WHITE, None)
        elif pose.eye_open > 0:
            pen.ellipse(ex, 594, 13, 17 * pose.eye_open, EYE, None)
        else:
            pen.arc((ex - 14, 584, ex + 14, 602), 0, 180, width=5)
    pen.ellipse(cx, 616, 6, 4, SKIN_SHADE, None)
    pen.ellipse(266, 628, 20, 12, CHEEK, None)
    pen.ellipse(394, 628, 20, 12, CHEEK, None)
    # Mouth: a smile when closed, an open mouth sized by the voice.
    if pose.mouth < 0.06:
        pen.arc((310, 618, 350, 646), 15, 165, width=5)
    else:
        half_w = 15 + 9 * pose.mouth
        half_h = 3 + 17 * pose.mouth
        my = 634 + half_h * 0.4
        pen.ellipse(cx, my, half_w, half_h, MOUTH, width=4)
        if pose.mouth > 0.25:
            pen.ellipse(
                cx, my + half_h * 0.5, half_w * 0.55, half_h * 0.35, TONGUE, None
            )


def _draw_character(pose: CharacterPose, s: float) -> Any:
    from PIL import Image

    k = s * SS
    x0, y0, x1, y1 = CHAR_BOX
    layer = Image.new(
        "RGBA", (round((x1 - x0) * k), round((y1 - y0) * k)), (0, 0, 0, 0)
    )
    pen = _Pen(layer, (x0, y0), k, pose.bob)
    pen.rounded(310, 655, 350, 730, 8, SKIN)  # neck
    pen.rounded(232, 712, 428, 990, 64, SHIRT)  # torso
    pen.line([(275, 794), (300, 716)], OUTLINE, 12 + 2 * OW)  # apron straps
    pen.line([(385, 794), (360, 716)], OUTLINE, 12 + 2 * OW)
    pen.line([(275, 794), (300, 716)], APRON, 12)
    pen.line([(385, 794), (360, 716)], APRON, 12)
    pen.rounded(262, 790, 398, 990, 26, APRON)
    pen.rounded(300, 842, 360, 884, 10, APRON, width=3.5)  # pocket
    # Waving arm (viewer's left) and presenting arm (viewer's right).
    w = pose.wave
    _draw_arm(
        pen,
        (246, 742),
        _lerp(15, 128, w),
        _lerp(-50, 160, w) + w * pose.wave_swing,
        -1,
    )
    _draw_arm(pen, (414, 742), 20 + pose.present * 0.3, 100 + pose.present, 1)

    hx0, hy0, hx1, hy1 = HEAD_BOX
    head = Image.new(
        "RGBA", (round((hx1 - hx0) * k), round((hy1 - hy0) * k)), (0, 0, 0, 0)
    )
    _draw_head(_Pen(head, (hx0, hy0), k, pose.bob), pose)
    pivot = ((330 - hx0) * k, (690 + pose.bob - hy0) * k)
    head = head.rotate(pose.head_tilt, resample=Image.Resampling.BILINEAR, center=pivot)
    layer.alpha_composite(head, (round((hx0 - x0) * k), round((hy0 - y0) * k)))
    return layer.resize(
        (round((x1 - x0) * s), round((y1 - y0) * s)), Image.Resampling.LANCZOS
    )


@dataclass
class Scene:
    """Everything that is drawn once; ``render_frame`` adds the motion."""

    size: tuple[int, int]
    s: float
    offset: tuple[int, int]
    background: Any
    awning: Any
    counter: Any
    sign: Any
    subtitle_font: Any
    shadow_lut: list[int]
    subtitle_cache: dict[str, Any] = field(default_factory=dict)

    def at(self, x: float, y: float) -> tuple[int, int]:
        return (
            self.offset[0] + round(x * self.s),
            self.offset[1] + round(y * self.s),
        )


def _font(fonts: FontFiles, size: float, *, bold: bool = True) -> Any:
    from PIL import ImageFont

    return ImageFont.truetype(
        str(fonts.bold if bold else fonts.regular), max(6, round(size))
    )


def _static_layer(box, s, paint) -> Any:
    from PIL import Image

    k = s * SS
    x0, y0, x1, y1 = box
    layer = Image.new(
        "RGBA", (round((x1 - x0) * k), round((y1 - y0) * k)), (0, 0, 0, 0)
    )
    paint(_Pen(layer, (x0, y0), k), k)
    return layer.resize(
        (round((x1 - x0) * s), round((y1 - y0) * s)), Image.Resampling.LANCZOS
    )


def _centered_text(pen: _Pen, k, cx, cy, text, font, fill, stroke) -> None:
    x, y = pen.xy(cx, cy)
    pen.draw.text(
        (x, y),
        text,
        font=font,
        fill=fill,
        anchor="mm",
        stroke_width=max(1, round(stroke * k)),
        stroke_fill=OUTLINE,
    )


def prepare_scene(background: Any, size: tuple[int, int], fonts: FontFiles) -> Scene:
    """Scale the AI background and draw the static stall parts once."""
    from PIL import Image, ImageFilter

    width, height = size
    s = min(width / REF_W, height / REF_H)
    offset = (round((width - REF_W * s) / 2), round((height - REF_H * s) / 2))

    bg = background.convert("RGB")
    cover = max(width * 1.08 / bg.width, height * 1.08 / bg.height)
    bg = bg.resize(
        (math.ceil(bg.width * cover), math.ceil(bg.height * cover)),
        Image.Resampling.LANCZOS,
    ).filter(ImageFilter.GaussianBlur(0.5 + 1.0 * s))

    def awning(pen: _Pen, k: float) -> None:
        for step in range(8):  # soft shadow under the awning
            alpha = round(70 * (1 - step / 8))
            y = 120 + step * 7
            pen.draw.rectangle(pen.box(0, y, 720, y + 7), fill=(20, 12, 8, alpha))
        for index in range(12):
            colour = AWNING_RED if index % 2 == 0 else AWNING_CREAM
            pen.ellipse(index * 60 + 30, 112, 30, 30, colour)
        for index in range(12):
            colour = AWNING_RED if index % 2 == 0 else AWNING_CREAM
            pen.draw.rectangle(
                pen.box(index * 60, 0, index * 60 + 60, 112), fill=colour
            )
        pen.rounded(-10, -10, 730, 24, 6, WOOD_DARK)

    def counter(pen: _Pen, k: float) -> None:
        pen.rounded(30, 906, 690, 1320, 20, PANEL)
        pen.draw.rectangle(pen.box(34, 930, 686, 940), fill=PANEL_LIGHT)
        pen.rounded(52, 1018, 668, 1300, 16, None, outline=PANEL_LIGHT, width=3)
        _centered_text(pen, k, 360, 978, PANEL_TEXT, _font(fonts, 50 * k), CREAM, 3)
        pen.rounded(14, 866, 706, 914, 14, WOOD)
        pen.draw.rectangle(pen.box(24, 874, 696, 880), fill=WOOD_LIGHT)
        # Bowl of pho.
        pen.rounded(560, 890, 620, 906, 6, WHITE)
        pen.chord((505, 800, 675, 900), 0, 180, WHITE)
        pen.arc((515, 812, 665, 888), 20, 160, fill=BOWL_BLUE, width=8)
        pen.ellipse(590, 852, 85, 15, WHITE)
        pen.ellipse(590, 852, 74, 10, BROTH, None)
        for nx in (548, 572, 604, 628):
            pen.arc((nx - 14, 846, nx + 14, 858), 200, 340, fill=CREAM, width=3)
        pen.ellipse(565, 851, 14, 5, BEEF, None)
        pen.ellipse(612, 853, 13, 5, BEEF, None)
        for hx, hy in ((585, 848), (595, 856), (632, 850), (550, 855)):
            pen.ellipse(hx, hy, 5, 3, HERB, None)
        pen.line([(612, 848), (712, 786)], OUTLINE, 9)
        pen.line([(612, 848), (712, 786)], WOOD_DARK, 5)
        pen.line([(626, 850), (720, 800)], OUTLINE, 9)
        pen.line([(626, 850), (720, 800)], WOOD_DARK, 5)

    def sign(pen: _Pen, k: float) -> None:
        pen.rounded(160, 190, 560, 308, 18, WOOD_DARK, width=6)
        pen.rounded(174, 202, 546, 296, 12, None, outline=WOOD_LIGHT, width=3)
        _centered_text(pen, k, 360, 249, SIGN_TEXT, _font(fonts, 62 * k), CREAM, 3)
        for hx, hy in ROPE_HOOKS:
            pen.ellipse(hx, hy, 6, 6, (90, 90, 90, 255), width=2)

    return Scene(
        size=size,
        s=s,
        offset=offset,
        background=bg,
        awning=_static_layer(AWNING_BOX, s, awning),
        counter=_static_layer(COUNTER_BOX, s, counter),
        sign=_static_layer(SIGN_BOX, s, sign),
        subtitle_font=_font(fonts, 44 * s),
        shadow_lut=[round(v * 0.38) for v in range(256)],
    )


def _rotate_point(point, pivot, degrees):
    """Where ``point`` goes when PIL rotates the image by ``degrees`` (CCW)."""
    a = math.radians(degrees)
    dx, dy = point[0] - pivot[0], point[1] - pivot[1]
    return (
        pivot[0] + dx * math.cos(a) + dy * math.sin(a),
        pivot[1] - dx * math.sin(a) + dy * math.cos(a),
    )


def _shadow(scene: Scene, layer: Any, blur: float) -> Any:
    from PIL import Image, ImageFilter

    shadow = Image.new("RGBA", layer.size, (20, 12, 8, 0))
    shadow.putalpha(layer.getchannel("A").point(scene.shadow_lut))
    return shadow.filter(ImageFilter.GaussianBlur(max(1.0, blur * scene.s)))


def wrap_text(text: str, font: Any, max_width: float) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and font.getlength(candidate) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _subtitle_image(scene: Scene, text: str) -> Any:
    from PIL import Image, ImageDraw

    cached = scene.subtitle_cache.get(text)
    if cached is not None:
        return cached
    font = scene.subtitle_font
    s = scene.s
    lines = wrap_text(text, font, SUBTITLE_MAX_W * s)
    ascent, descent = font.getmetrics()
    line_h = round((ascent + descent) * 1.12)
    pad_x, pad_y = round(26 * s), round(16 * s)
    text_w = max(font.getlength(line) for line in lines)
    box = Image.new(
        "RGBA",
        (round(text_w + 2 * pad_x), line_h * len(lines) + 2 * pad_y),
        (0, 0, 0, 0),
    )
    draw = ImageDraw.Draw(box)
    draw.rounded_rectangle(
        [0, 0, box.width - 1, box.height - 1],
        radius=max(2, round(22 * s)),
        fill=(18, 12, 8, 165),
    )
    for index, line in enumerate(lines):
        draw.text(
            (box.width / 2, pad_y + index * line_h + line_h / 2),
            line,
            font=font,
            fill=(255, 255, 255, 255),
            anchor="mm",
            stroke_width=max(1, round(4 * s)),
            stroke_fill=(26, 15, 10, 255),
        )
    scene.subtitle_cache[text] = box
    return box


def _steam(scene: Scene, t: float) -> Any:
    from PIL import Image, ImageDraw, ImageFilter

    s = scene.s
    x0, y0, x1, y1 = STEAM_BOX
    layer = Image.new(
        "RGBA", (round((x1 - x0) * s), round((y1 - y0) * s)), (0, 0, 0, 0)
    )
    draw = ImageDraw.Draw(layer)
    for index, base_x in enumerate((560, 592, 624)):
        phase = index * 2.1
        previous = None
        for step in range(26):
            v = step / 25
            y = 838 - v * 190
            x = base_x + 13 * (0.4 + v) * math.sin(
                2 * math.pi * (1.3 * v - 0.6 * t) + phase
            )
            point = ((x - x0) * s, (y - y0) * s)
            if previous is not None:
                pulse = 0.75 + 0.25 * math.sin(2 * math.pi * 0.4 * t + phase)
                alpha = round(200 * math.sin(math.pi * v) * pulse)
                draw.line(
                    [previous, point],
                    fill=(255, 255, 255, alpha),
                    width=max(1, round((10 + 9 * v) * s)),
                )
            previous = point
    return layer.filter(ImageFilter.GaussianBlur(max(1.0, 4.5 * s)))


def render_frame(
    t: float,
    scene: Scene,
    env_value: float,
    cue: str | None,
    *,
    duration_s: float,
    fps: int = DEFAULT_FPS,
) -> Any:
    """One RGB video frame at time ``t``; ``env_value`` is the mouth opening."""
    from PIL import Image

    width, height = scene.size
    s = scene.s
    progress = min(1.0, max(0.0, t / duration_s)) if duration_s else 0.0
    ease = 0.5 - 0.5 * math.cos(math.pi * progress)
    bg = scene.background
    pan_x = ease * (bg.width - width)
    pan_y = (bg.height - height) * (0.5 + 0.35 * math.sin(2 * math.pi * t / 11))
    frame = bg.transform(
        (width, height),
        Image.Transform.AFFINE,
        (1, 0, pan_x, 0, 1, pan_y),
        resample=Image.Resampling.BILINEAR,
    ).convert("RGBA")

    pose = character_pose(t, env_value, duration_s=duration_s, fps=fps)
    character = _draw_character(pose, s)
    frame.alpha_composite(
        _shadow(scene, character, 10), scene.at(CHAR_BOX[0] + 14, CHAR_BOX[1] + 18)
    )
    frame.alpha_composite(character, scene.at(CHAR_BOX[0], CHAR_BOX[1]))

    angle = 3.2 * math.sin(2 * math.pi * 0.45 * t)
    pivot_px = ((SIGN_PIVOT[0] - SIGN_BOX[0]) * s, (SIGN_PIVOT[1] - SIGN_BOX[1]) * s)
    sign = scene.sign.rotate(angle, resample=Image.Resampling.BICUBIC, center=pivot_px)
    frame.alpha_composite(
        _shadow(scene, sign, 6), scene.at(SIGN_BOX[0] + 8, SIGN_BOX[1] + 10)
    )
    pen = _Pen(frame, (-scene.offset[0] / s, -scene.offset[1] / s), s)
    for top, hook in zip(ROPE_TOPS, ROPE_HOOKS, strict=True):
        end = _rotate_point(hook, SIGN_PIVOT, angle)
        pen.line([top, end], (92, 62, 40, 255), 4)
    frame.alpha_composite(sign, scene.at(SIGN_BOX[0], SIGN_BOX[1]))
    frame.alpha_composite(scene.awning, scene.at(AWNING_BOX[0], AWNING_BOX[1]))
    frame.alpha_composite(scene.counter, scene.at(COUNTER_BOX[0], COUNTER_BOX[1]))
    frame.alpha_composite(_steam(scene, t), scene.at(STEAM_BOX[0], STEAM_BOX[1]))

    if cue:
        box = _subtitle_image(scene, cue)
        cx, cy = scene.at(REF_W / 2, SUBTITLE_CENTER_Y)
        frame.alpha_composite(box, (cx - box.width // 2, cy - box.height // 2))

    rgb = frame.convert("RGB")
    fade = min(1.0, t / 0.35, (duration_s - t) / 0.45) if duration_s else 1.0
    if fade < 1.0:
        rgb = Image.blend(Image.new("RGB", rgb.size, (0, 0, 0)), rgb, max(0.0, fade))
    return rgb


def iter_frames(
    scene: Scene,
    envelope: Sequence[float],
    cues: Sequence[SubtitleCue],
    frames: int,
    fps: int,
) -> Iterator[Any]:
    duration_s = frames / fps
    for index in range(frames):
        t = index / fps
        yield render_frame(
            t,
            scene,
            mouth_openness(envelope, index),
            cue_at(cues, t * 1000),
            duration_s=duration_s,
            fps=fps,
        )


# ---------------------------------------------------------------------------
# Encode and validate
# ---------------------------------------------------------------------------


def encode(
    frames: Iterable[Any],
    wav: Path,
    out: Path,
    *,
    size: tuple[int, int],
    fps: int,
    ffmpeg_binary: str = "ffmpeg",
    log_path: Path | None = None,
) -> Path:
    """Stream RGB frames to ffmpeg's stdin and mux them with the voice.

    Frames are written one at a time, never kept in memory. The voice is
    padded with silence (``apad``) and ``-shortest`` ends the file with the
    last video frame.
    """
    width, height = size
    command = [
        ffmpeg_binary,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{width}x{height}",
        "-r",
        str(fps),
        "-i",
        "-",
        "-i",
        str(wav),
        "-af",
        "apad",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-shortest",
        "-movflags",
        "+faststart",
        str(out),
    ]
    log_file = open(log_path, "ab") if log_path is not None else subprocess.DEVNULL  # noqa: SIM115
    try:
        proc = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log_file
        )
        written = 0
        try:
            for frame in frames:
                if frame.size != (width, height):
                    raise ValueError(f"frame {written} is {frame.size}, not {size}")
                proc.stdin.write(frame.convert("RGB").tobytes())
                written += 1
        except BrokenPipeError:
            pass
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        finally:
            if proc.stdin and not proc.stdin.closed:
                with contextlib.suppress(BrokenPipeError):
                    proc.stdin.close()
        code = proc.wait()
    finally:
        if log_path is not None:
            log_file.close()
    if code != 0 or not out.exists():
        tail = ""
        if log_path is not None and log_path.exists():
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-600:]
        raise RuntimeError(f"ffmpeg exited with {code} after {written} frames: {tail}")
    if written == 0:
        raise RuntimeError("no frames were rendered")
    return out


def ffprobe_binary(ffmpeg_binary: str) -> str:
    path = Path(ffmpeg_binary)
    if path.parent != Path("."):
        sibling = path.with_name(path.name.replace("ffmpeg", "ffprobe"))
        if sibling.exists():
            return str(sibling)
    return shutil.which("ffprobe") or "ffprobe"


def validate_mp4(
    path: Path,
    *,
    ffprobe: str = "ffprobe",
    expected_size: tuple[int, int] | None = None,
    min_s: float = VIDEO_MIN_S,
    max_s: float = VIDEO_MAX_S,
) -> dict[str, Any]:
    """Check the MP4 with the package probe and ffprobe; ValueError if bad."""
    from ai_youtube_agent.providers.video_render import probe_mp4

    data = path.read_bytes()
    info = probe_mp4(data)
    proc = subprocess.run(
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
        raise ValueError(f"ffprobe failed: {proc.stderr.strip()}")
    probe = json.loads(proc.stdout)
    streams = probe.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    problems = []
    if video is None or video.get("codec_name") != "h264":
        problems.append("no h264 video stream")
    if audio is None or audio.get("codec_name") != "aac":
        problems.append("no aac audio stream")
    if (
        video is not None
        and expected_size is not None
        and (video.get("width"), video.get("height")) != expected_size
    ):
        problems.append(f"video is {video.get('width')}x{video.get('height')}")
    duration = float(probe.get("format", {}).get("duration", 0.0))
    if not min_s <= duration <= max_s:
        problems.append(f"duration {duration:.2f} s outside {min_s:g}-{max_s:g} s")
    if not data:
        problems.append("empty file")
    if problems:
        raise ValueError("; ".join(problems))
    return {
        "path": str(path),
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "brands": list(info.brands),
        "duration_ms": info.duration_ms,
        "ffprobe": {
            "duration_s": round(duration, 3),
            "video": {
                "codec": video.get("codec_name"),
                "width": video.get("width"),
                "height": video.get("height"),
                "pix_fmt": video.get("pix_fmt"),
                "avg_frame_rate": video.get("avg_frame_rate"),
                "nb_frames": video.get("nb_frames"),
            },
            "audio": {
                "codec": audio.get("codec_name"),
                "sample_rate": audio.get("sample_rate"),
                "channels": audio.get("channels"),
            },
        },
    }


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def build_manifest(
    *,
    run_id: str,
    created_at: str,
    voice: str,
    checkpoint: str,
    size: tuple[int, int],
    fps: int,
    steps: list[dict[str, Any]],
    state: DemoState,
) -> dict[str, Any]:
    overall = (
        "SUCCESS"
        if steps and all(step["status"] == StepStatus.SUCCESS.value for step in steps)
        else "FAILED"
    )
    summary = state.mp4_summary
    return {
        "tool": "tools/free_ai_film_demo.py",
        "task": "BUDGET-01",
        "run_id": run_id,
        "created_at": created_at,
        "overall_status": overall,
        "script": {"language": "vi-VN", "text": SCRIPT_TEXT},
        "methods": {
            "background": background_method(state.comfy.get("checkpoint", checkpoint)),
            "motion": MOTION_METHOD,
            "voice": voice_method(voice),
            "subtitles": SUBTITLE_METHOD,
        },
        "cost": {"amount": "0", "paid_requests": 0},
        "budget": {
            "mode": state.budget.get("mode"),
            "status": state.budget.get("status"),
        },
        "steps": steps,
        "outputs": {
            "mp4": summary.get("path"),
            "sha256": summary.get("sha256"),
            "size_bytes": summary.get("size_bytes"),
            "ffprobe": summary.get("ffprobe"),
            "background_image": str(state.background_path)
            if state.background_path
            else None,
            "voice_mp3": str(state.voice_mp3) if state.voice_mp3 else None,
            "voice_wav": str(state.voice_wav) if state.voice_wav else None,
            "voice_duration_ms": state.voice_duration_ms,
        },
        "comfy": {
            "seed": state.comfy.get("seed"),
            "prompt_id": state.comfy.get("prompt_id"),
            "checkpoint": state.comfy.get("checkpoint"),
        },
        "render": {
            "size": f"{size[0]}x{size[1]}",
            "fps": fps,
            "frames": state.frames,
            "font_file": str(state.font_files.bold) if state.font_files else None,
            "subtitle_cues": [cue.as_dict() for cue in state.cues],
        },
    }


def write_manifest(manifest: dict[str, Any], path: Path) -> Path:
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def validate_manifest(path: Path) -> bool:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["methods"]["motion"] == MOTION_METHOD and data["cost"] == {
        "amount": "0",
        "paid_requests": 0,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_size(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)x(\d+)", text.strip())
    if not match:
        raise argparse.ArgumentTypeError("size must look like 720x1280")
    width, height = int(match.group(1)), int(match.group(2))
    if width < 64 or height < 64 or width % 2 or height % 2:
        raise argparse.ArgumentTypeError("width and height must be even and >= 64")
    return width, height


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT)
    parser.add_argument("--comfy-url", default=DEFAULT_COMFY_URL)
    parser.add_argument(
        "--checkpoint",
        default=os.environ.get("COMFYUI_CHECKPOINT") or DEFAULT_CHECKPOINT,
    )
    parser.add_argument("--voice", default=DEFAULT_VOICE)
    parser.add_argument("--size", type=parse_size, default=DEFAULT_SIZE)
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS)
    return parser.parse_args(argv)


def run_animate(
    settings: Any,
    size: tuple[int, int],
    fps: int,
    run_dir: Path,
    state: DemoState,
    log: Callable[[str], None],
) -> Path:
    state.font_files = resolve_font_files(settings.render_font_file)
    from PIL import Image

    if state.background_path is None or state.voice_wav is None or not state.frames:
        raise RuntimeError("background, voice or timeline missing")
    with Image.open(state.background_path) as image:
        scene = prepare_scene(image, size, state.font_files)
    out = run_dir / "free_ai_film_demo.mp4"
    started = time.monotonic()

    def frames() -> Iterator[Any]:
        for index, frame in enumerate(
            iter_frames(scene, state.envelope, state.cues, state.frames, fps)
        ):
            if index % (fps * 3) == 0:
                log(f"         animate: frame {index}/{state.frames}")
            yield frame

    encode(
        frames(),
        state.voice_wav,
        out,
        size=size,
        fps=fps,
        ffmpeg_binary=settings.ffmpeg_binary,
        log_path=run_dir / "ffmpeg.log",
    )
    log(
        f"         animate: {state.frames} frames in {time.monotonic() - started:.1f} s"
    )
    state.mp4_path = out
    return out


def run_validate(
    settings: Any, size: tuple[int, int], run_dir: Path, state: DemoState
) -> Path:
    if state.mp4_path is None:
        raise RuntimeError("no MP4")
    state.mp4_summary = validate_mp4(
        state.mp4_path,
        ffprobe=ffprobe_binary(settings.ffmpeg_binary),
        expected_size=size,
    )
    path = run_dir / "validation.json"
    path.write_text(json.dumps(state.mp4_summary, indent=2), encoding="utf-8")
    return path


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from ai_youtube_agent.core.config import load_settings
    from ai_youtube_agent.providers.registry.comfyui_adapter import ComfyUIAdapter
    from ai_youtube_agent.providers.registry.tts_adapters import EdgeTTSAdapter

    settings = load_settings()
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    created_at = _now()
    run_dir = (args.out_root / run_id).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    log = RunLog(run_dir / "run.log")
    log(f"free AI film demo, run {run_id} -> {run_dir}")

    comfy = ComfyUIAdapter(
        base_url=args.comfy_url,
        default_model=args.checkpoint,
        timeout_seconds=10.0,
        poll_interval_seconds=0.5,
        job_timeout_seconds=300.0,
    )
    edge = EdgeTTSAdapter(
        default_voice=args.voice, ffmpeg_binary=settings.ffmpeg_binary
    )
    state = DemoState()
    runner = StepRunner(STEP_NAMES, log=log)

    runner.run(
        "budget_check",
        lambda: run_budget_check(settings, [comfy, edge], run_dir, state),
        validate_budget,
    )
    runner.run(
        "background_image",
        lambda: run_background(comfy, run_dir, state),
        validate_image,
    )
    runner.run(
        "voice",
        lambda: run_voice(
            edge, args.voice, settings.ffmpeg_binary, run_dir, state, log=log
        ),
        lambda path: wav_duration_ms(path) > 0,
    )
    runner.run(
        "envelope_subtitles",
        lambda: run_envelope_subtitles(args.fps, run_dir, state),
        validate_timeline,
    )
    runner.run(
        "animate",
        lambda: run_animate(settings, args.size, args.fps, run_dir, state, log),
        lambda path: path.stat().st_size > 0,
    )
    runner.run("validate", lambda: run_validate(settings, args.size, run_dir, state))

    manifest_path = run_dir / "manifest.json"

    def manifest() -> dict[str, Any]:
        return build_manifest(
            run_id=run_id,
            created_at=created_at,
            voice=args.voice,
            checkpoint=args.checkpoint,
            size=args.size,
            fps=args.fps,
            steps=runner.as_list(),
            state=state,
        )

    # The manifest is written even after a failure: it is the run report.
    runner.run(
        "manifest",
        lambda: write_manifest(manifest(), manifest_path),
        validate_manifest,
        force=True,
    )
    final = manifest()
    write_manifest(final, manifest_path)
    log(f"overall {final['overall_status']}: {manifest_path}")
    return 0 if final["overall_status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
