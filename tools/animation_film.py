"""Animation film: a vertical 2D cartoon short made with free tools only (ANIM-01).

Development tool, not part of the package. It turns a storyboard (JSON) into an
MP4 and never calls a paid API:

1. ``budget_check``: the paid-API budget guard must say that ComfyUI and Edge TTS
   are free (status ``free``).
2. ``storyboard``: load and validate the storyboard.
3. ``voice``: one Edge TTS request per line (online free service), with word
   boundaries, trimmed and timed.
4. ``backgrounds``: one AI background per location from a local ComfyUI server
   (SD1.5, two-pass hires-fix); a failure is a failure, never a placeholder.
5. ``timeline_subtitles``: the film clock, SRT and VTT.
6. ``stills``: the first, middle and last frames (and a contact sheet).
7. ``audio``: music, ambience and effects synthesized by code, ducked under the
   voice, mixed and mastered to -14 LUFS.
8. ``render``: software 2D animation drawn frame by frame with Pillow and
   streamed to ffmpeg. This is NOT an AI video model.
9. ``validate``: container, decode, motion, scene variety, composition, audio QC.
10. ``manifest``: ``manifest.json`` with the methods, the cost (0), the budget
    status and every step; written even after a failure.

Run::

    uv run --extra tts --extra demo python tools/animation_film.py \\
        --storyboard tools/storyboards/quan_an_nho_shorts.json \\
        [--out-root artifacts/animation_film] [--comfy-url http://127.0.0.1:8188] \\
        [--checkpoint v1-5-pruned-emaonly.safetensors] [--cache-dir ...] \\
        [--stills 0.5,1.5 --stills-only] [--contact-sheet] [--steps a,b] [--strict]

Output goes to ``<out-root>/<run id>/``; an existing run directory is never
reused. Every step prints PENDING/RUNNING/SUCCESS/FAILED/BLOCKED and the lines are
also written to ``run.log``. The result is SUCCESS only when every step is.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ai_youtube_agent.animation import audio_mix, audio_qc, validate
from ai_youtube_agent.animation.backgrounds import JOB_TIMEOUT_S, BackgroundRecord
from ai_youtube_agent.animation.errors import AnimationError
from ai_youtube_agent.animation.manifest import (
    build_manifest,
    file_sha256,
    validate_manifest_file,
    write_manifest,
)
from ai_youtube_agent.animation.pipeline_steps import (
    RunLog,
    StepBlocked,
    StepRunner,
    now_iso,
    run_budget_check,
    validate_budget,
)
from ai_youtube_agent.animation.storyboard import Storyboard, load_storyboard
from ai_youtube_agent.animation.subtitles import (
    SAFE_AREA,
    CueSource,
    build_cues,
    write_srt,
    write_vtt,
)
from ai_youtube_agent.animation.timeline import ResolvedTimeline, resolve_timeline
from ai_youtube_agent.animation.tts import SpokenLine, speech_rate_problem

DEFAULT_OUT_ROOT = Path("artifacts/animation_film")
DEFAULT_STORYBOARD = Path("tools/storyboards/quan_an_nho_shorts.json")
DEFAULT_COMFY_URL = "http://127.0.0.1:8188"
DEFAULT_CHECKPOINT = "v1-5-pruned-emaonly.safetensors"
DEFAULT_VOICE = "vi-VN-HoaiMyNeural"
PROGRESS_EVERY_S = 3

FULL_STEPS = (
    "budget_check",
    "storyboard",
    "voice",
    "backgrounds",
    "timeline_subtitles",
    "stills",
    "audio",
    "render",
    "validate",
    "manifest",
)
STILLS_ONLY_STEPS = (
    "budget_check",
    "storyboard",
    "voice",
    "backgrounds",
    "timeline_subtitles",
    "stills",
    "manifest",
)
# Steps whose output a later step needs; a missing one blocks instead of crashing.
NEEDS = {
    "voice": ("storyboard",),
    "backgrounds": ("storyboard",),
    "timeline_subtitles": ("storyboard", "voice"),
    "stills": ("timeline_subtitles", "backgrounds"),
    "audio": ("timeline_subtitles",),
    "render": ("timeline_subtitles", "backgrounds", "audio"),
    "validate": ("render", "audio"),
}


@dataclass
class FilmState:
    """What the steps hand to each other."""

    run_dir: Path
    budget: dict[str, Any] = field(default_factory=dict)
    storyboard: Storyboard | None = None
    spoken: dict[str, SpokenLine] = field(default_factory=dict)
    backgrounds: dict[str, BackgroundRecord] = field(default_factory=dict)
    timeline: ResolvedTimeline | None = None
    cues: tuple[Any, ...] = ()
    renderer: Any = None
    fonts: Any = None
    audio: audio_mix.AudioBuild | None = None
    mp4: Path | None = None
    render_info: dict[str, Any] = field(default_factory=dict)
    composition: validate.CompositionTally | None = None
    qc: dict[str, Any] | None = None
    validation: dict[str, Any] | None = None
    outputs: dict[str, Any] = field(default_factory=dict)
    done: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


def parse_size(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)x(\d+)", text.strip())
    if not match:
        raise argparse.ArgumentTypeError("size must look like 1080x1920")
    width, height = int(match.group(1)), int(match.group(2))
    if width < 64 or height < 64 or width % 2 or height % 2:
        raise argparse.ArgumentTypeError("width and height must be even and >= 64")
    return width, height


def parse_times(text: str) -> list[float]:
    try:
        return [float(part) for part in text.split(",") if part.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--stills must be seconds like 0.5,1.5"
        ) from exc


def parse_steps(text: str) -> list[str]:
    names = [part.strip() for part in text.split(",") if part.strip()]
    unknown = [n for n in names if n not in FULL_STEPS]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown step {', '.join(unknown)}; steps are {', '.join(FULL_STEPS)}"
        )
    return names


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--storyboard", type=Path, default=DEFAULT_STORYBOARD)
    parser.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT)
    parser.add_argument("--comfy-url", default=DEFAULT_COMFY_URL)
    parser.add_argument(
        "--checkpoint",
        default=os.environ.get("COMFYUI_CHECKPOINT") or DEFAULT_CHECKPOINT,
        help="ComfyUI checkpoint file (must be installed there)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="cache of voice and background results (default <out-root>/_cache)",
    )
    parser.add_argument("--size", type=parse_size, default=None, help="e.g. 1080x1920")
    parser.add_argument("--fps", type=int, default=None)
    parser.add_argument(
        "--stills",
        type=parse_times,
        default=None,
        help="seconds of extra stills, e.g. 0.5,1.5",
    )
    parser.add_argument(
        "--stills-only",
        action="store_true",
        help="run only up to the stills (no audio, video or validation)",
    )
    parser.add_argument("--contact-sheet", action="store_true")
    parser.add_argument("--steps", type=parse_steps, default=None)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="every validation check fails the step (default: container, decode)",
    )
    return parser.parse_args(argv)


def choose_steps(args: argparse.Namespace) -> list[str]:
    """The steps to run, in order; the manifest is always the last one."""
    if args.steps:
        chosen = [s for s in FULL_STEPS if s in args.steps]
    elif args.stills_only:
        chosen = list(STILLS_ONLY_STEPS)
    else:
        chosen = list(FULL_STEPS)
    if "manifest" not in chosen:
        chosen.append("manifest")
    return chosen


def make_run_dir(out_root: Path, now: datetime | None = None) -> tuple[str, Path]:
    """A new run directory ``<out_root>/<timestamp>``; never an existing one."""
    out_root.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    run_id, number = stamp, 1
    while True:
        path = (out_root / run_id).resolve()
        try:
            path.mkdir()
        except FileExistsError:
            number += 1
            run_id = f"{stamp}-{number}"
            continue
        return run_id, path


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


class Pipeline:
    """The steps of one run, with their services injected."""

    def __init__(
        self,
        args: argparse.Namespace,
        settings: Any,
        comfy: Any,
        edge: Any,
        state: FilmState,
        log: Callable[[str], None],
        *,
        budget_check: Callable[..., Path] = run_budget_check,
    ) -> None:
        self.args = args
        self.settings = settings
        self.comfy = comfy
        self.edge = edge
        self.state = state
        self.log = log
        self.budget_check = budget_check
        self.cache_dir = args.cache_dir or (args.out_root / "_cache")

    def need(self, step: str) -> None:
        for name in NEEDS.get(step, ()):
            if name not in self.state.done:
                raise StepBlocked(f"the '{name}' step has not succeeded in this run")

    # --- budget, storyboard ----------------------------------------

    def run_budget(self) -> Path:
        return self.budget_check(
            self.settings,
            [self.comfy, self.edge],
            self.state.run_dir,
            self.state.budget,
        )

    def run_storyboard(self) -> Path:
        storyboard = load_storyboard(self.args.storyboard)
        fmt = storyboard.format
        if self.args.fps is not None and self.args.fps != fmt.fps:
            raise StepBlocked(
                f"--fps {self.args.fps} differs from the storyboard ({fmt.fps}); "
                "the frame rate is part of the storyboard"
            )
        size = self.args.size or (fmt.width, fmt.height)
        if size[0] * fmt.height != size[1] * fmt.width:
            raise StepBlocked(
                f"--size {size[0]}x{size[1]} is not {fmt.width}:{fmt.height}"
            )
        self.state.storyboard = storyboard
        path = self.state.run_dir / "storyboard.json"
        path.write_text(
            self.args.storyboard.read_text(encoding="utf-8"), encoding="utf-8"
        )
        return path

    @property
    def size(self) -> tuple[int, int]:
        assert self.state.storyboard is not None
        fmt = self.state.storyboard.format
        return self.args.size or (fmt.width, fmt.height)

    # --- voice, backgrounds ----------------------------------------

    def run_voice(self) -> Path:
        from ai_youtube_agent.animation.tts import synthesize_lines

        self.need("voice")
        assert self.state.storyboard is not None
        spoken = synthesize_lines(
            self.edge,
            self.state.storyboard,
            self.settings.ffmpeg_binary,
            self.state.run_dir / "voice",
            self.cache_dir / "voice",
            log=self.log,
        )
        problems = [p for p in map(speech_rate_problem, spoken.values()) if p]
        self.state.spoken = spoken
        path = self.state.run_dir / "voice.json"
        path.write_text(
            json.dumps(
                [s.as_dict() for s in spoken.values()], ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        if problems:
            raise AnimationError("; ".join(problems))
        return path

    def run_backgrounds(self) -> Path:
        from ai_youtube_agent.animation.backgrounds import generate_backgrounds

        self.need("backgrounds")
        assert self.state.storyboard is not None
        records = generate_backgrounds(
            self.comfy,
            self.state.storyboard,
            self.state.run_dir / "backgrounds",
            self.cache_dir / "backgrounds",
            free_vram=getattr(self.comfy, "free_memory", None),
            log=self.log,
        )
        self.state.backgrounds = records
        path = self.state.run_dir / "backgrounds.json"
        path.write_text(
            json.dumps(
                [r.as_dict() for r in records.values()], ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        return path

    # --- timeline, stills ----------------------------------------

    def run_timeline(self) -> Path:
        self.need("timeline_subtitles")
        assert self.state.storyboard is not None
        timeline = resolve_timeline(self.state.storyboard, self.state.spoken)
        self.state.timeline = timeline
        cues = build_cues(
            [CueSource(line.line_id, line.words) for line in timeline.lines]
        )
        self.state.cues = cues
        stem = self.state.storyboard.id
        srt = write_srt(cues, self.state.run_dir / f"{stem}.srt")
        write_vtt(cues, self.state.run_dir / f"{stem}.vtt")
        summary = {
            "total_ms": timeline.total_ms,
            "frames": timeline.frame_count,
            "fps": timeline.fps,
            "scenes": [
                {"id": s.id, "start_ms": s.start_ms, "end_ms": s.end_ms}
                for s in timeline.scenes
            ],
            "transitions": [
                {
                    "from": t.from_scene,
                    "to": t.to_scene,
                    "kind": t.kind,
                    "start_ms": t.start_ms,
                    "end_ms": t.end_ms,
                }
                for t in timeline.transitions
            ],
            "sfx": [
                {"kind": e.kind, "t_ms": e.t_ms, "source": e.source}
                for e in timeline.sfx
            ],
            "cues": [c.as_dict() for c in cues],
        }
        (self.state.run_dir / "timeline.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.log(
            f"         timeline: {timeline.total_ms} ms, "
            f"{timeline.frame_count} frames, "
            f"{len(cues)} subtitle cues"
        )
        return srt

    def renderer(self) -> Any:
        if self.state.renderer is not None:
            return self.state.renderer
        from ai_youtube_agent.animation.actions import load_envelope
        from ai_youtube_agent.animation.fonts import resolve_font_files
        from ai_youtube_agent.animation.renderer import FrameRenderer, RenderAssets

        assert self.state.timeline is not None
        fonts = resolve_font_files(self.settings.render_font_file)
        self.state.fonts = fonts
        envelopes = {}
        for line in self.state.timeline.lines:
            envelope = load_envelope(line.wav_path)
            if envelope is not None:
                envelopes[line.line_id] = envelope
        plates = {loc: Path(rec.path) for loc, rec in self.state.backgrounds.items()}
        assets = RenderAssets(plates=plates, fonts=fonts, envelopes=envelopes)
        self.state.renderer = FrameRenderer(
            self.state.timeline, assets, size=self.size, fps=self.state.timeline.fps
        )
        return self.state.renderer

    def run_stills(self) -> Path:
        self.need("stills")
        assert self.state.timeline is not None
        renderer = self.renderer()
        total_ms = self.state.timeline.total_ms
        named: list[tuple[str, float]] = [
            ("start", 400.0),
            ("middle", total_ms / 2),
            ("end", max(0.0, total_ms - 500.0)),
        ]
        named += [(f"t{t:g}s", t * 1000) for t in (self.args.stills or [])]
        started = time.monotonic()
        outputs = []
        for name, t_ms in named:
            ((_, image, _meta),) = renderer.render_stills([t_ms])
            path = self.state.run_dir / f"still_{name}.png"
            image.save(path)
            outputs.append(path)
        sheet_path = None
        if self.args.contact_sheet:
            sheet = renderer.contact_sheet()
            sheet_path = self.state.run_dir / "contact_sheet.png"
            sheet.save(sheet_path)
        self.state.outputs["stills"] = [str(p) for p in outputs]
        if sheet_path is not None:
            self.state.outputs["contact_sheet"] = str(sheet_path)
        self.log(
            f"         stills: {len(outputs)} stills"
            f"{' and a contact sheet' if sheet_path else ''} "
            f"in {time.monotonic() - started:.1f} s"
        )
        return sheet_path or outputs[0]

    # --- audio ----------------------------------------

    def run_audio(self) -> Path:
        from ai_youtube_agent.animation.actions import footstep_events

        self.need("audio")
        assert self.state.timeline is not None
        renderer = self.state.renderer
        footsteps = footstep_events(
            self.state.timeline, getattr(renderer, "choreo", None)
        )
        build = audio_mix.build_audio(
            self.state.timeline,
            self.state.run_dir,
            ffmpeg_binary=self.settings.ffmpeg_binary,
            footsteps=footsteps,
            log=self.log,
        )
        self.state.audio = build
        self.log(f"         audio: built in {build.seconds:.1f} s")
        return build.mix

    def check_mix(self, path: Path) -> bool:
        report = audio_qc.measure_loudness(path, self.settings.ffmpeg_binary)
        result = audio_qc.AudioQcResult()
        audio_qc.evaluate_loudness(report, result)
        self.log(
            f"         audio: mix {report.integrated_lufs:.1f} LUFS, "
            f"{report.true_peak_dbtp:.1f} dBTP, LRA {report.lra:.1f}"
        )
        return result.passed

    # --- render ----------------------------------------

    def run_render(self) -> Path:
        from ai_youtube_agent.animation.encode import encode_frames
        from ai_youtube_agent.animation.renderer import iter_frames

        self.need("render")
        assert self.state.timeline is not None and self.state.audio is not None
        assert self.state.storyboard is not None
        renderer = self.renderer()
        tally = validate.CompositionTally(
            size=self.size, safe_area=_scaled(SAFE_AREA, renderer.fs)
        )
        self.state.composition = tally
        out = self.state.run_dir / f"{self.state.storyboard.id}.mp4"
        total = renderer.frame_count
        started = time.monotonic()
        step = self.state.timeline.fps * PROGRESS_EVERY_S

        def frames() -> Any:
            for index, frame in enumerate(iter_frames(renderer, on_meta=tally.add)):
                if index % step == 0:
                    elapsed = time.monotonic() - started
                    self.log(
                        f"         render: frame {index}/{total} ({elapsed:.0f} s)"
                    )
                yield frame

        result = encode_frames(
            frames(),
            out,
            size=self.size,
            fps=self.state.timeline.fps,
            audio=self.state.audio.mix,
            pad_audio=True,
            ffmpeg_binary=self.settings.ffmpeg_binary,
            log_path=self.state.run_dir / "ffmpeg.log",
        )
        seconds = time.monotonic() - started
        self.state.mp4 = out
        self.state.render_info = {
            "size": f"{self.size[0]}x{self.size[1]}",
            "fps": self.state.timeline.fps,
            "frames": result.frames,
            "seconds": round(seconds, 1),
            "seconds_per_frame": round(seconds / max(1, result.frames), 3),
            "font_file": str(self.state.fonts.bold) if self.state.fonts else None,
            "subtitle_cues": len(self.state.cues),
            "ffmpeg_command": list(result.command),
        }
        self.log(
            f"         render: {result.frames} frames in {seconds:.1f} s "
            f"({seconds / max(1, result.frames):.2f} s/frame)"
        )
        return out

    # --- validate ----------------------------------------

    def run_validate(self) -> Path:
        self.need("validate")
        state = self.state
        assert state.mp4 is not None and state.timeline is not None
        assert state.audio is not None and state.storyboard is not None
        ffmpeg = self.settings.ffmpeg_binary
        timeline, fmt = state.timeline, state.storyboard.format
        low, high = fmt.duration_ms_range
        expected = validate.ExpectedMedia(
            width=self.size[0],
            height=self.size[1],
            fps=timeline.fps,
            frames=timeline.frame_count,
            min_s=low / 1000,
            max_s=high / 1000,
        )
        report = validate.ValidationReport()
        probe = validate.probe_media(state.mp4, validate.ffprobe_binary(ffmpeg))
        problems, facts = validate.check_container(
            probe, expected, faststart=validate.faststart_ok(state.mp4)
        )
        report.add("container", problems, facts)
        report.add("decode", validate.decode_errors(state.mp4, ffmpeg), {})

        series = validate.motion_series(state.mp4, state.run_dir, ffmpeg)
        scenes = [(s.id, s.start_ms / 1000, s.end_ms / 1000) for s in timeline.scenes]
        problems, facts = validate.analyze_motion(series, scenes)
        report.add("motion", problems, facts)

        histograms = {}
        for scene in timeline.scenes:
            middle = (scene.start_ms + scene.end_ms) / 2000
            histograms[scene.id] = validate.colour_histogram(
                validate.thumbnail(state.mp4, middle, ffmpeg)
            )
        problems, facts = validate.analyze_scene_distinctness(
            histograms, [s.id for s in timeline.scenes]
        )
        report.add("scenes", problems, facts)

        assert state.composition is not None
        problems, facts = state.composition.report()
        report.add("composition", problems, facts)

        result = self.measure_final_audio(state.mp4, ffmpeg)
        state.qc = result.as_dict()
        report.add("audio", result.problems, {"measurements": state.qc})
        (state.run_dir / "audio_qc.json").write_text(
            json.dumps(state.qc, indent=2), encoding="utf-8"
        )
        state.validation = report.as_dict(self.args.strict)
        path = state.run_dir / "validation.json"
        path.write_text(
            json.dumps(state.validation, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        for problem in report.problems:
            self.log(f"         validate: {problem}")
        failed = report.failed(self.args.strict)
        if failed:
            raise AnimationError("; ".join(failed))
        return path

    def measure_final_audio(self, mp4: Path, ffmpeg: str) -> audio_qc.AudioQcResult:
        assert self.state.audio is not None
        build = self.state.audio
        result = audio_qc.AudioQcResult()
        audio_qc.evaluate_loudness(audio_qc.measure_loudness(mp4, ffmpeg), result)
        decoded = audio_qc.decode_audio(
            mp4, self.state.run_dir / "final_audio.wav", ffmpeg
        )
        channels, rate = audio_qc.read_pcm16(decoded)
        audio_qc.evaluate_samples(channels, rate, result)
        narration, narration_rate = audio_qc.read_pcm16(build.narration)
        music, music_rate = audio_qc.read_pcm16(build.music)
        ambience, _ = audio_qc.read_pcm16(build.ambience)
        bed = audio_qc.mono_mix(
            [
                [a + b for a, b in zip(m, e, strict=True)]
                for m, e in zip(music, ambience, strict=True)
            ]
        )
        audio_qc.evaluate_mix_balance(
            [float(v) for v in narration[0]],
            narration_rate,
            bed,
            music_rate,
            build.windows,
            build.total_ms,
            result,
        )
        decoded.unlink(missing_ok=True)
        return result


def _scaled(rect: Any, factor: float) -> tuple[int, int, int, int]:
    return (
        round(rect.left * factor),
        round(rect.top * factor),
        round(rect.right * factor),
        round(rect.bottom * factor),
    )


# ---------------------------------------------------------------------------
# Manifest and main
# ---------------------------------------------------------------------------


def media_summary(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    return {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "sha256": file_sha256(path),
    }


def build_run_manifest(
    run_id: str,
    created_at: str,
    runner: StepRunner,
    state: FilmState,
    pipeline: Pipeline,
) -> dict[str, Any]:
    storyboard = state.storyboard
    if storyboard is None:
        storyboard = load_storyboard(pipeline.args.storyboard)
    outputs = dict(state.outputs)
    outputs["mp4"] = media_summary(state.mp4)
    stem = storyboard.id
    for key, name in (
        ("srt", f"{stem}.srt"),
        ("vtt", f"{stem}.vtt"),
        ("narration", "narration.wav"),
        ("music", "music.wav"),
        ("ambience", "ambience.wav"),
        ("sfx", "sfx.wav"),
        ("mix", "mix.wav"),
        ("audio_qc", "audio_qc.json"),
        ("validation", "validation.json"),
    ):
        if (state.run_dir / name).exists():
            outputs[key] = str(state.run_dir / name)
    render = dict(state.render_info)
    if state.audio is not None:
        render["audio"] = state.audio.as_dict()
    return build_manifest(
        run_id=run_id,
        created_at=created_at,
        storyboard=storyboard,
        steps=runner.as_list(),
        budget=state.budget,
        backgrounds=state.backgrounds,
        spoken=state.spoken,
        timeline=state.timeline,
        audio_qc=state.qc,
        validation=state.validation,
        render=render or None,
        outputs=outputs,
    )


def create_services(args: argparse.Namespace, settings: Any) -> tuple[Any, Any]:
    from ai_youtube_agent.providers.registry.comfyui_adapter import ComfyUIAdapter
    from ai_youtube_agent.providers.registry.tts_adapters import EdgeTTSAdapter

    comfy = ComfyUIAdapter(
        base_url=args.comfy_url,
        default_model=args.checkpoint,
        timeout_seconds=10.0,
        poll_interval_seconds=0.5,
        job_timeout_seconds=JOB_TIMEOUT_S,
    )
    edge = EdgeTTSAdapter(
        default_voice=DEFAULT_VOICE, ffmpeg_binary=settings.ffmpeg_binary
    )
    return comfy, edge


def run(
    args: argparse.Namespace,
    settings: Any,
    comfy: Any,
    edge: Any,
    *,
    budget_check: Callable[..., Path] = run_budget_check,
    now: datetime | None = None,
) -> int:
    run_id, run_dir = make_run_dir(args.out_root, now)
    created_at = now_iso()
    log = RunLog(run_dir / "run.log")
    log(f"animation film, run {run_id} -> {run_dir}")
    state = FilmState(run_dir=run_dir)
    pipeline = Pipeline(
        args, settings, comfy, edge, state, log, budget_check=budget_check
    )
    names = choose_steps(args)
    runner = StepRunner(names, log=log)

    def step(
        name: str, fn: Callable[[], Path], check: Callable[[Path], object] | None = None
    ) -> None:
        if name not in names:
            return
        if runner.run(name, fn, check) is not None:
            state.done.add(name)

    step("budget_check", pipeline.run_budget, validate_budget)
    step("storyboard", pipeline.run_storyboard, lambda p: p.stat().st_size > 0)
    step("voice", pipeline.run_voice, lambda p: bool(state.spoken))
    step("backgrounds", pipeline.run_backgrounds, lambda p: bool(state.backgrounds))
    step("timeline_subtitles", pipeline.run_timeline, lambda p: p.stat().st_size > 0)
    step("stills", pipeline.run_stills, lambda p: p.stat().st_size > 0)
    step("audio", pipeline.run_audio, pipeline.check_mix)
    step("render", pipeline.run_render, lambda p: p.stat().st_size > 0)
    step("validate", pipeline.run_validate)

    manifest_path = run_dir / "manifest.json"

    def manifest() -> dict[str, Any]:
        return build_run_manifest(run_id, created_at, runner, state, pipeline)

    # The manifest is written even after a failure: it is the run report.
    runner.run(
        "manifest",
        lambda: write_manifest(manifest(), manifest_path),
        validate_manifest_file,
        force=True,
    )
    final = manifest()
    write_manifest(final, manifest_path)
    log(f"overall {final['overall_status']}: {manifest_path}")
    return 0 if final["overall_status"] == "SUCCESS" else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from ai_youtube_agent.core.config import load_settings

    settings = load_settings()
    comfy, edge = create_services(args, settings)
    return run(args, settings, comfy, edge)


if __name__ == "__main__":
    raise SystemExit(main())
