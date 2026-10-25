"""Sound made by code: music, ambience and sound effects (no samples, no stock).

Everything is computed from seeds and the timeline with plain Python: ``array``
buffers, ``map``/comprehensions for the bulk work and a few one-pole filters, so
there is no numpy and a 28 s film takes a few seconds to synthesize.

* Music: a gentle 84 bpm piece in the storyboard key. A diatonic progression
  (I vi IV V, then V I at the end) is played by a detuned two-channel pad,
  a soft bass and plucked arpeggios; each scene mood changes the pattern and the
  density, the last chord is a rolled I(add9) that decays naturally.
* Ambience: street (dawn), kitchen hum, bubbling, sizzle, stove glow and soft
  chatter, cross-faded at scene boundaries.
* Effects: footstep, clink, bowl_place, chair, cloth, whoosh, steam_hiss and
  door_bell, placed at the times of the resolved timeline.

Output is float in about -1..1 at 48 kHz, peak-normalised to -6 dBFS before the
mix (``audio_mix`` sets the levels).
"""

from __future__ import annotations

import math
import random
import re
from array import array
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import accumulate, repeat
from operator import add, mul
from typing import Final

from ai_youtube_agent.animation.errors import AnimationError

RATE: Final[int] = 48000
PEAK: Final[float] = 0.5  # -6 dBFS
TAU: Final[float] = 2 * math.pi

FINAL_TAIL_S: Final[float] = 3.0
BEATS_PER_BAR: Final[int] = 4
MAJOR_SCALE: Final[tuple[int, ...]] = (0, 2, 4, 5, 7, 9, 11)
MINOR_SCALE: Final[tuple[int, ...]] = (0, 2, 3, 5, 7, 8, 10)
PITCH_CLASSES: Final[dict[str, int]] = {
    "C": 0,
    "D": 2,
    "E": 4,
    "F": 5,
    "G": 7,
    "A": 9,
    "B": 11,
}
# I vi IV V (scale degrees, 0-based); the bar before the end is V, the end is I.
PROGRESSION: Final[tuple[int, ...]] = (0, 5, 3, 4)
DOMINANT: Final[int] = 4
SUBDOMINANT: Final[int] = 3
PLUCK_LOW, PLUCK_HIGH = 60, 82
PAD_CENTER: Final[int] = 59
PAD_LOW, PAD_HIGH = 50, 67
BASS_LOW: Final[int] = 36

# Per mood: how loud the pad is and the pluck pattern as
# (beat in the bar, index into the arpeggio, velocity) for even and odd bars.
PAD_LEVEL: Final[dict[str, float]] = {
    "sparse": 0.55,
    "tender": 0.8,
    "curious": 0.6,
    "busy_warm": 0.75,
    "resolved": 0.95,
}
PluckStep = tuple[float, int, float]
PATTERNS: Final[dict[str, tuple[tuple[PluckStep, ...], tuple[PluckStep, ...]]]] = {
    "sparse": (
        ((0.0, 2, 0.5), (1.5, 3, 0.38), (3.0, 1, 0.34)),
        ((0.5, 1, 0.42), (2.0, 3, 0.46)),
    ),
    "tender": (
        tuple(
            (i * 0.5, index, vel)
            for i, (index, vel) in enumerate(
                zip(
                    (0, 1, 2, 3, 2, 1, 2, 3),
                    (0.62, 0.36, 0.42, 0.52, 0.4, 0.34, 0.4, 0.46),
                    strict=True,
                )
            )
        ),
        tuple(
            (i * 0.5, index, vel)
            for i, (index, vel) in enumerate(
                zip(
                    (1, 2, 3, 4, 3, 2, 1, 2),
                    (0.58, 0.36, 0.42, 0.5, 0.4, 0.34, 0.38, 0.44),
                    strict=True,
                )
            )
        ),
    ),
    "curious": (
        (
            (0.0, 1, 0.5),
            (0.75, 2, 0.4),
            (1.5, 3, 0.44),
            (2.5, 2, 0.4),
            (3.0, 4, 0.44),
        ),
        (
            (0.0, 2, 0.5),
            (0.75, 3, 0.4),
            (1.5, 1, 0.44),
            (2.5, 3, 0.4),
            (3.25, 2, 0.34),
        ),
    ),
    "busy_warm": (
        tuple(
            (i * 0.5, index, vel)
            for i, (index, vel) in enumerate(
                zip(
                    (0, 2, 1, 3, 2, 4, 3, 2),
                    (0.6, 0.38, 0.44, 0.5, 0.4, 0.5, 0.42, 0.38),
                    strict=True,
                )
            )
        ),
        tuple(
            (i * 0.5, index, vel)
            for i, (index, vel) in enumerate(
                zip(
                    (1, 3, 2, 4, 3, 2, 1, 2),
                    (0.58, 0.38, 0.44, 0.5, 0.42, 0.4, 0.4, 0.38),
                    strict=True,
                )
            )
        ),
    ),
    "resolved": (
        ((0.0, 2, 0.55), (1.0, 3, 0.44), (2.0, 4, 0.5), (3.0, 3, 0.4)),
        ((0.0, 3, 0.5), (1.5, 2, 0.4), (2.0, 1, 0.44), (3.0, 2, 0.36)),
    ),
}
# Pluck colour per mood: "soft" rings, "short" is staccato.
PLUCK_TONE: Final[dict[str, str]] = {
    "sparse": "soft",
    "tender": "soft",
    "curious": "short",
    "busy_warm": "soft",
    "resolved": "soft",
}
BASS_BEATS: Final[dict[str, tuple[float, ...]]] = {
    "sparse": (0.0,),
    "tender": (0.0, 2.0),
    "curious": (0.0, 2.0),
    "busy_warm": (0.0, 1.0, 2.0, 3.0),
    "resolved": (0.0, 2.0),
}
SHAKER_MOODS: Final[frozenset[str]] = frozenset({"busy_warm"})
DEFAULT_MOOD: Final[str] = "tender"

