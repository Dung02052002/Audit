"""Storyboard: the JSON description of the film and its validation.

``load_storyboard`` parses a JSON file or dict into frozen dataclasses and
validates it; every problem found is reported at once in a ``StoryboardError``.
Times in the storyboard are milliseconds. A scene's ``at`` anchors are
relative to the scene start; camera ``moves`` are relative to their shot start.
The stage is 1080x1920 at zoom 1; ``x``/``y`` of cast and props are
normalised stage coordinates (0..1, outside the range means off screen).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from ai_youtube_agent.animation.easing import EASINGS
from ai_youtube_agent.animation.errors import StoryboardError

SCHEMA_VERSION: Final[int] = 1
LANGUAGE: Final[str] = "vi-VN"
SCENES_MIN: Final[int] = 4
SCENES_MAX: Final[int] = 6
FRAMINGS_MIN_DISTINCT: Final[int] = 3
HOOK_MAX_START_MS: Final[int] = 1500
# Measured with Edge TTS vi-VN voices at the storyboard rates: about 3.3 to 4.0
# syllables per second including the pauses between words.
ESTIMATE_SYLLABLES_PER_SECOND: Final[float] = 3.4
SCENE_TAIL_HOLD_MS: Final[int] = 350
DEFAULT_LINE_PAUSE_MS: Final[int] = 300

# Action kinds the rig/action layer must implement (slice B). Anything else in a
# storyboard is rejected.
ACTION_KINDS: Final[tuple[str, ...]] = (
    "idle",
    "walk",
    "look_at",
    "turn_body",
    "wave",
    "welcome",
    "point",
    "nod",
    "shrug",
    "sigh",
    "wipe_counter",
    "reach_take",
    "stroke_prop",
    "stir",
    "ladle_to_bowl",
    "add_topping",
    "carry_bowl",
    "place_bowl",
    "sit",
    "shiver",
    "sniff",
    "taste",
    "smile_big",
    "wipe_eye",
    "thumbs_up",
    "cover_mouth",
    "blush",
    "idle_talk",
)
CAMERA_MOVE_KINDS: Final[tuple[str, ...]] = (
    "hold",
    "push_in",
    "pull_back",
    "pan_to",
    "follow",
    "handheld",
)
TRANSITION_KINDS: Final[tuple[str, ...]] = (
    "cut",
    "crossfade",
    "whip_pan",
    "steam_dissolve",
    "dip",
)
GRADES: Final[tuple[str, ...]] = ("dawn", "morning", "warm")
MOODS: Final[tuple[str, ...]] = (
    "sparse",
    "tender",
    "curious",
    "busy_warm",
    "resolved",
)
AMBIENCE_KINDS: Final[tuple[str, ...]] = (
    "street_dawn",
    "street",
    "kitchen",
    "bubbling",
    "kitchen_hum",
    "stove_glow",
    "sizzle",
    "soft_chatter",
)
EFFECT_KINDS: Final[tuple[str, ...]] = (
    "steam_burst",
    "steam_rise",
    "lantern_glow",
    "passersby",
    "broth_splash",
)
SFX_KINDS: Final[tuple[str, ...]] = (
    "footstep",
    "clink",
    "bowl_place",
    "chair",
    "cloth",
    "whoosh",
    "steam_hiss",
    "door_bell",
)
PROP_KINDS: Final[tuple[str, ...]] = (
    "pot",
    "bowl_pho",
    "ladle",
    "spoon",
    "tongs",
    "stool",
    "table",
    "counter",
    "stove",
    "hanging_ladle",
    "lantern",
    "sign",
)
FACINGS: Final[tuple[str, ...]] = ("left", "right", "front")
CAST_STATES: Final[tuple[str, ...]] = ("standing", "sitting")
FOCUS_PARTS: Final[tuple[str, ...]] = ("head", "body", "hands")
NAMED_TARGETS: Final[tuple[str, ...]] = ("street", "door", "camera", "counter")
MOVE_TO_KEYS: Final[tuple[str, ...]] = ("zoom", "framing", "x", "y", "amplitude")
TRANSITION_DIRECTIONS: Final[tuple[str, ...]] = ("left", "right")
# Action params that name a prop of the scene and a target to look at.
PROP_PARAMS: Final[tuple[str, ...]] = ("prop", "from_prop", "to_prop")
TARGET_PARAMS: Final[tuple[str, ...]] = ("target",)

_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9_]*")
_VOICE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[a-z]{2,3}-[A-Z]{2,}(-[A-Za-z0-9]+)?-[A-Za-z0-9]+Neural"
)
_RATE_PATTERN: Final[re.Pattern[str]] = re.compile(r"[+-][0-9]{1,3}%")
_PITCH_PATTERN: Final[re.Pattern[str]] = re.compile(r"[+-][0-9]{1,3}Hz")
_COLOR_PATTERN: Final[re.Pattern[str]] = re.compile(r"#[0-9A-Fa-f]{6}")


class Framing(StrEnum):
    """Shot sizes; the zoom of each is in ``animation.camera.FRAMING_ZOOM``."""

    WIDE = "WIDE"
    MEDIUM = "MEDIUM"
    CLOSE_UP = "CLOSE_UP"
    EXTREME_CLOSE = "EXTREME_CLOSE"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FormatSpec:
    width: int
    height: int
    fps: int
    duration_ms_range: tuple[int, int]
    fade_out_ms: int = 350


@dataclass(frozen=True)
class StyleSpec:
    background_style_suffix: str
    negative_prompt: str
    palette: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class VoiceSpec:
    id: str
    voice_id: str
    rate: str = "+0%"
    pitch: str = "+0Hz"


@dataclass(frozen=True)
class MusicSpec:
    bpm: int
    key: str


@dataclass(frozen=True)
class AudioSpec:
    target_lufs: float
    true_peak_db: float
    music: MusicSpec
    duck_depth_db: float
    fade_out_ms: int = 500


@dataclass(frozen=True)
class LocationSpec:
    id: str
    prompt: str
    seed: int
    grades: tuple[str, ...]


@dataclass(frozen=True)
class CharacterRef:
    id: str
    rig: str


@dataclass(frozen=True)
class Anchor:
    """Where something starts, relative to its scene.

    Exactly one of ``scene_ms`` (from the scene start), ``scene_end_ms`` (this
    many ms before the scene end), ``line`` (a line of the scene, with ``word``
    for a word start/end or none for the line start/end) and ``after_action``
    (the end of another action) is set; ``offset_ms`` is added to the result.
    ``edge`` is ``"start"`` or ``"end"``.
    """

    scene_ms: int | None = None
    scene_end_ms: int | None = None
    line: str | None = None
    word: int | None = None
    edge: str = "start"
    after_action: str | None = None
    offset_ms: int = 0

    @property
    def kind(self) -> str:
        if self.scene_ms is not None:
            return "scene_ms"
        if self.scene_end_ms is not None:
            return "scene_end"
        if self.after_action is not None:
            return "after_action"
        if self.word is not None:
            return "word"
        return "line_edge"


@dataclass(frozen=True)
class CastEntry:
    character: str
    x: float
    y_ground: float
    facing: str = "front"
    state: str = "standing"
    # Locations where the character is on screen; empty means everywhere.
    visible_in: tuple[str, ...] = ()


@dataclass(frozen=True)
class PropPlacement:
    id: str
    kind: str
    at: tuple[float, float]
    state: str = "default"
    # Location the prop stands in; ``None`` means the scene location.
    location: str | None = None


@dataclass(frozen=True)
class NarrationLine:
    """One spoken line (narrator or character) of a scene."""

    id: str
    voice: str
    text: str
    at: Anchor
    pause_after_ms: int = DEFAULT_LINE_PAUSE_MS


@dataclass(frozen=True)
class ActionSpec:
    id: str
    character: str
    kind: str
    at: Anchor
    duration_ms: int
    ease_in_ms: int = 0
    ease_out_ms: int = 0
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EffectSpec:
    """A scenery effect (steam, glow, passers-by) that no character performs."""

    id: str
    kind: str
    at: Anchor
    duration_ms: int
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FocusSpec:
    """What a shot looks at: a character, several (two-shot), a prop or a point."""

    character: str | None = None
    characters: tuple[str, ...] = ()
    prop: str | None = None
    x: float | None = None
    y: float | None = None
    part: str = "head"


@dataclass(frozen=True)
class CameraMoveSpec:
    kind: str
    t0: int
    t1: int
    to: Mapping[str, Any] = field(default_factory=dict)
    ease: str | None = None
    from_: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ShotSpec:
    id: str
    start_ms: int
    framing: Framing
    focus: FocusSpec
    moves: tuple[CameraMoveSpec, ...] = ()
    location: str | None = None


@dataclass(frozen=True)
class TransitionSpec:
    kind: str
    duration_ms: int
    direction: str | None = None


@dataclass(frozen=True)
class Scene:
    id: str
    location: str
    grade: str
    mood: str
    min_duration_ms: int
    cast: tuple[CastEntry, ...]
    props: tuple[PropPlacement, ...]
    lines: tuple[NarrationLine, ...]
    actions: tuple[ActionSpec, ...]
    shots: tuple[ShotSpec, ...]
    ambience: tuple[str, ...] = ()
    transition_out: TransitionSpec | None = None
    effects: tuple[EffectSpec, ...] = ()

    def shot_location(self, shot: ShotSpec) -> str:
        return shot.location or self.location


@dataclass(frozen=True)
class Storyboard:
    schema_version: int
    id: str
    title: str
    language: str
    format: FormatSpec
    style: StyleSpec
    voices: tuple[VoiceSpec, ...]
    audio: AudioSpec
    locations: tuple[LocationSpec, ...]
    characters: tuple[CharacterRef, ...]
    scenes: tuple[Scene, ...]

    def voice(self, voice_id: str) -> VoiceSpec:
        for voice in self.voices:
            if voice.id == voice_id:
                return voice
        raise KeyError(voice_id)

    def location(self, location_id: str) -> LocationSpec:
        for location in self.locations:
            if location.id == location_id:
                return location
        raise KeyError(location_id)

    def all_lines(self) -> tuple[NarrationLine, ...]:
        return tuple(line for scene in self.scenes for line in scene.lines)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def text_tokens(text: str) -> list[str]:
    """The whitespace tokens of ``text`` that contain a letter or digit.

    Vietnamese writes one syllable per token, so this is also the syllable list
    and the word list that Edge's word boundaries are aligned to.
    """
    return [token for token in text.split() if any(c.isalnum() for c in token)]


def syllable_count(text: str) -> int:
    return len(text_tokens(text))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class _Reader:
    """Typed access to JSON objects that records issues instead of raising."""

    def __init__(self) -> None:
        self.issues: list[str] = []

    def add(self, path: str, message: str) -> None:
        self.issues.append(f"{path}: {message}")

    def obj(self, value: Any, path: str) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        self.add(path, "must be an object")
        return {}

    def seq(self, data: Mapping[str, Any], key: str, path: str) -> list[Any]:
        value = data.get(key)
        if isinstance(value, list):
            return value
        self.add(f"{path}.{key}", "must be a list")
        return []

    def opt_seq(self, data: Mapping[str, Any], key: str, path: str) -> list[Any]:
        if key not in data:
            return []
        return self.seq(data, key, path)

    def unknown(
        self, data: Mapping[str, Any], allowed: Sequence[str], path: str
    ) -> None:
        for key in data:
            if key not in allowed:
                self.add(path, f"unknown key '{key}'")

    def string(
        self, data: Mapping[str, Any], key: str, path: str, default: str | None = None
    ) -> str:
        value = data.get(key, default)
        if isinstance(value, str) and value.strip():
            return value
        self.add(f"{path}.{key}", "must be a non-empty string")
        return default or ""

    def identifier(self, data: Mapping[str, Any], key: str, path: str) -> str:
        value = self.string(data, key, path)
        if value and not _ID_PATTERN.fullmatch(value):
            self.add(f"{path}.{key}", f"'{value}' must match [a-z][a-z0-9_]*")
        return value

    def integer(
        self,
        data: Mapping[str, Any],
        key: str,
        path: str,
        *,
        default: int | None = None,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        value = data.get(key, default)
        fallback = default if default is not None else (minimum or 0)
        if isinstance(value, bool) or not isinstance(value, int):
            self.add(f"{path}.{key}", "must be an integer")
            return fallback
        if (minimum is not None and value < minimum) or (
            maximum is not None and value > maximum
        ):
            self.add(f"{path}.{key}", f"{value} is out of range")
            return fallback
        return value

    def number(
        self,
        data: Mapping[str, Any],
        key: str,
        path: str,
        *,
        default: float | None = None,
    ) -> float:
        value = data.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int | float):
            self.add(f"{path}.{key}", "must be a number")
            return default if default is not None else 0.0
        return float(value)

    def choice(
        self,
        data: Mapping[str, Any],
        key: str,
        path: str,
        options: Sequence[str],
        default: str | None = None,
    ) -> str:
        value = data.get(key, default)
        if value in options:
            return str(value)
        self.add(f"{path}.{key}", f"'{value}' is not one of {', '.join(options)}")
        return default or options[0]


def _parse_anchor(reader: _Reader, value: Any, path: str) -> Anchor:
    data = reader.obj(value, path)
    reader.unknown(
        data,
        (
            "scene_ms",
            "scene_end_ms",
            "line",
            "word",
            "edge",
            "after_action",
            "offset_ms",
        ),
        path,
    )
    modes = [
        key
        for key in ("scene_ms", "scene_end_ms", "line", "after_action")
        if key in data
    ]
    if len(modes) != 1:
        reader.add(
            path,
            "needs exactly one of scene_ms, scene_end_ms, line, after_action",
        )
        return Anchor(scene_ms=0)
    offset = reader.integer(data, "offset_ms", path, default=0)
    edge = reader.choice(data, "edge", path, ("start", "end"), "start")
    mode = modes[0]
    if mode == "scene_ms":
        return Anchor(
            scene_ms=reader.integer(data, "scene_ms", path, minimum=0),
            offset_ms=offset,
        )
    if mode == "scene_end_ms":
        return Anchor(
            scene_end_ms=reader.integer(data, "scene_end_ms", path, minimum=0),
            offset_ms=offset,
        )
    if mode == "after_action":
        return Anchor(
            after_action=reader.string(data, "after_action", path), offset_ms=offset
        )
    word = reader.integer(data, "word", path, minimum=0) if "word" in data else None
    return Anchor(
        line=reader.string(data, "line", path), word=word, edge=edge, offset_ms=offset
    )


def _parse_format(reader: _Reader, value: Any) -> FormatSpec:
    path = "format"
    data = reader.obj(value, path)
    reader.unknown(
        data, ("width", "height", "fps", "duration_ms_range", "fade_out_ms"), path
    )
    window = data.get("duration_ms_range")
    low, high = 0, 0
    if (
        isinstance(window, list)
        and len(window) == 2
        and all(isinstance(v, int) and not isinstance(v, bool) for v in window)
        and 0 < window[0] <= window[1]
    ):
        low, high = window
    else:
        reader.add(f"{path}.duration_ms_range", "must be [min_ms, max_ms]")
    return FormatSpec(
        width=reader.integer(data, "width", path, minimum=64),
        height=reader.integer(data, "height", path, minimum=64),
        fps=reader.integer(data, "fps", path, minimum=1, maximum=120),
        duration_ms_range=(low, high),
        fade_out_ms=reader.integer(data, "fade_out_ms", path, default=350, minimum=0),
    )


def _parse_style(reader: _Reader, value: Any) -> StyleSpec:
    path = "style"
    data = reader.obj(value, path)
    reader.unknown(
        data, ("background_style_suffix", "negative_prompt", "palette"), path
    )
    palette_raw = reader.obj(data.get("palette", {}), f"{path}.palette")
    palette: dict[str, str] = {}
    for name, color in palette_raw.items():
        if isinstance(color, str) and _COLOR_PATTERN.fullmatch(color):
            palette[str(name)] = color
        else:
            reader.add(f"{path}.palette.{name}", "must be a #RRGGBB colour")
    return StyleSpec(
        background_style_suffix=reader.string(data, "background_style_suffix", path),
        negative_prompt=reader.string(data, "negative_prompt", path),
        palette=palette,
    )


def _parse_voices(reader: _Reader, value: Any) -> tuple[VoiceSpec, ...]:
    data = reader.obj(value, "voices")
    voices: list[VoiceSpec] = []
    for voice_key, raw in data.items():
        path = f"voices.{voice_key}"
        item = reader.obj(raw, path)
        reader.unknown(item, ("voice_id", "rate", "pitch"), path)
        if not _ID_PATTERN.fullmatch(str(voice_key)):
            reader.add(path, "the voice id must match [a-z][a-z0-9_]*")
        voice_id = reader.string(item, "voice_id", path)
        if voice_id and not _VOICE_PATTERN.fullmatch(voice_id):
            reader.add(f"{path}.voice_id", f"'{voice_id}' is not an Edge neural voice")
        rate = reader.string(item, "rate", path, "+0%")
        pitch = reader.string(item, "pitch", path, "+0Hz")
        if not _RATE_PATTERN.fullmatch(rate):
            reader.add(f"{path}.rate", f"'{rate}' must look like -6%")
        if not _PITCH_PATTERN.fullmatch(pitch):
            reader.add(f"{path}.pitch", f"'{pitch}' must look like -4Hz")
        voices.append(VoiceSpec(str(voice_key), voice_id, rate, pitch))
    if not voices:
        reader.add("voices", "needs at least one voice")
    return tuple(voices)


def _parse_audio(reader: _Reader, value: Any) -> AudioSpec:
    path = "audio"
    data = reader.obj(value, path)
    reader.unknown(
        data,
        ("target_lufs", "true_peak_db", "music", "duck_depth_db", "fade_out_ms"),
        path,
    )
    music = reader.obj(data.get("music"), f"{path}.music")
    reader.unknown(music, ("bpm", "key"), f"{path}.music")
    return AudioSpec(
        target_lufs=reader.number(data, "target_lufs", path),
        true_peak_db=reader.number(data, "true_peak_db", path),
        music=MusicSpec(
            bpm=reader.integer(music, "bpm", f"{path}.music", minimum=40, maximum=220),
            key=reader.string(music, "key", f"{path}.music"),
        ),
        duck_depth_db=reader.number(data, "duck_depth_db", path),
        fade_out_ms=reader.integer(data, "fade_out_ms", path, default=500, minimum=0),
    )


def _parse_location(reader: _Reader, value: Any, path: str) -> LocationSpec:
    data = reader.obj(value, path)
    reader.unknown(data, ("id", "prompt", "seed", "grades"), path)
    grades = tuple(str(g) for g in reader.opt_seq(data, "grades", path))
    for grade in grades:
        if grade not in GRADES:
            reader.add(f"{path}.grades", f"'{grade}' is not one of {', '.join(GRADES)}")
    return LocationSpec(
        id=reader.identifier(data, "id", path),
        prompt=reader.string(data, "prompt", path),
        seed=reader.integer(data, "seed", path, minimum=0),
        grades=grades,
    )


def _parse_cast(reader: _Reader, value: Any, path: str) -> CastEntry:
    data = reader.obj(value, path)
    reader.unknown(
        data, ("character", "x", "y_ground", "facing", "state", "visible_in"), path
    )
    return CastEntry(
        character=reader.string(data, "character", path),
        x=reader.number(data, "x", path),
        y_ground=reader.number(data, "y_ground", path),
        facing=reader.choice(data, "facing", path, FACINGS, "front"),
        state=reader.choice(data, "state", path, CAST_STATES, "standing"),
        visible_in=tuple(str(v) for v in reader.opt_seq(data, "visible_in", path)),
    )


def _parse_prop(reader: _Reader, value: Any, path: str) -> PropPlacement:
    data = reader.obj(value, path)
    reader.unknown(data, ("id", "kind", "at", "state", "location"), path)
    at = data.get("at")
    point = (0.0, 0.0)
    if (
        isinstance(at, list)
        and len(at) == 2
        and all(isinstance(v, int | float) and not isinstance(v, bool) for v in at)
    ):
        point = (float(at[0]), float(at[1]))
    else:
        reader.add(f"{path}.at", "must be [x, y]")
    return PropPlacement(
        id=reader.identifier(data, "id", path),
        kind=reader.choice(data, "kind", path, PROP_KINDS),
        at=point,
        state=reader.string(data, "state", path, "default"),
        location=reader.string(data, "location", path) if "location" in data else None,
    )


def _parse_line(reader: _Reader, value: Any, path: str) -> NarrationLine:
    data = reader.obj(value, path)
    reader.unknown(data, ("id", "voice", "text", "at", "pause_after_ms"), path)
    return NarrationLine(
        id=reader.identifier(data, "id", path),
        voice=reader.string(data, "voice", path),
        text=reader.string(data, "text", path),
        at=_parse_anchor(reader, data.get("at"), f"{path}.at"),
        pause_after_ms=reader.integer(
            data, "pause_after_ms", path, default=DEFAULT_LINE_PAUSE_MS, minimum=0
        ),
    )


def _parse_action(reader: _Reader, value: Any, path: str) -> ActionSpec:
    data = reader.obj(value, path)
    reader.unknown(
        data,
        (
            "id",
            "character",
            "kind",
            "at",
            "duration_ms",
            "ease_in_ms",
            "ease_out_ms",
            "params",
        ),
        path,
    )
    kind = reader.string(data, "kind", path)
    return ActionSpec(
        id=reader.identifier(data, "id", path),
        character=reader.string(data, "character", path),
        kind=kind,
        at=_parse_anchor(reader, data.get("at"), f"{path}.at"),
        duration_ms=reader.integer(data, "duration_ms", path, minimum=1),
        ease_in_ms=reader.integer(data, "ease_in_ms", path, default=0, minimum=0),
        ease_out_ms=reader.integer(data, "ease_out_ms", path, default=0, minimum=0),
        params=dict(reader.obj(data.get("params", {}), f"{path}.params")),
    )


def _parse_effect(reader: _Reader, value: Any, path: str) -> EffectSpec:
    data = reader.obj(value, path)
    reader.unknown(data, ("id", "kind", "at", "duration_ms", "params"), path)
    return EffectSpec(
        id=reader.identifier(data, "id", path),
        kind=reader.choice(data, "kind", path, EFFECT_KINDS),
        at=_parse_anchor(reader, data.get("at"), f"{path}.at"),
        duration_ms=reader.integer(data, "duration_ms", path, minimum=1),
        params=dict(reader.obj(data.get("params", {}), f"{path}.params")),
    )


def _parse_focus(reader: _Reader, value: Any, path: str) -> FocusSpec:
    data = reader.obj(value, path)
    reader.unknown(data, ("character", "characters", "prop", "x", "y", "part"), path)
    modes = [key for key in ("character", "characters", "prop") if key in data]
    has_point = "x" in data or "y" in data
    if len(modes) + (1 if has_point else 0) != 1:
        reader.add(path, "needs exactly one of character, characters, prop, x+y")
    characters = tuple(str(c) for c in reader.opt_seq(data, "characters", path))
    if "characters" in data and len(characters) < 2:
        reader.add(f"{path}.characters", "needs at least two characters")
    return FocusSpec(
        character=reader.string(data, "character", path)
        if "character" in data
        else None,
        characters=characters,
        prop=reader.string(data, "prop", path) if "prop" in data else None,
        x=reader.number(data, "x", path) if has_point else None,
        y=reader.number(data, "y", path) if has_point else None,
        part=reader.choice(data, "part", path, FOCUS_PARTS, "head"),
    )


def _parse_move_values(
    reader: _Reader, value: Any, path: str, *, allow_framing: bool = True
) -> dict[str, Any]:
    data = reader.obj(value, path)
    reader.unknown(data, MOVE_TO_KEYS, path)
    parsed: dict[str, Any] = {}
    for key, item in data.items():
        if key == "framing":
            if allow_framing and item in Framing._value2member_map_:
                parsed[key] = str(item)
            else:
                reader.add(f"{path}.framing", f"'{item}' is not a framing")
        elif isinstance(item, int | float) and not isinstance(item, bool):
            parsed[key] = float(item)
        else:
            reader.add(f"{path}.{key}", "must be a number")
    return parsed


def _parse_move(reader: _Reader, value: Any, path: str) -> CameraMoveSpec:
    data = reader.obj(value, path)
    reader.unknown(data, ("kind", "t0", "t1", "to", "from", "ease"), path)
    kind = reader.choice(data, "kind", path, CAMERA_MOVE_KINDS)
    t0 = reader.integer(data, "t0", path, minimum=0)
    t1 = reader.integer(data, "t1", path, minimum=0)
    if t1 < t0:
        reader.add(path, "t1 must not be before t0")
    to = _parse_move_values(reader, data.get("to", {}), f"{path}.to")
    from_ = (
        _parse_move_values(reader, data["from"], f"{path}.from")
        if "from" in data
        else None
    )
    if kind in ("push_in", "pull_back") and "zoom" not in to and "framing" not in to:
        reader.add(f"{path}.to", f"{kind} needs 'zoom' or 'framing'")
    if kind == "pan_to" and not ("x" in to and "y" in to):
        reader.add(f"{path}.to", "pan_to needs 'x' and 'y'")
    ease = data.get("ease")
    if ease is not None and ease not in EASINGS:
        reader.add(f"{path}.ease", f"'{ease}' is not one of {', '.join(EASINGS)}")
        ease = None
    return CameraMoveSpec(kind=kind, t0=t0, t1=t1, to=to, ease=ease, from_=from_)


def _parse_shot(reader: _Reader, value: Any, path: str) -> ShotSpec:
    data = reader.obj(value, path)
    reader.unknown(
        data, ("id", "start_ms", "framing", "focus", "moves", "location"), path
    )
    moves = tuple(
        _parse_move(reader, raw, f"{path}.moves[{index}]")
        for index, raw in enumerate(reader.opt_seq(data, "moves", path))
    )
    return ShotSpec(
        id=reader.identifier(data, "id", path),
        start_ms=reader.integer(data, "start_ms", path, minimum=0),
        framing=Framing(
            reader.choice(data, "framing", path, [f.value for f in Framing])
        ),
        focus=_parse_focus(reader, data.get("focus"), f"{path}.focus"),
        moves=moves,
        location=reader.string(data, "location", path) if "location" in data else None,
    )


def _parse_transition(reader: _Reader, value: Any, path: str) -> TransitionSpec:
    data = reader.obj(value, path)
    reader.unknown(data, ("kind", "duration_ms", "direction"), path)
    kind = reader.choice(data, "kind", path, TRANSITION_KINDS)
    direction = data.get("direction")
    if direction is not None and direction not in TRANSITION_DIRECTIONS:
        reader.add(f"{path}.direction", "must be 'left' or 'right'")
        direction = None
    return TransitionSpec(
        kind=kind,
        duration_ms=reader.integer(data, "duration_ms", path, default=0, minimum=0),
        direction=direction,
    )


def _parse_scene(reader: _Reader, value: Any, path: str) -> Scene:
    data = reader.obj(value, path)
    reader.unknown(
        data,
        (
            "id",
            "location",
            "grade",
            "mood",
            "min_duration_ms",
            "cast",
            "props",
            "lines",
            "actions",
            "shots",
            "ambience",
            "transition_out",
            "effects",
        ),
        path,
    )
    ambience = tuple(str(a) for a in reader.opt_seq(data, "ambience", path))
    for name in ambience:
        if name not in AMBIENCE_KINDS:
            reader.add(f"{path}.ambience", f"'{name}' is not a known ambience")
    transition = (
        _parse_transition(reader, data["transition_out"], f"{path}.transition_out")
        if "transition_out" in data
        else None
    )
    return Scene(
        id=reader.identifier(data, "id", path),
        location=reader.string(data, "location", path),
        grade=reader.choice(data, "grade", path, GRADES),
        mood=reader.choice(data, "mood", path, MOODS),
        min_duration_ms=reader.integer(data, "min_duration_ms", path, minimum=1000),
        cast=tuple(
            _parse_cast(reader, raw, f"{path}.cast[{i}]")
            for i, raw in enumerate(reader.opt_seq(data, "cast", path))
        ),
        props=tuple(
            _parse_prop(reader, raw, f"{path}.props[{i}]")
            for i, raw in enumerate(reader.opt_seq(data, "props", path))
        ),
        lines=tuple(
            _parse_line(reader, raw, f"{path}.lines[{i}]")
            for i, raw in enumerate(reader.opt_seq(data, "lines", path))
        ),
        actions=tuple(
            _parse_action(reader, raw, f"{path}.actions[{i}]")
            for i, raw in enumerate(reader.opt_seq(data, "actions", path))
        ),
        shots=tuple(
            _parse_shot(reader, raw, f"{path}.shots[{i}]")
            for i, raw in enumerate(reader.seq(data, "shots", path))
        ),
        ambience=ambience,
        transition_out=transition,
        effects=tuple(
            _parse_effect(reader, raw, f"{path}.effects[{i}]")
            for i, raw in enumerate(reader.opt_seq(data, "effects", path))
        ),
    )


def parse_storyboard(data: Mapping[str, Any]) -> tuple[Storyboard, list[str]]:
    """Parse ``data`` into a ``Storyboard`` and the list of structural issues."""
    reader = _Reader()
    root = reader.obj(data, "storyboard")
    reader.unknown(
        root,
        (
            "schema_version",
            "id",
            "title",
            "language",
            "format",
            "style",
            "voices",
            "audio",
            "locations",
            "characters",
            "scenes",
        ),
        "storyboard",
    )
    version = reader.integer(root, "schema_version", "storyboard", minimum=1)
    if version != SCHEMA_VERSION:
        reader.add(
            "storyboard.schema_version", f"only version {SCHEMA_VERSION} is supported"
        )
    language = reader.string(root, "language", "storyboard", LANGUAGE)
    characters = []
    for index, raw in enumerate(reader.seq(root, "characters", "storyboard")):
        path = f"characters[{index}]"
        item = reader.obj(raw, path)
        reader.unknown(item, ("id", "rig"), path)
        characters.append(
            CharacterRef(
                reader.identifier(item, "id", path), reader.string(item, "rig", path)
            )
        )
    storyboard = Storyboard(
        schema_version=version,
        id=reader.identifier(root, "id", "storyboard"),
        title=reader.string(root, "title", "storyboard"),
        language=language,
        format=_parse_format(reader, root.get("format")),
        style=_parse_style(reader, root.get("style")),
        voices=_parse_voices(reader, root.get("voices")),
        audio=_parse_audio(reader, root.get("audio")),
        locations=tuple(
            _parse_location(reader, raw, f"locations[{i}]")
            for i, raw in enumerate(reader.seq(root, "locations", "storyboard"))
        ),
        characters=tuple(characters),
        scenes=tuple(
            _parse_scene(reader, raw, f"scenes[{i}]")
            for i, raw in enumerate(reader.seq(root, "scenes", "storyboard"))
        ),
    )
    return storyboard, reader.issues


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _duplicates(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    repeated: list[str] = []
    for value in values:
        if value in seen and value not in repeated:
            repeated.append(value)
        seen.add(value)
    return repeated


def _unique(issues: list[str], what: str, values: Sequence[str]) -> None:
    for value in _duplicates(values):
        issues.append(f"{what} id '{value}' is used more than once")


def estimate_scene_ms(scene: Scene) -> int:
    """Rough duration of ``scene`` before any speech exists (for validation)."""
    end = 0.0
    cursor: float | None = None
    for line in scene.lines:
        anchor = line.at
        if anchor.scene_ms is not None:
            start = float(anchor.scene_ms + anchor.offset_ms)
        elif anchor.kind == "line_edge" and cursor is not None:
            start = cursor + anchor.offset_ms
        else:
            start = end
        length = syllable_count(line.text) / ESTIMATE_SYLLABLES_PER_SECOND * 1000
        end = max(end, start + length)
        cursor = start + length + line.pause_after_ms
    return max(scene.min_duration_ms, round(end) + SCENE_TAIL_HOLD_MS)


def estimate_total_ms(storyboard: Storyboard) -> int:
    return sum(estimate_scene_ms(scene) for scene in storyboard.scenes)


def _validate_anchor(
    issues: list[str],
    where: str,
    anchor: Anchor,
    scene: Scene,
    line_words: Mapping[str, int],
    action_ids: set[str],
) -> None:
    if anchor.line is not None:
        if anchor.line not in line_words:
            issues.append(
                f"{where}: anchor line '{anchor.line}' is not a line of scene "
                f"'{scene.id}'"
            )
        elif anchor.word is not None and anchor.word >= line_words[anchor.line]:
            issues.append(
                f"{where}: word {anchor.word} is outside line '{anchor.line}' "
                f"({line_words[anchor.line]} syllables)"
            )
    if anchor.after_action is not None and anchor.after_action not in action_ids:
        issues.append(
            f"{where}: after_action '{anchor.after_action}' is not an action of "
            f"scene '{scene.id}'"
        )


def _shot_signature(scene: Scene) -> tuple[Any, ...]:
    return (
        tuple(scene.shot_location(shot) for shot in scene.shots),
        tuple(shot.framing for shot in scene.shots),
    )


def _validate_scene(
    issues: list[str],
    scene: Scene,
    storyboard: Storyboard,
    voice_ids: set[str],
    location_ids: set[str],
    character_ids: set[str],
    is_last: bool,
) -> None:
    where = f"scene '{scene.id}'"
    if scene.location not in location_ids:
        issues.append(f"{where}: unknown location '{scene.location}'")
    elif scene.grade not in storyboard.location(scene.location).grades:
        issues.append(
            f"{where}: grade '{scene.grade}' is not allowed by location "
            f"'{scene.location}'"
        )
    cast_ids = [entry.character for entry in scene.cast]
    _unique(issues, f"{where} cast", cast_ids)
    for character in cast_ids:
        if character not in character_ids:
            issues.append(f"{where}: cast member '{character}' is not a character")
    prop_ids = [prop.id for prop in scene.props]
    _unique(issues, f"{where} prop", prop_ids)
    line_words: dict[str, int] = {}
    for line in scene.lines:
        if line.voice not in voice_ids:
            issues.append(f"line '{line.id}': unknown voice '{line.voice}'")
        if not text_tokens(line.text):
            issues.append(f"line '{line.id}': the text has no words")
        line_words[line.id] = syllable_count(line.text)
    action_ids = {action.id for action in scene.actions}
    targets = set(NAMED_TARGETS) | set(prop_ids) | character_ids
    for line in scene.lines:
        _validate_anchor(
            issues, f"line '{line.id}'", line.at, scene, line_words, action_ids
        )
        if line.at.after_action is not None or line.at.scene_end_ms is not None:
            issues.append(
                f"line '{line.id}': a line starts at scene_ms or after another line"
            )
        earlier = [item.id for item in scene.lines[: scene.lines.index(line)]]
        if line.at.line is not None and line.at.line not in earlier:
            issues.append(
                f"line '{line.id}': must be anchored to a line declared before it"
            )
    for effect in scene.effects:
        label = f"effect '{effect.id}'"
        _validate_anchor(issues, label, effect.at, scene, line_words, action_ids)
        prop = effect.params.get("prop")
        if prop is not None and prop not in prop_ids:
            issues.append(f"{label}: param prop '{prop}' is not a prop of the scene")
    for entry in scene.cast:
        for place in entry.visible_in:
            if place not in location_ids:
                issues.append(
                    f"{where}: '{entry.character}' is visible_in unknown location "
                    f"'{place}'"
                )
    for prop in scene.props:
        if prop.location is not None and prop.location not in location_ids:
            issues.append(f"prop '{prop.id}': unknown location '{prop.location}'")
    for action in scene.actions:
        label = f"action '{action.id}'"
        if action.kind not in ACTION_KINDS:
            issues.append(f"{label}: unknown action kind '{action.kind}'")
        if action.character not in cast_ids:
            issues.append(f"{label}: '{action.character}' is not in the scene cast")
        _validate_anchor(issues, label, action.at, scene, line_words, action_ids)
        if action.at.after_action == action.id:
            issues.append(f"{label}: anchored to itself")
        if action.ease_in_ms + action.ease_out_ms > action.duration_ms:
            issues.append(f"{label}: ease_in_ms + ease_out_ms exceed duration_ms")
        for key in PROP_PARAMS:
            value = action.params.get(key)
            if value is not None and value not in prop_ids:
                issues.append(
                    f"{label}: param {key} '{value}' is not a prop of the scene"
                )
        for key in TARGET_PARAMS:
            value = action.params.get(key)
            if value is not None and value not in targets:
                issues.append(f"{label}: param {key} '{value}' is not a known target")
        sfx = action.params.get("sfx")
        names = [sfx] if isinstance(sfx, str) else sfx if isinstance(sfx, list) else []
        for name in names:
            if name not in SFX_KINDS:
                issues.append(f"{label}: unknown sfx '{name}'")
    if not scene.shots:
        issues.append(f"{where}: needs at least one shot")
    else:
        if scene.shots[0].start_ms != 0:
            issues.append(f"{where}: the first shot must start at 0")
        starts = [shot.start_ms for shot in scene.shots]
        if any(b <= a for a, b in zip(starts, starts[1:], strict=False)):
            issues.append(f"{where}: shot start_ms must be strictly increasing")
    for shot in scene.shots:
        label = f"shot '{shot.id}'"
        if shot.location is not None and shot.location not in location_ids:
            issues.append(f"{label}: unknown location '{shot.location}'")
        focus = shot.focus
        ids = [focus.character] if focus.character else list(focus.characters)
        for character in ids:
            if character not in cast_ids:
                issues.append(f"{label}: focus '{character}' is not in the scene cast")
        if focus.prop is not None and focus.prop not in prop_ids:
            issues.append(f"{label}: focus prop '{focus.prop}' is not in the scene")
    transition = scene.transition_out
    if transition is not None:
        if transition.kind == "dip" and not is_last:
            issues.append(f"{where}: 'dip' is only allowed on the last scene")
        if transition.kind == "cut" and transition.duration_ms:
            issues.append(f"{where}: a cut has no duration")
        if transition.kind != "cut" and transition.duration_ms <= 0:
            issues.append(f"{where}: transition '{transition.kind}' needs duration_ms")
        if transition.kind == "whip_pan" and transition.direction is None:
            issues.append(f"{where}: whip_pan needs a direction")
    elif not is_last:
        issues.append(f"{where}: transition_out is required except on the last scene")


def validate_storyboard(storyboard: Storyboard) -> list[str]:
    """Every semantic problem of ``storyboard`` (empty when it is valid)."""
    issues: list[str] = []
    voice_ids = {voice.id for voice in storyboard.voices}
    location_ids = {location.id for location in storyboard.locations}
    character_ids = {character.id for character in storyboard.characters}
    scenes = storyboard.scenes

    if storyboard.language != LANGUAGE:
        issues.append(f"language must be {LANGUAGE}")
    fmt = storyboard.format
    if fmt.duration_ms_range[0] <= 0:
        issues.append("format.duration_ms_range is invalid")
    _unique(issues, "voice", [voice.id for voice in storyboard.voices])
    _unique(issues, "location", [location.id for location in storyboard.locations])
    _unique(issues, "character", [character.id for character in storyboard.characters])
    _unique(issues, "scene", [scene.id for scene in scenes])
    _unique(issues, "line", [line.id for scene in scenes for line in scene.lines])
    _unique(
        issues,
        "action",
        [action.id for scene in scenes for action in scene.actions],
    )
    _unique(issues, "shot", [shot.id for scene in scenes for shot in scene.shots])
    _unique(
        issues,
        "effect",
        [effect.id for scene in scenes for effect in scene.effects],
    )

    if not SCENES_MIN <= len(scenes) <= SCENES_MAX:
        issues.append(f"needs {SCENES_MIN} to {SCENES_MAX} scenes, found {len(scenes)}")
    for index, scene in enumerate(scenes):
        _validate_scene(
            issues,
            scene,
            storyboard,
            voice_ids,
            location_ids,
            character_ids,
            index == len(scenes) - 1,
        )

    framings = {shot.framing for scene in scenes for shot in scene.shots}
    if len(framings) < FRAMINGS_MIN_DISTINCT:
        issues.append(
            f"needs at least {FRAMINGS_MIN_DISTINCT} distinct framings, "
            f"found {len(framings)}"
        )

    if scenes:
        first = scenes[0]
        starts = [
            line.at.scene_ms + line.at.offset_ms
            for line in first.lines
            if line.at.scene_ms is not None
        ]
        if not first.lines or not starts or min(starts) > HOOK_MAX_START_MS:
            issues.append(
                f"scene '{first.id}': needs a line starting by scene_ms "
                f"{HOOK_MAX_START_MS}"
            )
        moving = any(
            move.kind != "hold" and move.t0 <= HOOK_MAX_START_MS
            for shot in first.shots
            for move in shot.moves
        )
        if not moving:
            issues.append(
                f"scene '{first.id}': needs a moving hook (a camera move "
                f"starting by {HOOK_MAX_START_MS} ms)"
            )

    for previous, current in zip(scenes, scenes[1:], strict=False):
        same_look = _shot_signature(previous) == _shot_signature(current)
        kinds_previous = sorted(action.kind for action in previous.actions)
        kinds_current = sorted(action.kind for action in current.actions)
        if same_look and kinds_previous == kinds_current:
            issues.append(
                f"scenes '{previous.id}' and '{current.id}' look the same "
                "(same location, framings and actions)"
            )

    if scenes:
        estimate = estimate_total_ms(storyboard)
        low, high = fmt.duration_ms_range
        if not low <= estimate <= high:
            issues.append(
                f"estimated duration {estimate} ms is outside {low}-{high} ms"
            )
    return issues


def storyboard_from_dict(data: Mapping[str, Any]) -> Storyboard:
    """Parse and validate ``data``; raises ``StoryboardError`` with all issues."""
    storyboard, issues = parse_storyboard(data)
    if not issues:
        issues = validate_storyboard(storyboard)
    if issues:
        raise StoryboardError(issues)
    return storyboard


def load_storyboard(source: Path | str | Mapping[str, Any]) -> Storyboard:
    """Load a storyboard from a JSON file path or an already parsed dict."""
    if isinstance(source, Mapping):
        return storyboard_from_dict(source)
    path = Path(source)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise StoryboardError([f"cannot read {path}: {exc}"]) from exc
    except ValueError as exc:
        raise StoryboardError([f"{path} is not valid JSON: {exc}"]) from exc
    return storyboard_from_dict(data)
