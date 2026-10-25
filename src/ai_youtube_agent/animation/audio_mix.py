"""Mix of the film's sound: narration, ducked music and ambience, effects, master.

The narration is placed on the film clock from the resolved timeline. The speech
windows measured on that real track drive the duck envelope that lowers the
music and the ambience under the voice. The four stems are written as WAV files
and ffmpeg mixes them, applies a limiter and a two-pass ``loudnorm`` to -14 LUFS
and -1.5 dBTP.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import sys
import time
import wave
import zlib
from array import array
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from operator import mul
from pathlib import Path
from typing import Any, Final

from ai_youtube_agent.animation import audio_synth as synth
from ai_youtube_agent.animation.audio_qc import Window
from ai_youtube_agent.animation.errors import AnimationError
from ai_youtube_agent.animation.timeline import ResolvedLine, ResolvedTimeline, SfxEvent
from ai_youtube_agent.animation.tts import read_wav_samples

NARRATION_RATE: Final[int] = 24000
MIX_RATE: Final[int] = synth.RATE
VOICE_RMS: Final[float] = 0.1  # -20 dBFS RMS per line, before the master
MUSIC_BELOW_VOICE_DB: Final[float] = 9.0
AMBIENCE_BELOW_VOICE_DB: Final[float] = 18.0
SFX_ABOVE_VOICE_DB: Final[float] = 6.0
AMBIENCE_DUCK_FACTOR: Final[float] = 0.55
LINE_FADE_S: Final[float] = 0.006
SPEECH_THRESHOLD_DB: Final[float] = -42.0
SPEECH_HOP_MS: Final[int] = 10
SPEECH_MERGE_GAP_MS: Final[int] = 240
SPEECH_MIN_MS: Final[int] = 80
CONTROL_MS: Final[int] = 5
DUCK_ATTACK_MS: Final[float] = 70.0
DUCK_RELEASE_MS: Final[float] = 300.0
TARGET_LUFS: Final[float] = -14.0
TARGET_TRUE_PEAK: Final[float] = -1.5
TARGET_LRA: Final[float] = 11.0
LIMIT: Final[float] = 0.89
RunProcess = Callable[..., Any]


# ---------------------------------------------------------------------------
# WAV I/O (16-bit)
# ---------------------------------------------------------------------------


def _to_int16(samples: Sequence[float]) -> array:
    return array("h", [max(-32768, min(32767, round(v * 32768.0))) for v in samples])


def write_wav(path: Path, channels: Sequence[Sequence[float]], rate: int) -> Path:
    """Write float channels (-1..1) as a 16-bit PCM WAV."""
    count = min(len(c) for c in channels)
    data = array("h", bytes(2 * count * len(channels)))
    for index, channel in enumerate(channels):
        data[index :: len(channels)] = _to_int16(channel[:count])
    if sys.byteorder == "big":
        data.byteswap()
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(len(channels))
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(data.tobytes())
    return path


def read_wav_float(path: Path) -> tuple[array, int]:
    """First channel of a 16-bit WAV as floats in -1..1."""
    samples, rate = read_wav_samples(path)
    return array("d", [v / 32768.0 for v in samples]), rate


# ---------------------------------------------------------------------------
# Narration
# ---------------------------------------------------------------------------


def normalize_line(samples: array, rms: float = VOICE_RMS) -> array:
    """The line at the same RMS as every other line, with tiny fades at the ends."""
    return synth.fade(
        synth.set_rms(samples, rms), LINE_FADE_S, LINE_FADE_S, NARRATION_RATE
    )


def place_narration(
    lines: Sequence[ResolvedLine],
    total_ms: float,
    *,
    rms: float = VOICE_RMS,
    rate: int = NARRATION_RATE,
) -> array:
    """The spoken lines on the film clock (mono, floats, ``total_ms`` long)."""
    track = synth.zeros(int(math.ceil(total_ms * rate / 1000)))
    for line in lines:
        samples, line_rate = read_wav_float(line.wav_path)
        if line_rate != rate:
            raise AnimationError(
                f"{line.wav_path.name} is {line_rate} Hz, not {rate} Hz"
            )
        synth.mix_into(
            track, normalize_line(samples, rms), int(round(line.start_ms * rate / 1000))
        )
    return track


def speech_windows(
    narration: Sequence[float],
    rate: int,
    word_spans: Sequence[Window] = (),
    *,
    threshold_db: float = SPEECH_THRESHOLD_DB,
    hop_ms: int = SPEECH_HOP_MS,
    merge_gap_ms: int = SPEECH_MERGE_GAP_MS,
    min_ms: int = SPEECH_MIN_MS,
) -> list[Window]:
    """The stretches where somebody speaks, in ms: the loud parts of the track
    joined over short gaps, plus the timed words, minus blips under ``min_ms``."""
    size = max(1, rate * hop_ms // 1000)
    threshold = 10 ** (threshold_db / 10)
    spans: list[Window] = []
    for index, begin in enumerate(range(0, len(narration) - size + 1, size)):
        chunk = narration[begin : begin + size]
        if sum(v * v for v in chunk) / size > threshold:
            spans.append((index * hop_ms, (index + 1) * hop_ms))
    merged: list[list[float]] = []
    for start, end in sorted([*spans, *word_spans]):
        if merged and start - merged[-1][1] <= merge_gap_ms:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged if b - a >= min_ms]


def _smoothstep(u: float) -> float:
    u = max(0.0, min(1.0, u))
    return u * u * (3 - 2 * u)


def duck_gain_envelope(
    windows: Sequence[Window],
    total_ms: float,
    depth_db: float = 9.0,
    attack_ms: float = DUCK_ATTACK_MS,
    release_ms: float = DUCK_RELEASE_MS,
    rate: int = MIX_RATE,
) -> array:
    """Per-sample linear gain that is 1.0 in the pauses and ``-depth_db`` in speech.

    The gain falls over ``attack_ms`` *before* a window starts (so it is already
    down on the first syllable) and recovers over ``release_ms`` after it ends.
    """
    steps = int(math.ceil(total_ms / CONTROL_MS)) + 2
    duck = [0.0] * steps
    for start, end in windows:
        first = max(0, int((start - attack_ms) / CONTROL_MS))
        last = min(steps - 1, int((end + release_ms) / CONTROL_MS) + 1)
        for index in range(first, last + 1):
            t = index * CONTROL_MS
            if t < start:
                weight = _smoothstep((t - (start - attack_ms)) / attack_ms)
            elif t <= end:
                weight = 1.0
            else:
                weight = 1.0 - _smoothstep((t - end) / release_ms)
            if weight > duck[index]:
                duck[index] = weight
    controls = [10 ** (-depth_db * d / 20) for d in duck]
    per_step = rate * CONTROL_MS // 1000
    gains: list[float] = []
    for index in range(steps - 1):
        a, b = controls[index], controls[index + 1]
        step = (b - a) / per_step
        gains.extend(a + step * k for k in range(per_step))
    return array("d", gains)


def apply_gain(samples: Sequence[float], gains: Sequence[float]) -> array:
    count = min(len(samples), len(gains))
    return array("d", map(mul, samples[:count], gains[:count]))


# ---------------------------------------------------------------------------
# ffmpeg master
# ---------------------------------------------------------------------------


def loudnorm_filter(measured: dict[str, str] | None = None) -> str:
    base = f"loudnorm=I={TARGET_LUFS:g}:TP={TARGET_TRUE_PEAK:g}:LRA={TARGET_LRA:g}"
    if measured is None:
        return base + ":print_format=json"
    return (
        f"{base}:measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
        f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
        f":offset={measured['target_offset']}:linear=true:print_format=summary"
    )


def build_mix_command(
    narration: Path,
    music: Path,
    ambience: Path,
    sfx: Path,
    out: Path | None,
    *,
    total_s: float,
    fade_out_ms: int,
    measured: dict[str, str] | None = None,
    ffmpeg_binary: str = "ffmpeg",
) -> list[str]:
    """Mix of the four stems; ``out=None`` runs the loudness analysis pass only.

    Narration is resampled to 48 kHz stereo, the music gets a light early-reflection
    reverb, the sum fades out at the end, is limited and normalised to -14 LUFS;
    the limiter and loudnorm delay and trim a few ms, so the end is padded back.
    """
    fade_s = fade_out_ms / 1000
    graph = (
        "[0:a]aresample=48000,pan=stereo|c0=c0|c1=c0[voice];"
        "[1:a]aecho=0.85:0.9:70|140:0.22|0.12[music];"
        "[2:a]anull[amb];"
        "[3:a]pan=stereo|c0=c0|c1=c0[sfx];"
        "[voice][music][amb][sfx]amix=inputs=4:normalize=0:duration=longest,"
        f"afade=t=in:d=0.03,afade=t=out:st={max(0.0, total_s - fade_s):.3f}"
        f":d={fade_s:.3f},"
        f"alimiter=limit={LIMIT}:attack=5:release=60:level=0,"
        f"{loudnorm_filter(measured)},apad=whole_dur={total_s:.3f}[mix]"
    )
    command = [
        ffmpeg_binary,
        "-y",
        "-hide_banner",
        "-nostats",
        "-i",
        str(narration),
        "-i",
        str(music),
        "-i",
        str(ambience),
        "-i",
        str(sfx),
        "-filter_complex",
        graph,
        "-map",
        "[mix]",
        "-t",
        f"{total_s:.3f}",
        "-ar",
        str(MIX_RATE),
        "-ac",
        "2",
    ]
    if out is None:
        return [*command, "-f", "null", "-"]
    return [*command, "-c:a", "pcm_s16le", str(out)]


def parse_loudnorm_json(stderr: str) -> dict[str, str]:
    """The measurement block that ``loudnorm=print_format=json`` prints at the end."""
    matches = re.findall(r"\{[^{}]*\}", stderr)
    for block in reversed(matches):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        if {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"} <= set(
            data
        ):
            return {key: str(value) for key, value in data.items()}
    raise AnimationError("ffmpeg loudnorm printed no measurement")


# ---------------------------------------------------------------------------
# The whole audio build
# ---------------------------------------------------------------------------


@dataclass
class AudioBuild:
    """Files and numbers of one audio build (for the manifest and the QC)."""

    narration: Path
    music: Path
    ambience: Path
    sfx: Path
    mix: Path
    windows: list[Window]
    total_ms: float
    seconds: float = 0.0
    duck_depth_db: float = 0.0
    music_events: int = 0
    sfx_events: int = 0
    loudnorm_pass1: dict[str, str] = field(default_factory=dict)
    commands: list[list[str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "narration": str(self.narration),
            "music": str(self.music),
            "ambience": str(self.ambience),
            "sfx": str(self.sfx),
            "mix": str(self.mix),
            "speech_windows": [[round(a), round(b)] for a, b in self.windows],
            "duck_depth_db": self.duck_depth_db,
            "music_events": self.music_events,
            "sfx_events": self.sfx_events,
            "loudnorm_pass1": self.loudnorm_pass1,
            "seconds": round(self.seconds, 1),
        }


def _run_ffmpeg(command: list[str], run: RunProcess, what: str) -> str:
    proc = run(command, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AnimationError(f"ffmpeg {what} failed: {str(proc.stderr)[-400:]}")
    return str(proc.stderr)


def build_audio(
    timeline: ResolvedTimeline,
    out_dir: Path,
    *,
    ffmpeg_binary: str = "ffmpeg",
    footsteps: Sequence[SfxEvent] = (),
    run: RunProcess = subprocess.run,
    log: Callable[[str], None] = print,
) -> AudioBuild:
    """Synthesize, duck and master the film's sound into ``out_dir``.

    Writes ``narration.wav``, ``music.wav``, ``ambience.wav`` (both ducked),
    ``sfx.wav`` and ``mix.wav`` (the mastered 48 kHz stereo track).
    """
    started = time.monotonic()
    storyboard = timeline.storyboard
    spec = storyboard.audio
    out_dir.mkdir(parents=True, exist_ok=True)
    seconds = math.ceil(timeline.frame_count * 1000 / timeline.fps) / 1000
    total_ms = seconds * 1000

    narration = place_narration(timeline.lines, total_ms)
    word_spans = [(w.start_ms, w.end_ms) for w in timeline.words()]
    windows = speech_windows(narration, NARRATION_RATE, word_spans)
    if not windows:
        raise AnimationError("the narration track has no speech")
    log(f"         audio: narration placed, {len(windows)} speech windows")

    moods = [
        (s.start_ms / 1000, s.end_ms / 1000, s.scene.mood) for s in timeline.scenes
    ]
    events = synth.plan_music(
        seconds,
        spec.music.bpm,
        spec.music.key,
        moods,
        seed=_seed(storyboard.id, "music"),
    )
    left, right = synth.render_music(
        events, seconds, seed=_seed(storyboard.id, "music")
    )
    log(
        f"         audio: music, {len(events)} notes "
        f"({time.monotonic() - started:.1f} s)"
    )
    scenes = [
        (s.start_ms / 1000, s.end_ms / 1000, s.scene.ambience) for s in timeline.scenes
    ]
    ambience = synth.render_ambience(
        scenes, seconds, seed=_seed(storyboard.id, "ambience")
    )
    sfx_events = sorted([*timeline.sfx, *footsteps], key=lambda e: (e.t_ms, e.kind))
    sfx = synth.render_sfx_track(sfx_events, seconds)
    log(
        f"         audio: ambience and {len(sfx_events)} effects "
        f"({time.monotonic() - started:.1f} s)"
    )

    voice_level = VOICE_RMS
    music_level = voice_level * 10 ** (-MUSIC_BELOW_VOICE_DB / 20)
    ambience_level = voice_level * 10 ** (-AMBIENCE_BELOW_VOICE_DB / 20)
    sfx_scale = voice_level * 10 ** (SFX_ABOVE_VOICE_DB / 20)
    stereo_rms = math.sqrt((synth.rms_of(left) ** 2 + synth.rms_of(right) ** 2) / 2)
    music_gain = music_level / stereo_rms if stereo_rms else 0.0
    depth = float(spec.duck_depth_db)
    duck_music = duck_gain_envelope(windows, total_ms, depth)
    duck_ambience = duck_gain_envelope(windows, total_ms, depth * AMBIENCE_DUCK_FACTOR)
    music_stems = [
        apply_gain(synth.scaled(channel, music_gain), duck_music)
        for channel in (left, right)
    ]
    ambience_stem = apply_gain(synth.set_rms(ambience, ambience_level), duck_ambience)
    sfx_stem = synth.scaled(sfx, sfx_scale)

    paths = {
        "narration": write_wav(out_dir / "narration.wav", [narration], NARRATION_RATE),
        "music": write_wav(out_dir / "music.wav", music_stems, MIX_RATE),
        "ambience": write_wav(
            out_dir / "ambience.wav", [ambience_stem, ambience_stem], MIX_RATE
        ),
        "sfx": write_wav(out_dir / "sfx.wav", [sfx_stem], MIX_RATE),
    }
    mix_path = out_dir / "mix.wav"
    commands: list[list[str]] = []
    common = dict(
        total_s=seconds,
        fade_out_ms=int(spec.fade_out_ms),
        ffmpeg_binary=ffmpeg_binary,
    )
    analysis = build_mix_command(
        paths["narration"],
        paths["music"],
        paths["ambience"],
        paths["sfx"],
        None,
        **common,
    )
    commands.append(analysis)
    measured = parse_loudnorm_json(_run_ffmpeg(analysis, run, "loudness analysis"))
    log(
        f"         audio: pass 1 measured {measured['input_i']} LUFS, "
        f"{measured['input_tp']} dBTP, LRA {measured['input_lra']}"
    )
    final = build_mix_command(
        paths["narration"],
        paths["music"],
        paths["ambience"],
        paths["sfx"],
        mix_path,
        measured=measured,
        **common,
    )
    commands.append(final)
    _run_ffmpeg(final, run, "master")
    if not mix_path.exists():
        raise AnimationError("ffmpeg wrote no mix")
    return AudioBuild(
        narration=paths["narration"],
        music=paths["music"],
        ambience=paths["ambience"],
        sfx=paths["sfx"],
        mix=mix_path,
        windows=windows,
        total_ms=total_ms,
        seconds=time.monotonic() - started,
        duck_depth_db=depth,
        music_events=len(events),
        sfx_events=len(sfx_events),
        loudnorm_pass1=measured,
        commands=commands,
    )


def _seed(*parts: str) -> int:
    return zlib.crc32("|".join(parts).encode("utf-8"))