AMBIENCE_GAIN_DB: Final[dict[str, float]] = {
    "street_dawn": -2.0,
    "street": 0.0,
    "kitchen": 0.0,
    "bubbling": -1.0,
    "kitchen_hum": -3.0,
    "stove_glow": -4.0,
    "sizzle": -3.0,
    "soft_chatter": -6.0,
}
AMBIENCE_FADE_S: Final[float] = 0.35
# Peak of each effect relative to the others (linear, before the mix levels it).
SFX_GAIN: Final[dict[str, float]] = {
    "footstep": 0.42,
    "clink": 0.55,
    "bowl_place": 0.7,
    "chair": 0.6,
    "cloth": 0.4,
    "whoosh": 0.55,
    "steam_hiss": 0.45,
    "door_bell": 0.55,
}
SFX_KINDS: Final[tuple[str, ...]] = tuple(SFX_GAIN)


# ---------------------------------------------------------------------------
# Buffer helpers (float arrays)
# ---------------------------------------------------------------------------


def zeros(count: int) -> array:
    return array("d", bytes(8 * max(0, count)))


def mix_into(
    destination: array, source: Sequence[float], start: int, gain: float = 1.0
) -> None:
    """``destination[start:] += gain * source`` clipped to the destination."""
    if start < 0:
        source = source[-start:]
        start = 0
    count = min(len(source), len(destination) - start)
    if count <= 0:
        return
    scaled = (
        source[:count] if gain == 1.0 else [value * gain for value in source[:count]]
    )
    destination[start : start + count] = array(
        "d", map(add, destination[start : start + count], scaled)
    )


def scaled(samples: Sequence[float], gain: float) -> array:
    return array("d", [value * gain for value in samples])


def peak_of(samples: Sequence[float]) -> float:
    return max((abs(value) for value in samples), default=0.0)


def rms_of(samples: Sequence[float]) -> float:
    if not len(samples):
        return 0.0
    return math.sqrt(sum(value * value for value in samples) / len(samples))


def to_db(value: float) -> float:
    return -math.inf if value <= 0 else 20 * math.log10(value)


def normalize_peak(samples: array, peak: float = PEAK) -> array:
    top = peak_of(samples)
    return scaled(samples, peak / top) if top > 0 else samples


def set_rms(samples: Sequence[float], rms: float) -> array:
    current = rms_of(samples)
    return scaled(samples, rms / current) if current > 0 else array("d", samples)


def fade(
    samples: array, fade_in_s: float = 0.0, fade_out_s: float = 0.0, rate: int = RATE
) -> array:
    """Raised-cosine fades applied in place (returns the same array)."""
    count = len(samples)
    head = min(count, int(fade_in_s * rate))
    if head > 1:
        ramp = [0.5 - 0.5 * math.cos(math.pi * i / head) for i in range(head)]
        samples[:head] = array("d", map(mul, samples[:head], ramp))
    tail = min(count, int(fade_out_s * rate))
    if tail > 1:
        ramp = [0.5 + 0.5 * math.cos(math.pi * i / tail) for i in range(tail)]
        samples[count - tail :] = array("d", map(mul, samples[count - tail :], ramp))
    return samples


def noise(rng: random.Random, count: int) -> array:
    draw = rng.random
    return array("d", [draw() * 2.0 - 1.0 for _ in range(count)])


def lowpass(samples: Sequence[float], cutoff_hz: float, rate: int = RATE) -> array:
    """One-pole low-pass."""
    alpha = 1.0 - math.exp(-TAU * cutoff_hz / rate)
    state = 0.0
    out: list[float] = []
    append = out.append
    for value in samples:
        state += alpha * (value - state)
        append(state)
    return array("d", out)


def highpass(samples: Sequence[float], cutoff_hz: float, rate: int = RATE) -> array:
    low = lowpass(samples, cutoff_hz, rate)
    return array("d", map(lambda x, y: x - y, samples, low))


def bandpass(
    samples: Sequence[float], low_hz: float, high_hz: float, rate: int = RATE
) -> array:
    return lowpass(highpass(samples, low_hz, rate), high_hz, rate)


