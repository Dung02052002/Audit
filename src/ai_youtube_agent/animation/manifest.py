"""Run manifest of the animation film, with its honesty rules.

The manifest says exactly how the film was made. ``validate_manifest`` is strict:
it fails when the motion description, the cost, the way a background was made or
the timing source of a line differs from what the tool really does.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from ai_youtube_agent.animation.backgrounds import (
    POST_RESIZE_LABEL,
    BackgroundRecord,
    BackgroundTier,
)
from ai_youtube_agent.animation.storyboard import Storyboard, VoiceSpec
from ai_youtube_agent.animation.subtitles import TIMING_SOURCES
from ai_youtube_agent.animation.timeline import ResolvedTimeline
from ai_youtube_agent.animation.tts import SpokenLine

TOOL: Final[str] = "tools/animation_film.py"
TASK: Final[str] = "ANIM-01"
BACKGROUND_BASE_METHOD: Final[str] = (
    "ComfyUI SD1.5 two-pass hires-fix "
    "(txt2img 576x1024 -> img2img 1080x1920, denoise 0.5)"
)
MOTION_METHOD: Final[str] = "software 2D animation (code), not an AI video model"
MUSIC_METHOD: Final[str] = (
    "synthesized by code, rights-clear, no samples or third-party tracks"
)
SUBTITLES_METHOD: Final[str] = "Pillow burn-in from Edge word-boundary timings"
VOICE_SUFFIX: Final[str] = "online free service, not local"
COST: Final[dict[str, Any]] = {"amount": "0", "paid_requests": 0}
SUCCESS: Final[str] = "SUCCESS"


def voice_method(voices: Sequence[VoiceSpec]) -> str:
    ids = sorted({voice.voice_id for voice in voices})
    return f"Edge TTS {', '.join(ids)} ({VOICE_SUFFIX})"


def background_method(records: Sequence[BackgroundRecord]) -> str:
    """The base method plus what the used tiers actually changed."""
    extras: list[str] = []
    for tier in BackgroundTier:
        used = [r for r in records if r.tier == tier.value]
        if not used:
            continue
        if tier is BackgroundTier.B:
            extras.append("tier B: explicit tiled VAE encode/decode")
        elif tier is BackgroundTier.C:
            extras.append(
                f"tier C: pass 2 at {used[0].pass2_size[0]}x{used[0].pass2_size[1]} "
                f"with tiled VAE, post_resize: {POST_RESIZE_LABEL}"
            )
    return BACKGROUND_BASE_METHOD + "".join(f"; {extra}" for extra in extras)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def build_manifest(
    *,
    run_id: str,
    created_at: str,
    storyboard: Storyboard,
    steps: Sequence[Mapping[str, Any]],
    budget: Mapping[str, Any],
    backgrounds: Mapping[str, BackgroundRecord],
    spoken: Mapping[str, SpokenLine],
    timeline: ResolvedTimeline | None = None,
    audio_qc: Mapping[str, Any] | None = None,
    validation: Mapping[str, Any] | None = None,
    render: Mapping[str, Any] | None = None,
    outputs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The manifest as a JSON-serialisable dict (written even after a failure)."""
    overall = (
        SUCCESS
        if steps and all(step["status"] == SUCCESS for step in steps)
        else "FAILED"
    )
    records = list(backgrounds.values())
    line_entries = [line.as_dict() for line in spoken.values()]
    timing_counts: dict[str, int] = {}
    for entry in line_entries:
        timing_counts[entry["timing_source"]] = (
            timing_counts.get(entry["timing_source"], 0) + 1
        )
    return {
        "tool": TOOL,
        "task": TASK,
        "run_id": run_id,
        "created_at": created_at,
        "overall_status": overall,
        "storyboard": {
            "id": storyboard.id,
            "title": storyboard.title,
            "language": storyboard.language,
            "scenes": [scene.id for scene in storyboard.scenes],
            "lines": {line.id: line.text for line in storyboard.all_lines()},
        },
        "methods": {
            "background": background_method(records),
            "motion": MOTION_METHOD,
            "voice": voice_method(storyboard.voices),
            "music": MUSIC_METHOD,
            "subtitles": SUBTITLES_METHOD,
        },
        "cost": dict(COST),
        "budget": {"mode": budget.get("mode"), "status": budget.get("status")},
        "steps": list(steps),
        "backgrounds": [record.as_dict() for record in records],
        "voice": {"lines": line_entries, "timing_sources": timing_counts},
        "seeds": {r.location_id: r.seed for r in records},
        "prompt_ids": {r.location_id: r.prompt_id for r in records},
        "timeline": _timeline_summary(timeline),
        "audio_qc": dict(audio_qc) if audio_qc is not None else None,
        "validation": dict(validation) if validation is not None else None,
        "render": dict(render) if render is not None else None,
        "outputs": dict(outputs) if outputs is not None else {},
    }