def exp_decay(count: int, tau_s: float, rate: int = RATE, start: float = 1.0) -> list:
    """``start * exp(-t / tau)`` for ``count`` samples (geometric, C speed)."""
    if count <= 0:
        return []
    ratio = math.exp(-1.0 / (tau_s * rate))
    return list(accumulate(repeat(ratio, count - 1), mul, initial=start))


def sine(freq_hz: float, count: int, rate: int = RATE, phase: float = 0.0) -> list:
    step = TAU * freq_hz / rate
    sin = math.sin
    return [sin(step * i + phase) for i in range(count)]


def db_to_gain(db: float) -> float:
    return 10 ** (db / 20)


# ---------------------------------------------------------------------------
# Notes and chords
# ---------------------------------------------------------------------------


def parse_key(text: str) -> tuple[int, tuple[int, ...], str]:
    """``(tonic pitch class, scale, name)`` of a key like ``"C major"``."""
    match = re.fullmatch(
        r"\s*([A-Ga-g])([#b]?)\s*(major|minor|maj|min)?\s*", text or ""
    )
    if match is None:
        raise AnimationError(f"cannot read the music key '{text}'")
    tonic = PITCH_CLASSES[match.group(1).upper()]
    tonic += {"#": 1, "b": -1, "": 0}[match.group(2)]
    minor = (match.group(3) or "major").startswith("min")
    return (
        tonic % 12,
        MINOR_SCALE if minor else MAJOR_SCALE,
        "minor" if minor else "major",
    )


def midi_to_hz(midi: int) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


def chord_pitch_classes(
    tonic: int, scale: Sequence[int], degree: int, size: int
) -> list[int]:
    """Pitch classes of the diatonic chord on ``degree``, ``size`` thirds high."""
    return [(tonic + scale[(degree + 2 * k) % 7]) % 12 for k in range(size)]


def nearest_midi(pitch_class: int, centre: int, low: int, high: int) -> int:
    """The MIDI note of ``pitch_class`` closest to ``centre`` in ``[low, high]``."""
    candidates = [m for m in range(low, high + 1) if m % 12 == pitch_class]
    if not candidates:
        candidates = [centre - ((centre - pitch_class) % 12)]
    return min(candidates, key=lambda m: (abs(m - centre), m))


@dataclass(frozen=True)
class Chord:
    degree: int
    root: int  # pitch class
    tones: tuple[int, ...]  # pitch classes, root first
    start_s: float
    end_s: float
    final: bool = False

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


@dataclass(frozen=True)
class NoteEvent:
    kind: str  # pluck | bass | pad | shaker
    midi: int
    start_s: float
    dur_s: float
    velocity: float
    pan: float = 0.0
    tone: str = "soft"
    channel: int | None = None  # pad only: 0 left, 1 right

    @property
    def end_s(self) -> float:
        return self.start_s + self.dur_s


def final_start_s(total_s: float, bpm: float) -> float:
    """Start of the closing chord: on the beat grid, ``FINAL_TAIL_S`` before the end."""
    beat = 60.0 / bpm
    return max(0.0, math.floor((total_s - FINAL_TAIL_S) / beat) * beat)


def progression_degrees(bars: int) -> list[int]:
    """Degrees of ``bars`` bars: I vi IV V repeated, ending IV V (then closing I)."""
    if bars <= 0:
        return []
    degrees = [PROGRESSION[i % len(PROGRESSION)] for i in range(bars)]
    degrees[-1] = DOMINANT if bars > 1 else PROGRESSION[0]
    if bars >= 3:
        degrees[-2] = SUBDOMINANT
    for index in range(1, bars - 2):
        following = degrees[index + 1] if index + 1 < bars - 1 else SUBDOMINANT
        if degrees[index] in (degrees[index - 1], following):
            options = [
                d for d in PROGRESSION if d not in (degrees[index - 1], following)
            ]
            degrees[index] = options[0]
    return degrees


def plan_chords(
    total_s: float, bpm: float, tonic: int, scale: Sequence[int]
) -> list[Chord]:
    """The chord of every bar, ending V then I (the closing chord has a 9th)."""
    beat = 60.0 / bpm
    bar = BEATS_PER_BAR * beat
    closing = final_start_s(total_s, bpm)
    starts = [
        i * bar
        for i in range(int(closing / bar + 1e-9) + 1)
        if i * bar < closing - 1e-6
    ]
    if len(starts) >= 2 and closing - starts[-1] < 2 * beat:
        starts.pop()
    degrees = progression_degrees(len(starts))
    chords: list[Chord] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else closing
        degree = degrees[index]
        size = 3 if degree == DOMINANT else 4
        pcs = chord_pitch_classes(tonic, scale, degree, size)
        chords.append(Chord(degree, pcs[0], tuple(pcs), start, end))
    pcs = chord_pitch_classes(tonic, scale, 0, 5)
    chords.append(Chord(0, pcs[0], tuple(pcs), closing, total_s, final=True))
    return chords


def arpeggio_notes(chord: Chord) -> list[int]:
    """Sorted MIDI notes of the chord tones in the pluck register."""
    notes = [
        m
        for m in range(PLUCK_LOW, PLUCK_HIGH + 1)
        if m % 12 in {pc % 12 for pc in chord.tones}
    ]
    return notes


def pad_notes(chord: Chord) -> list[int]:
    """Soft pad voicing: the chord tones above the root, folded near the centre."""
    pcs = list(chord.tones[1:]) if len(chord.tones) > 3 else list(chord.tones)
    if chord.final:
        pcs = list(chord.tones[1:4])
    notes = {nearest_midi(pc, PAD_CENTER, PAD_LOW, PAD_HIGH) for pc in pcs}
    return sorted(notes)


def bass_note(chord: Chord, fifth: bool = False) -> int:
    pc = chord.tones[2] if fifth and len(chord.tones) > 2 else chord.root
    midi = BASS_LOW + ((pc - BASS_LOW) % 12)
    return midi


def plan_music(
    total_s: float,
    bpm: float,
    key: str,
    scenes: Sequence[tuple[float, float, str]],
    *,
    seed: int = 20261101,
) -> list[NoteEvent]:
    """The notes of the whole piece, from the key, the tempo and the scene moods.

    ``scenes`` is the list of ``(start_s, end_s, mood)`` of the scenes.
    """
    tonic, scale, _ = parse_key(key)
    beat = 60.0 / bpm

    def mood(t: float) -> str:
        for start, end, name in scenes:
            if start <= t < end:
                return name
        return scenes[-1][2] if scenes else DEFAULT_MOOD

    rng = random.Random(seed)
    events: list[NoteEvent] = []
    unit = 0
    for chord in plan_chords(total_s, bpm, tonic, scale):
        level = PAD_LEVEL.get(mood(chord.start_s), 0.7)
        arp = arpeggio_notes(chord)
        if chord.final:
            _closing_events(events, chord, arp, level, rng)
            continue
        for channel in (0, 1):
            for midi in pad_notes(chord):
                events.append(
                    NoteEvent(
                        "pad",
                        midi,
                        max(0.0, chord.start_s - 0.25),
                        chord.duration_s + 0.25 + 0.7,
                        level,
                        -0.4 if channel == 0 else 0.4,
                        channel=channel,
                    )
                )
        position = chord.start_s
        while position < chord.end_s - 1e-6:
            name = mood(position)
            tone = PLUCK_TONE.get(name, "soft")
            pattern = PATTERNS.get(name, PATTERNS[DEFAULT_MOOD])[unit % 2]
            for step_beat, index, velocity in pattern:
                t = position + step_beat * beat
                if t >= chord.end_s - 0.05:
                    continue
                jitter = rng.uniform(-0.007, 0.007) if t > 0.05 else 0.0
                midi = arp[min(index, len(arp) - 1)]
                events.append(
                    NoteEvent(
                        "pluck",
                        midi,
                        max(0.0, t + jitter),
                        0.55 if tone == "short" else 2.2,
                        velocity * rng.uniform(0.92, 1.06),
                        ((midi % 3) - 1) * 0.28,
                        tone,
                    )
                )
            for step_beat in BASS_BEATS.get(name, (0.0,)):
                t = position + step_beat * beat
                if t >= chord.end_s - 0.05:
                    continue
                fifth = name == "busy_warm" and int(step_beat) % 2 == 1
                events.append(
                    NoteEvent(
                        "bass",
                        bass_note(chord, fifth),
                        t,
                        1.2,
                        0.8 if step_beat == 0 else 0.55,
                    )
                )
            if name in SHAKER_MOODS:
                for k in range(BEATS_PER_BAR):
                    t = position + (k + 0.5) * beat
                    if t < chord.end_s - 0.05:
                        events.append(
                            NoteEvent("shaker", 0, t, 0.12, 0.5 + 0.2 * (k % 2), 0.3)
                        )
            position += BEATS_PER_BAR * beat
            unit += 1
    return sorted(events, key=lambda e: (e.start_s, e.kind, e.midi))


def _closing_events(
    events: list[NoteEvent],
    chord: Chord,
    arp: Sequence[int],
    level: float,
    rng: random.Random,
) -> None:
    """The last chord: a rolled I(add9) over a long pad that decays on its own."""
    length = chord.duration_s + 1.5
    for channel in (0, 1):
        for midi in pad_notes(chord):
            events.append(
                NoteEvent(
                    "pad",
                    midi,
                    max(0.0, chord.start_s - 0.25),
                    length + 0.25,
                    level,
                    -0.4 if channel == 0 else 0.4,
                    channel=channel,
                )
            )
    events.append(
        NoteEvent("bass", bass_note(chord), chord.start_s, 3.5, 0.9, tone="long")
    )
    roll = [arp[0], arp[2], arp[3], arp[4], arp[min(6, len(arp) - 1)]]
    for index, midi in enumerate(roll):
        events.append(
            NoteEvent(
                "pluck",
                midi,
                chord.start_s + index * 0.11 + rng.uniform(0, 0.008),
                4.5,
                0.6 - 0.04 * index,
                (index - 2) * 0.2,
                "long",
            )
        )


# ---------------------------------------------------------------------------
# Voices
# ---------------------------------------------------------------------------