def _timeline_summary(timeline: ResolvedTimeline | None) -> dict[str, Any] | None:
    if timeline is None:
        return None
    return {
        "total_ms": timeline.total_ms,
        "fps": timeline.fps,
        "frame_count": timeline.frame_count,
        "scenes": [
            {"id": s.id, "start_ms": s.start_ms, "end_ms": s.end_ms}
            for s in timeline.scenes
        ],
        "transitions": [
            {
                "from": t.from_scene,
                "to": t.to_scene,
                "kind": t.kind,
                "duration_ms": t.duration_ms,
            }
            for t in timeline.transitions
        ],
    }


def validate_manifest(data: Mapping[str, Any]) -> list[str]:
    """Every way ``data`` is not an honest manifest (empty when it is)."""
    problems: list[str] = []
    methods = data.get("methods")
    if not isinstance(methods, Mapping):
        return ["methods is missing"]
    if methods.get("motion") != MOTION_METHOD:
        problems.append(f"methods.motion must be exactly '{MOTION_METHOD}'")
    if methods.get("music") != MUSIC_METHOD:
        problems.append(f"methods.music must be exactly '{MUSIC_METHOD}'")
    if methods.get("subtitles") != SUBTITLES_METHOD:
        problems.append(f"methods.subtitles must be exactly '{SUBTITLES_METHOD}'")
    voice = methods.get("voice")
    if not (
        isinstance(voice, str)
        and voice.startswith("Edge TTS ")
        and voice.endswith(f"({VOICE_SUFFIX})")
    ):
        problems.append("methods.voice must name Edge TTS as an online free service")
    if data.get("cost") != COST:
        problems.append(f"cost must be exactly {COST}")

    background = methods.get("background")
    if not (
        isinstance(background, str) and background.startswith(BACKGROUND_BASE_METHOD)
    ):
        problems.append(
            "methods.background must start with the two-pass hires-fix text"
        )
        background = ""
    problems.extend(_background_problems(data.get("backgrounds"), str(background)))
    problems.extend(_voice_problems(data.get("voice")))

    if data.get("overall_status") == SUCCESS:
        budget = data.get("budget")
        if not isinstance(budget, Mapping) or budget.get("status") != "free":
            problems.append("a successful run needs budget.status 'free'")
    return problems


def _background_problems(records: Any, method: str) -> list[str]:
    problems: list[str] = []
    for record in records if isinstance(records, list) else []:
        name = record.get("location_id", "?")
        tier = record.get("tier")
        if tier not in {t.value for t in BackgroundTier}:
            problems.append(f"background '{name}': unknown tier {tier!r}")
            continue
        resized = record.get("post_resize")
        if (tier == "C") != (resized is not None):
            problems.append(
                f"background '{name}': post_resize must be set only on tier C"
            )
        if resized is not None and resized != POST_RESIZE_LABEL:
            problems.append(f"background '{name}': post_resize label is wrong")
        if (tier in ("B", "C")) != bool(record.get("tiled_vae")):
            problems.append(
                f"background '{name}': tiled_vae does not match tier {tier}"
            )
        if tier == "C" and POST_RESIZE_LABEL not in method:
            problems.append("methods.background must say the tier C post-resize")
        if tier == "B" and "tiled VAE" not in method:
            problems.append("methods.background must say the tier B tiled VAE")
    return problems


def _voice_problems(voice: Any) -> list[str]:
    problems: list[str] = []
    if not isinstance(voice, Mapping):
        return ["voice is missing"]
    lines = voice.get("lines")
    counts: dict[str, int] = {}
    for entry in lines if isinstance(lines, list) else []:
        source = entry.get("timing_source")
        if source not in TIMING_SOURCES:
            problems.append(
                f"voice line '{entry.get('line_id', '?')}': timing_source "
                f"{source!r} is not one of {', '.join(TIMING_SOURCES)}"
            )
        else:
            counts[source] = counts.get(source, 0) + 1
    if voice.get("timing_sources") != counts:
        problems.append("voice.timing_sources does not match the line timing sources")
    return problems


def write_manifest(manifest: Mapping[str, Any], path: Path) -> Path:
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def validate_manifest_file(path: Path) -> bool:
    """Step validator: the manifest file parses and is honest."""
    return not validate_manifest(json.loads(path.read_text(encoding="utf-8")))