PLUCK_PARTIALS: Final[tuple[tuple[int, float, float], ...]] = (
    # (harmonic, amplitude, decay time relative to the fundamental)
    (1, 1.0, 1.0),
    (2, 0.5, 0.55),
    (3, 0.28, 0.36),
    (4, 0.14, 0.26),
    (5, 0.07, 0.18),
    (6, 0.03, 0.12),
)
PLUCK_TAU: Final[dict[str, float]] = {"soft": 1.0, "short": 0.2, "long": 2.2}


def pluck_tone(
    freq_hz: float, seconds: float, *, tau_s: float = 1.0, bright: float = 1.0
) -> array:
    """A soft harp-like pluck: decaying harmonics, each one an exact multiple."""
    count = int(seconds * RATE)
    out: array | None = None
    for harmonic, amplitude, relative in PLUCK_PARTIALS:
        freq = freq_hz * harmonic
        if freq > 0.42 * RATE:
            break
        amp = amplitude * (bright if harmonic > 1 else 1.0)
        envelope = exp_decay(count, tau_s * relative, start=amp)
        part = array("d", map(mul, envelope, sine(freq, count)))
        out = part if out is None else array("d", map(add, out, part))
    assert out is not None
    return fade(out, 0.004, min(0.08, seconds / 4))


def pad_tone(freq_hz: float, seconds: float, detune_hz: float) -> array:
    """A warm pad note: fundamental, soft 2nd and 3rd partials, slow breathing."""
    count = int(seconds * RATE)
    base = sine(freq_hz + detune_hz, count)
    second = sine(2 * freq_hz - 0.5 * detune_hz, count, phase=0.7)
    third = sine(3 * freq_hz + 0.3 * detune_hz, count, phase=1.9)
    lfo_step = TAU * (0.11 + (freq_hz % 7) * 0.013) / RATE
    cos = math.cos
    return array(
        "d",
        [
            (a + 0.26 * b + 0.07 * c) * (1.0 + 0.1 * cos(lfo_step * i))
            for i, (a, b, c) in enumerate(zip(base, second, third, strict=True))
        ],
    )


def shaker_hit(rng: random.Random) -> array:
    count = int(0.13 * RATE)
    burst = highpass(noise(rng, count), 5200)
    envelope = [
        (1 - math.exp(-i / 90.0)) * e for i, e in enumerate(exp_decay(count, 0.028))
    ]
    return array("d", map(mul, burst, envelope))


def pad_envelope(count: int, attack_s: float = 0.55, release_s: float = 0.8) -> list:
    attack = min(count // 2, int(attack_s * RATE))
    release = min(count - attack, int(release_s * RATE))
    sustain = count - attack - release
    rise = [0.5 - 0.5 * math.cos(math.pi * i / max(1, attack)) for i in range(attack)]
    fall = [0.5 + 0.5 * math.cos(math.pi * i / max(1, release)) for i in range(release)]
    return rise + [1.0] * sustain + fall


def render_music(
    events: Sequence[NoteEvent], total_s: float, *, seed: int = 20261101
) -> tuple[array, array]:
    """Mix the planned notes into a stereo pair of buffers of ``total_s``."""
    count = int(math.ceil(total_s * RATE))
    left, right = zeros(count), zeros(count)
    tone_cache: dict[tuple, array] = {}
    rng = random.Random(seed + 1)
    shaker_cache = [shaker_hit(rng) for _ in range(3)]
    shaker_index = 0

    def pluck(tone: str, midi: int, seconds: float) -> array:
        key = (tone, midi, seconds)
        if key not in tone_cache:
            tone_cache[key] = pluck_tone(
                midi_to_hz(midi),
                seconds,
                tau_s=PLUCK_TAU[tone],
                bright=1.3 if tone != "short" else 1.6,
            )
        return tone_cache[key]

    # Pad tones are rendered once per (note, channel) at the longest need.
    longest: dict[tuple[int, int], float] = {}
    for event in events:
        if event.kind == "pad":
            key = (event.midi, event.channel or 0)
            longest[key] = max(longest.get(key, 0.0), event.dur_s)
    pad_cache = {
        key: pad_tone(midi_to_hz(key[0]), length, 0.5 if key[1] == 0 else -0.5)
        for key, length in longest.items()
    }
    for event in events:
        start = int(event.start_s * RATE)
        theta = (event.pan + 1.0) * math.pi / 4
        gain_left, gain_right = math.cos(theta), math.sin(theta)
        if event.kind == "pad":
            tone = pad_cache[(event.midi, event.channel or 0)]
            window = tone[: int(event.dur_s * RATE)]
            shaped = array("d", map(mul, window, pad_envelope(len(window))))
            target = left if event.channel in (0, None) else right
            mix_into(target, shaped, start, event.velocity * 0.1)
            continue
        if event.kind == "shaker":
            hit = shaker_cache[shaker_index % len(shaker_cache)]
            shaker_index += 1
            mix_into(left, hit, start, event.velocity * 0.07 * gain_left * 1.4)
            mix_into(right, hit, start, event.velocity * 0.07 * gain_right * 1.4)
            continue
        if event.kind == "bass":
            midi = event.midi
            seconds = 3.5 if event.tone == "long" else 1.3
            key = ("bass", midi, seconds)
            if key not in tone_cache:
                tone_cache[key] = pluck_tone(
                    midi_to_hz(midi),
                    seconds,
                    tau_s=0.6 if event.tone != "long" else 1.6,
                    bright=0.25,
                )
            body = tone_cache[key]
            mix_into(left, body, start, event.velocity * 0.22)
            mix_into(right, body, start, event.velocity * 0.22)
            continue
        body = pluck(event.tone, event.midi, event.dur_s)
        mix_into(left, body, start, event.velocity * 0.2 * gain_left * 1.3)
        mix_into(right, body, start, event.velocity * 0.2 * gain_right * 1.3)
    top = max(peak_of(left), peak_of(right))
    if top > 0:
        left, right = scaled(left, PEAK / top), scaled(right, PEAK / top)
    # the closing chord rings on; only the last 80 ms are rounded off
    return fade(left, 0.0, 0.08), fade(right, 0.0, 0.08)


# ---------------------------------------------------------------------------
# Ambience
# ---------------------------------------------------------------------------


def _bird_chirp(rng: random.Random) -> array:
    seconds = rng.uniform(0.09, 0.16)
    count = int(seconds * RATE)
    f0 = rng.uniform(2800, 3800)
    f1 = f0 + rng.uniform(500, 1400) * rng.choice((-1, 1))
    phase = 0.0
    out: list[float] = []
    for i in range(count):
        u = i / count
        freq = f0 + (f1 - f0) * u + 180 * math.sin(TAU * 38 * u)
        phase += TAU * freq / RATE
        out.append(math.sin(phase) * math.sin(math.pi * u) ** 1.5)
    return array("d", out)


def _motorbike(rng: random.Random, seconds: float) -> array:
    count = int(seconds * RATE)
    f0 = rng.uniform(78, 96)
    out: list[float] = []
    phase = 0.0
    for i in range(count):
        u = i / count
        freq = f0 * (1.35 - 0.5 * u)  # passes by: the pitch falls
        phase += TAU * freq / RATE
        wave_value = (
            math.sin(phase)
            + 0.5 * math.sin(2 * phase + 0.4)
            + 0.3 * math.sin(3 * phase)
        )
        out.append(wave_value * math.sin(math.pi * u) ** 2)
    return lowpass(array("d", out), 520)


def _bubble(rng: random.Random) -> array:
    seconds = rng.uniform(0.05, 0.11)
    count = int(seconds * RATE)
    f0 = rng.uniform(260, 820)
    phase = 0.0
    out: list[float] = []
    for i in range(count):
        u = i / count
        phase += TAU * f0 * (1.0 + 0.45 * u) / RATE
        out.append(math.sin(phase) * math.exp(-u * 5.0))
    return array("d", out)


def _place_events(
    rng: random.Random,
    base: array,
    seconds: float,
    rate_hz: float,
    make,
    gain_range: tuple[float, float],
) -> None:
    t = rng.expovariate(rate_hz)
    while t < seconds:
        mix_into(base, make(rng), int(t * RATE), rng.uniform(*gain_range))
        t += max(0.12, rng.expovariate(rate_hz))


def render_ambience_layer(kind: str, seconds: float, seed: int) -> array:
    """One ambience kind for ``seconds`` at a reference RMS of 0.1 (no gain yet)."""
    if kind not in AMBIENCE_GAIN_DB:
        raise AnimationError(f"unknown ambience '{kind}'")
    rng = random.Random(seed)
    count = int(seconds * RATE)
    if kind in ("street", "street_dawn"):
        bed = lowpass(noise(rng, count), 420)
        air = lowpass(noise(rng, count), 2200)
        layer = array("d", [a + 0.12 * b for a, b in zip(bed, air, strict=True)])
        layer = set_rms(layer, 0.1)
        if seconds >= 2.0:
            pass_s = min(seconds - 0.4, rng.uniform(2.6, 3.6))
            bike = set_rms(_motorbike(rng, pass_s), 0.06)
            mix_into(
                layer,
                bike,
                int(rng.uniform(0.2, max(0.25, seconds - pass_s - 0.1)) * RATE),
            )
        if kind == "street_dawn":
            for _ in range(3):
                chirps = array("d", bytes(8 * int(0.9 * RATE)))
                t = 0.0
                for _ in range(rng.randint(2, 4)):
                    mix_into(
                        chirps, _bird_chirp(rng), int(t * RATE), rng.uniform(0.05, 0.1)
                    )
                    t += rng.uniform(0.15, 0.24)
                mix_into(
                    layer, chirps, int(rng.uniform(0.1, max(0.2, seconds - 1.0)) * RATE)
                )
        return layer
    if kind == "kitchen_hum":
        mains = [
            0.5 * a + 0.2 * b + 0.08 * c
            for a, b, c in zip(
                sine(100.0, count),
                sine(200.0, count, phase=0.5),
                sine(300.0, count, phase=1.1),
                strict=True,
            )
        ]
        fan = lowpass(noise(rng, count), 700)
        layer = array("d", [m + 0.9 * f for m, f in zip(mains, fan, strict=True)])
        return set_rms(layer, 0.1)
    if kind == "kitchen":
        room = lowpass(noise(rng, count), 620)
        layer = set_rms(room, 0.1)
        clinks = [normalize_peak(sfx_clink(v), 1.0) for v in range(3)]

        def tick(r: random.Random) -> array:
            return clinks[r.randrange(len(clinks))]

        _place_events(rng, layer, seconds, 0.7, tick, (0.03, 0.1))
        return set_rms(layer, 0.1)
    if kind == "bubbling":
        simmer = lowpass(noise(rng, count), 260)
        layer = set_rms(simmer, 0.045)
        _place_events(rng, layer, seconds, 4.5, _bubble, (0.06, 0.2))
        return set_rms(layer, 0.1)
    if kind == "sizzle":
        hiss = highpass(noise(rng, count), 3200)
        layer = set_rms(hiss, 0.03)

        def crackle(r: random.Random) -> array:
            n = int(r.uniform(0.003, 0.009) * RATE)
            return array("d", map(mul, noise(r, n), exp_decay(n, 0.0018)))

        _place_events(rng, layer, seconds, 36.0, crackle, (0.15, 0.7))
        return set_rms(layer, 0.1)
    if kind == "stove_glow":
        base = lowpass(noise(rng, count), 190)
        flutter = lowpass(noise(rng, count), 9)
        mod = [1.0 + 18.0 * f for f in flutter]
        layer = array("d", [b * max(0.15, m) for b, m in zip(base, mod, strict=True)])
        return set_rms(layer, 0.1)
    if kind == "soft_chatter":
        out = zeros(count)
        for voice in range(2):
            carrier = bandpass(noise(rng, count), 260 + 120 * voice, 2600)
            rate_hz = rng.uniform(3.2, 4.6)
            phase_a, phase_b = rng.uniform(0, TAU), rng.uniform(0, TAU)
            gate = [
                max(0.0, math.sin(TAU * rate_hz * i / RATE + phase_a)) ** 2
                * max(
                    0.0, 0.4 + math.sin(TAU * 0.45 * (voice + 1) * i / RATE + phase_b)
                )
                for i in range(count)
            ]
            voiced = array("d", map(mul, carrier, gate))
            mix_into(out, voiced, 0, 1.0)
        return set_rms(out, 0.1)
    raise AnimationError(f"unknown ambience '{kind}'")


def render_ambience(
    scenes: Sequence[tuple[float, float, Sequence[str]]],
    total_s: float,
    *,
    seed: int = 20261102,
) -> array:
    """Mono ambience bed: each scene's kinds, cross-faded at the boundaries."""
    count = int(math.ceil(total_s * RATE))
    bed = zeros(count)
    for index, (start, end, kinds) in enumerate(scenes):
        begin = max(0.0, start - AMBIENCE_FADE_S) if index else 0.0
        stop = (
            min(total_s, end + AMBIENCE_FADE_S) if index < len(scenes) - 1 else total_s
        )
        seconds = max(0.5, stop - begin)
        for kind_index, kind in enumerate(kinds):
            layer = render_ambience_layer(kind, seconds, seed + 97 * index + kind_index)
            layer = fade(
                scaled(layer, db_to_gain(AMBIENCE_GAIN_DB[kind])),
                AMBIENCE_FADE_S if index else 0.05,
                AMBIENCE_FADE_S if index < len(scenes) - 1 else 0.05,
            )
            mix_into(bed, layer, int(begin * RATE))
    return normalize_peak(bed)


# ---------------------------------------------------------------------------
# Sound effects
# ---------------------------------------------------------------------------


def _decaying_sine(
    freq: float, seconds: float, tau_s: float, amp: float = 1.0
) -> array:
    count = int(seconds * RATE)
    return array("d", map(mul, exp_decay(count, tau_s, start=amp), sine(freq, count)))


def _sum(parts: Sequence[array]) -> array:
    out = zeros(max(len(p) for p in parts))
    for part in parts:
        mix_into(out, part, 0)
    return out


def sfx_footstep(variant: int) -> array:
    rng = random.Random(1000 + variant)
    pitch = 1.0 if variant % 2 == 0 else 0.88
    thud = _sum(
        [
            _decaying_sine(78 * pitch, 0.16, 0.038, 1.0),
            _decaying_sine(150 * pitch, 0.1, 0.02, 0.4),
        ]
    )
    scuff_count = int(0.09 * RATE)
    scuff = lowpass(noise(rng, scuff_count), 1400)
    scuff = array("d", map(mul, scuff, exp_decay(scuff_count, 0.022)))
    out = _sum([thud, scaled(scuff, 0.55)])
    return fade(out, 0.002, 0.02)


def sfx_clink(variant: int) -> array:
    base = 2300.0 * (1.0 + 0.06 * variant)
    parts = [
        _decaying_sine(base, 0.35, 0.09, 1.0),
        _decaying_sine(base * 2.43, 0.25, 0.05, 0.55),
        _decaying_sine(base * 4.1, 0.15, 0.03, 0.3),
    ]
    return fade(_sum(parts), 0.001, 0.05)


def sfx_bowl_place(variant: int) -> array:
    rng = random.Random(2000 + variant)
    knock = _decaying_sine(190, 0.12, 0.03, 1.0)
    ring = _sum(
        [_decaying_sine(1180, 0.5, 0.16, 0.5), _decaying_sine(2860, 0.35, 0.09, 0.28)]
    )
    click_count = int(0.02 * RATE)
    click = array(
        "d",
        map(mul, lowpass(noise(rng, click_count), 3500), exp_decay(click_count, 0.004)),
    )
    return fade(_sum([knock, ring, scaled(click, 0.6)]), 0.001, 0.08)


def sfx_chair(variant: int) -> array:
    rng = random.Random(3000 + variant)
    seconds = 0.38
    count = int(seconds * RATE)
    scrape = bandpass(noise(rng, count), 180, 900)
    shape = [
        math.sin(math.pi * i / count) ** 1.4
        * (0.75 + 0.25 * math.sin(TAU * 17 * i / RATE))
        for i in range(count)
    ]
    scrape = array("d", map(mul, scrape, shape))
    thump = _decaying_sine(125, 0.18, 0.045, 1.0)
    out = zeros(count)
    mix_into(out, scrape, 0, 1.2)
    mix_into(out, thump, int(0.26 * RATE), 0.9)
    return fade(out, 0.005, 0.03)


def sfx_cloth(variant: int) -> array:
    rng = random.Random(4000 + variant)
    count = int(0.28 * RATE)
    rustle = bandpass(noise(rng, count), 900, 3600)
    shape = [math.sin(math.pi * i / count) ** 2 for i in range(count)]
    return array("d", map(mul, rustle, shape))


def sfx_whoosh(variant: int) -> array:
    rng = random.Random(5000 + variant)
    seconds = 0.5
    count = int(seconds * RATE)
    source = noise(rng, count)
    low = band = 0.0
    out: list[float] = []
    f0, f1, damping = 260.0, 2600.0, 0.5
    for i, value in enumerate(source):
        u = i / count
        freq = f0 * (f1 / f0) ** u
        coeff = 2 * math.sin(math.pi * freq / RATE)
        low += coeff * band
        high = value - low - damping * band
        band += coeff * high
        out.append(band * math.sin(math.pi * u) ** 2)
    return array("d", out)


def sfx_steam_hiss(variant: int) -> array:
    rng = random.Random(6000 + variant)
    count = int(0.55 * RATE)
    hiss = highpass(lowpass(noise(rng, count), 9000), 4200)
    shape = [math.sin(math.pi * i / count) ** 1.6 for i in range(count)]
    return array("d", map(mul, hiss, shape))


def sfx_door_bell(variant: int) -> array:
    def strike(freq: float) -> array:
        return _sum(
            [
                _decaying_sine(freq, 1.3, 0.34, 1.0),
                _decaying_sine(freq * 2.76, 0.8, 0.14, 0.35),
                _decaying_sine(freq * 5.4, 0.4, 0.06, 0.15),
            ]
        )

    out = zeros(int(1.6 * RATE))
    mix_into(out, strike(2093.0), 0, 1.0)
    mix_into(out, strike(2637.0), int(0.17 * RATE), 0.85)
    return fade(out, 0.001, 0.12)


SFX_MAKERS = {
    "footstep": sfx_footstep,
    "clink": sfx_clink,
    "bowl_place": sfx_bowl_place,
    "chair": sfx_chair,
    "cloth": sfx_cloth,
    "whoosh": sfx_whoosh,
    "steam_hiss": sfx_steam_hiss,
    "door_bell": sfx_door_bell,
}
SFX_VARIANTS: Final[int] = 3


def render_sfx(kind: str, variant: int = 0) -> array:
    """One effect normalised to a peak of 1.0."""
    if kind not in SFX_MAKERS:
        raise AnimationError(f"unknown sound effect '{kind}'")
    return normalize_peak(SFX_MAKERS[kind](variant), 1.0)


def render_sfx_track(events: Sequence[object], total_s: float) -> array:
    """Mono track of the effects; ``events`` have ``kind`` and ``t_ms`` (SfxEvent)."""
    count = int(math.ceil(total_s * RATE))
    track = zeros(count)
    cache: dict[tuple[str, int], array] = {}
    counters: dict[str, int] = {}
    for event in events:
        kind = event.kind  # type: ignore[attr-defined]
        if kind not in SFX_MAKERS:
            raise AnimationError(f"unknown sound effect '{kind}'")
        variant = counters.get(kind, 0) % SFX_VARIANTS
        counters[kind] = counters.get(kind, 0) + 1
        key = (kind, variant)
        if key not in cache:
            cache[key] = render_sfx(kind, variant)
        mix_into(
            track,
            cache[key],
            int(event.t_ms * RATE / 1000),  # type: ignore[attr-defined]
            SFX_GAIN[kind],
        )
    return track
