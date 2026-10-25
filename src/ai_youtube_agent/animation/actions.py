"""Actions: the 28 action kinds as poses, root motion, prop events and speech.

``Choreography`` turns a ``ResolvedTimeline`` into pure functions of time: where
each character stands and faces, its full ``Pose`` (idle breathing and blinking,
the walk cycle, held props, then every active action blended on top in start
order with ease ramps), the state of every prop and the mouth while a line is
spoken. Nothing here needs Pillow and nothing keeps state between calls, so a
frame depends only on its time.
"""

from __future__ import annotations

import array
import bisect
import math
import random
import wave
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from ai_youtube_agent.animation.camera import STAGE_H, STAGE_W
from ai_youtube_agent.animation.easing import clamp, ease_out_back, lerp, smoothstep
from ai_youtube_agent.animation.rig import (
    DEFAULT_STRIDE,
    LIFT_PX,
    MAX_YAW_DEG,
    RIG,
    VISEME_SHAPES,
    WORLD_SCALE,
    Pose,
    Vec,
    facing_sign,
    foot_contacts,
    get_style,
    near_side,
    phase_at_distance,
    plan_walk,
    rest_hand,
    stride_for_speed,
    viseme_for_syllable,
    walk_cycle,
)
from ai_youtube_agent.animation.storyboard import (
    ACTION_KINDS,
    CastEntry,
    PropPlacement,
)
from ai_youtube_agent.animation.timeline import (
    ResolvedAction,
    ResolvedLine,
    ResolvedTimeline,
    SfxEvent,
)

# --- stage geometry ----------------------------------------
PROP_SCALE: Final[float] = WORLD_SCALE
# Stage pixels (the props are drawn in local units and scaled by PROP_SCALE).
SURFACE_TOP: Final[dict[str, float]] = {
    "table": 238.0 * PROP_SCALE,
    "counter": 262.0 * PROP_SCALE,
    "stove": 250.0 * PROP_SCALE,
}
SURFACE_HALF_W: Final[dict[str, float]] = {
    "table": 150.0 * PROP_SCALE,
    "counter": 235.0 * PROP_SCALE,
    "stove": 150.0 * PROP_SCALE,
}
RESTING_KINDS: Final[frozenset[str]] = frozenset(
    {"pot", "bowl_pho", "ladle", "spoon", "tongs"}
)
POT_H: Final[float] = 118.0 * PROP_SCALE
BOWL_H: Final[float] = 62.0 * PROP_SCALE
STOOL_H: Final[float] = 118.0 * PROP_SCALE
LADLE_REST_ANGLE: Final[float] = 26.0  # leaning in the pot, handle sticking out
VOICE_CHARACTERS: Final[dict[str, str]] = {"customer": "chu_ba"}

IDLE_YAW: Final[float] = 0.7
WALK_YAW: Final[float] = 0.92
FACING_YAW: Final[dict[str, float]] = {
    "left": -IDLE_YAW,
    "right": IDLE_YAW,
    "front": 0.0,
}
TURN_MS: Final[float] = 450.0
HOLD_BLEND_MS: Final[float] = 260.0
MIN_RAMP_MS: Final[float] = 110.0
DEFAULT_WALK_RAMP_MS: Final[float] = 230.0
ENVELOPE_HOP_MS: Final[int] = 20
SPEECH_ATTACK_MS: Final[float] = 35.0
SPEECH_RELEASE_MS: Final[float] = 55.0

Point = tuple[float, float]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _rot(vec: Vec, degrees: float) -> Vec:
    a = math.radians(degrees)
    c, s = math.cos(a), math.sin(a)
    return (vec[0] * c - vec[1] * s, vec[0] * s + vec[1] * c)


def _add(a: Vec, b: Vec) -> Vec:
    return (a[0] + b[0], a[1] + b[1])


def _mix(a: Vec, b: Vec, t: float) -> Vec:
    return (lerp(a[0], b[0], t), lerp(a[1], b[1], t))


def _seg(u: float, a: float, b: float) -> float:
    """Smooth 0..1 ramp of ``u`` between ``a`` and ``b``."""
    return smoothstep(a, b, u)


def path(u: float, keys: Sequence[tuple[float, Vec]]) -> Vec:
    """Position along keyframes ``(u, point)`` with a smooth step between them."""
    if u <= keys[0][0]:
        return keys[0][1]
    for (u0, p0), (u1, p1) in zip(keys, keys[1:], strict=False):
        if u <= u1:
            return _mix(p0, p1, smoothstep(u0, u1, u))
    return keys[-1][1]


def scalar_path(u: float, keys: Sequence[tuple[float, float]]) -> float:
    if u <= keys[0][0]:
        return keys[0][1]
    for (u0, v0), (u1, v1) in zip(keys, keys[1:], strict=False):
        if u <= u1:
            return lerp(v0, v1, smoothstep(u0, u1, u))
    return keys[-1][1]


def ramp_weight(action: ResolvedAction, t_ms: float) -> float:
    """The ease-in/ease-out weight of an action at ``t_ms`` (0 outside it)."""
    if not action.start_ms <= t_ms < action.end_ms:
        return 0.0
    duration = action.end_ms - action.start_ms
    ease_in = max(float(action.ease_in_ms), MIN_RAMP_MS)
    ease_out = max(float(action.ease_out_ms), MIN_RAMP_MS)
    scale = min(1.0, duration / (ease_in + ease_out)) if duration else 1.0
    return smoothstep(0, ease_in * scale, t_ms - action.start_ms) * smoothstep(
        0, ease_out * scale, action.end_ms - t_ms
    )


# ---------------------------------------------------------------------------
# Speech envelope and mouth
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Envelope:
    """Loudness of a spoken line (0..1) sampled every ``hop_ms``."""

    hop_ms: int
    values: tuple[float, ...]

    def at(self, t_ms: float) -> float:
        if not self.values:
            return 0.0
        position = max(0.0, t_ms) / self.hop_ms
        index = int(position)
        if index >= len(self.values) - 1:
            return self.values[-1]
        return lerp(self.values[index], self.values[index + 1], position - index)


def load_envelope(wav_path: Path, hop_ms: int = ENVELOPE_HOP_MS) -> Envelope | None:
    """RMS envelope of a mono 16-bit WAV, or ``None`` if it cannot be read."""
    try:
        with wave.open(str(wav_path), "rb") as handle:
            if handle.getsampwidth() != 2:
                return None
            rate, channels = handle.getframerate(), handle.getnchannels()
            frames = handle.readframes(handle.getnframes())
    except (OSError, EOFError, wave.Error):
        return None
    samples = array.array("h")
    samples.frombytes(frames[: len(frames) // 2 * 2])
    if channels > 1:
        samples = samples[::channels]
    window = max(1, rate * hop_ms // 1000)
    levels: list[float] = []
    for start in range(0, len(samples), window):
        chunk = samples[start : start + window]
        if not chunk:
            break
        levels.append(math.sqrt(sum(s * s for s in chunk) / len(chunk)))
    if not levels:
        return None
    ordered = sorted(levels)
    reference = ordered[int(len(ordered) * 0.95)] or 1.0
    return Envelope(hop_ms, tuple(min(1.0, value / reference) for value in levels))


def speech_mouth(
    line: ResolvedLine, t_ms: float, envelope: Envelope | None
) -> tuple[float, float] | None:
    """``(mouth_open, mouth_wide)`` while ``line`` is spoken, else ``None``.

    Each word opens the mouth with the shape of its vowel (a viseme) and the
    loudness envelope of the line, if known, scales it; between words the mouth
    closes.
    """
    if not line.start_ms <= t_ms <= line.end_ms + 60:
        return None
    best = 0.0
    wide = 0.0
    for word in line.words:
        start, end = word.start_ms - SPEECH_ATTACK_MS, word.end_ms + SPEECH_RELEASE_MS
        if not start <= t_ms <= end:
            continue
        rise = smoothstep(start, start + SPEECH_ATTACK_MS + 30, t_ms)
        fall = smoothstep(end, end - SPEECH_RELEASE_MS - 30, t_ms)
        span = max(1.0, word.end_ms - word.start_ms)
        pulse = 0.55 + 0.45 * math.sin(math.pi * clamp((t_ms - word.start_ms) / span))
        shape_open, shape_wide = VISEME_SHAPES[viseme_for_syllable(word.text)]
        level = envelope.at(t_ms - line.start_ms) if envelope else 0.8
        openness = shape_open * rise * fall * pulse * clamp(0.4 + level)
        if openness >= best:
            best, wide = openness, shape_wide
    return best, wide


# ---------------------------------------------------------------------------
# Runtime model of a scene
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PropRuntime:
    id: str
    kind: str
    x: float  # stage pixels
    y: float  # anchor: base for standing props, hook for hanging ones
    state: str
    location: str | None
    angle: float = 0.0  # rest rotation (degrees)
    host: str | None = None

    @property
    def base(self) -> Point:
        return (self.x, self.y)

    def grab_point(self) -> Point:
        """Where a hand takes the prop."""
        if self.kind == "bowl_pho":
            return (self.x, self.y - BOWL_H * 0.55)
        if self.kind == "hanging_ladle":
            return (self.x, self.y + 84.0 * PROP_SCALE)
        return (self.x, self.y)

    @property
    def top(self) -> Point:
        """Where something is put down on the prop (its upper surface)."""
        return (self.x, self.y - SURFACE_TOP.get(self.kind, 0.0))


@dataclass(frozen=True)
class MoveSegment:
    """Root motion of a walk or a carry: a trapezoid speed profile."""

    action_id: str
    kind: str
    t0: float
    t1: float
    ramp_in: float
    ramp_out: float
    x0: float
    x1: float
    y0: float
    y1: float
    stride: float
    steps: int

    @property
    def distance(self) -> float:
        return math.hypot(self.x1 - self.x0, self.y1 - self.y0)

    @property
    def direction(self) -> float:
        return 1.0 if self.x1 >= self.x0 else -1.0

    def progress(self, t: float) -> float:
        """Fraction of the path covered at ``t`` (0..1)."""
        duration = self.t1 - self.t0
        if duration <= 0:
            return 1.0
        a, b = self.ramp_in, self.ramp_out
        if a + b > duration:
            scale = duration / (a + b)
            a, b = a * scale, b * scale
        v = 1.0 / (duration - (a + b) / 2)
        s = clamp(t - self.t0, 0.0, duration)
        if s < a:
            return v * s * s / (2 * a)
        if s <= duration - b:
            return v * (a / 2 + (s - a))
        rest = duration - s
        return 1.0 - v * rest * rest / (2 * b)

    def time_at_distance(self, distance: float) -> float:
        """The time at which ``distance`` pixels have been walked."""
        target = clamp(distance / self.distance) if self.distance else 1.0
        low, high = self.t0, self.t1
        for _ in range(28):
            mid = (low + high) / 2
            if self.progress(mid) < target:
                low = mid
            else:
                high = mid
        return high


@dataclass(frozen=True)
class WalkSample:
    segment: MoveSegment
    distance: float
    phase: float
    envelope: float  # 0 at the ends of the walk, 1 in the middle


@dataclass(frozen=True)
class YawEvent:
    t0: float
    t1: float
    start: float
    end: float


@dataclass(frozen=True)
class SitEvent:
    t0: float
    t1: float
    x_from: float
    x_to: float
    y_to: float


@dataclass(frozen=True)
class CharacterTrack:
    """Where one character stands, faces and sits during a scene."""

    cast: CastEntry
    rig: str
    x0: float
    y0: float
    yaw0: float
    sit0: float
    segments: tuple[MoveSegment, ...]
    yaw_events: tuple[YawEvent, ...]
    sit_events: tuple[SitEvent, ...]

    @property
    def character(self) -> str:
        return self.cast.character

    def position(self, t: float) -> Point:
        x, y = self.x0, self.y0
        for seg in self.segments:
            if t >= seg.t1:
                x, y = seg.x1, seg.y1
            elif t > seg.t0:
                p = seg.progress(t)
                return lerp(seg.x0, seg.x1, p), lerp(seg.y0, seg.y1, p)
            else:
                break
        for ev in self.sit_events:
            if t >= ev.t1:
                x, y = ev.x_to, ev.y_to
            elif t > ev.t0:
                p = smoothstep(ev.t0, ev.t1, t)
                return lerp(ev.x_from, ev.x_to, p), lerp(y, ev.y_to, p)
        return x, y

    def yaw(self, t: float) -> float:
        current = self.yaw0
        for ev in self.yaw_events:
            if t >= ev.t1:
                current = ev.end
            elif t > ev.t0:
                return lerp(ev.start, ev.end, smoothstep(ev.t0, ev.t1, t))
            else:
                break
        return current

    def sit(self, t: float) -> float:
        value = self.sit0
        for ev in self.sit_events:
            if t >= ev.t1:
                value = 1.0
            elif t > ev.t0:
                return smoothstep(ev.t0, ev.t1, t)
        return value

    def walk_sample(self, t: float) -> WalkSample | None:
        for seg in self.segments:
            if seg.t0 <= t <= seg.t1 + 1:
                distance = seg.distance * seg.progress(t)
                envelope = smoothstep(0, DEFAULT_WALK_RAMP_MS, t - seg.t0) * smoothstep(
                    0, DEFAULT_WALK_RAMP_MS, seg.t1 - t
                )
                return WalkSample(
                    seg, distance, phase_at_distance(distance, seg.stride), envelope
                )
        return None


@dataclass(frozen=True)
class HoldInterval:
    """A prop held in a hand from ``t0`` to ``t1`` (``None``: until the scene ends)."""

    prop_id: str
    character: str
    hand: str
    t0: float
    t1: float | None
    action_id: str
    release: Point | None = None
    release_angle: float = 0.0
    release_host: str | None = None


@dataclass(frozen=True)
class VisualEvent:
    prop_id: str
    field: str
    t0: float
    t1: float
    start: float
    end: float


@dataclass(frozen=True)
class PourWindow:
    action_id: str
    character: str
    t0: float
    t1: float
    ladle: str
    bowl: str


@dataclass(frozen=True)
class PropState:
    """One prop at one time; a held prop is placed by the renderer at the hand."""

    prop: PropRuntime
    x: float
    y: float
    angle: float
    holder: str | None
    hand: str | None
    location: str | None
    visible: bool
    broth: float = 0.0
    beef: float = 0.0
    herbs: float = 0.0
    scoop: float = 0.0
    hold_blend: float = 1.0


@dataclass(frozen=True)
class HoldSpec:
    """How a prop sits in the hands (pelvis-relative, mirrored by the facing)."""

    anchor: Vec  # where the prop's anchor goes
    grip: Vec  # the holding hand relative to the anchor
    angle: float  # prop rotation while carried (degrees, mirrored)
    support: Vec | None = None  # the other hand, relative to the anchor


HOLDS: Final[dict[str, HoldSpec]] = {
    "bowl_pho": HoldSpec((92.0, -98.0), (-60.0, -34.0), 0.0, (58.0, -34.0)),
    "ladle": HoldSpec((100.0, -100.0), (0.0, 0.0), -152.0, None),
    "hanging_ladle": HoldSpec((100.0, -100.0), (0.0, 0.0), -152.0, None),
    "spoon": HoldSpec((84.0, -150.0), (0.0, 0.0), -140.0, None),
    "tongs": HoldSpec((84.0, -150.0), (0.0, 0.0), -140.0, None),
}
DEFAULT_HOLD: Final[HoldSpec] = HOLDS["ladle"]


@dataclass(frozen=True)
class SceneRuntime:
    scene_id: str
    location: str
    tracks: Mapping[str, CharacterTrack]
    props: Mapping[str, PropRuntime]
    actions: Mapping[str, tuple[ResolvedAction, ...]]  # by character
    holds: tuple[HoldInterval, ...]
    visuals: tuple[VisualEvent, ...]
    pours: tuple[PourWindow, ...]
    start_ms: float
    end_ms: float


@dataclass(frozen=True)
class CharacterFrame:
    """Everything the renderer needs to draw one character at one time."""

    character: str
    rig: str
    x: float
    y: float
    scale: float
    pose: Pose
    planted: tuple[bool, bool]  # (left, right) foot on the ground
    speaking: bool
    sitting: float


# ---------------------------------------------------------------------------
# Action context
# ---------------------------------------------------------------------------


@dataclass
class ActionContext:
    """What an action sees: its progress, the body so far and the scene."""

    action: ResolvedAction
    t: float
    u: float
    yaw: float
    scale: float
    origin: Point
    pose: Pose
    scene: SceneRuntime
    choreo: Choreography
    character: str

    @property
    def dur_s(self) -> float:
        return (self.action.end_ms - self.action.start_ms) / 1000

    @property
    def clock_s(self) -> float:
        """Seconds since the action started."""
        return (self.t - self.action.start_ms) / 1000

    @property
    def sf(self) -> float:
        return facing_sign(self.yaw)

    @property
    def near(self) -> str:
        return near_side(self.yaw)

    def side(self, default: str | None = None) -> str:
        value = self.action.params.get("hand", default or self.near)
        return str(value)

    def other(self, side: str) -> str:
        return "left" if side == "right" else "right"

    def lat(self, side: str) -> float:
        return 1.0 if side == "left" else -1.0

    def rest(self, side: str) -> Vec:
        return rest_hand(side, self.yaw)

    def pelvis_world(self) -> Point:
        pose = self.pose
        return (
            self.origin[0] + pose.dx * self.scale,
            self.origin[1]
            + (RIG.hip_y + pose.dy + RIG.sit_drop * pose.sit) * self.scale,
        )

    def local(self, world: Point) -> Vec:
        px, py = self.pelvis_world()
        return ((world[0] - px) / self.scale, (world[1] - py) / self.scale)

    def world(self, local: Vec) -> Point:
        px, py = self.pelvis_world()
        return (px + local[0] * self.scale, py + local[1] * self.scale)

    def prop(self, name: str) -> PropRuntime:
        return self.scene.props[name]

    def param_prop(self, key: str = "prop") -> PropRuntime | None:
        name = self.action.params.get(key)
        return self.scene.props.get(str(name)) if name else None

    def prop_state(self, name: str, t: float | None = None) -> PropState:
        return self.choreo.prop_state(
            self.scene.scene_id, name, self.t if t is None else t
        )

    def prop_world(self, name: str) -> Point:
        """Where the prop is now; a held one at the analytic carry position."""
        state = self.prop_state(name)
        if state.holder == self.character:
            return self.carry_anchor_world(state.prop.kind)
        if state.holder is not None:
            other = self.choreo.character_world_hand(
                self.scene.scene_id, state.holder, state.hand or "right", self.t
            )
            return other
        return (state.x, state.y)

    def carry_anchor_world(self, kind: str) -> Point:
        spec = HOLDS.get(kind, DEFAULT_HOLD)
        return self.world((self.sf * spec.anchor[0], spec.anchor[1]))

    def carry_hand_local(self, kind: str, *, support: bool = False) -> Vec:
        spec = HOLDS.get(kind, DEFAULT_HOLD)
        offset = spec.support if support and spec.support else spec.grip
        return (self.sf * (spec.anchor[0] + offset[0]), spec.anchor[1] + offset[1])

    def target_direction(self, target: str) -> tuple[float, float]:
        """``(horizontal, vertical)`` look direction toward a named target."""
        named = {
            "street": (-0.8, 0.05),
            "door": (0.9, 0.0),
            "camera": (0.0, 0.0),
            "counter": (0.0, 0.85),
        }
        if target in named:
            return named[target]
        here = self.origin
        point: Point | None = None
        track = self.scene.tracks.get(target)
        if track is not None:
            point = self.choreo.character_position(self.scene.scene_id, target, self.t)
        elif target in self.scene.props:
            point = self.scene.props[target].base
        if point is None:
            return (0.0, 0.0)
        dx = point[0] - here[0]
        return (clamp(dx / 280.0, -1.0, 1.0), 0.0)


ActionFn = Callable[[ActionContext], Mapping[str, Any]]


# ---------------------------------------------------------------------------
# The 28 actions
# ---------------------------------------------------------------------------


def _idle(ctx: ActionContext) -> Mapping[str, Any]:
    return {}


def _walk(ctx: ActionContext) -> Mapping[str, Any]:
    """The legs and arm swing come from the locomotion layer; add a purposeful lean."""
    return {"lean": 3.0}


def _look_at(ctx: ActionContext) -> Mapping[str, Any]:
    target = str(ctx.action.params.get("target", "camera"))
    horizontal, vertical = ctx.target_direction(target)
    head = clamp(horizontal - 0.75 * ctx.yaw, -0.95, 0.95)
    return {
        "head_yaw": head,
        "gaze_x": horizontal * 0.7,
        "gaze_y": vertical * 0.6,
        "head_nod": vertical * 0.35,
    }


def _turn_body(ctx: ActionContext) -> Mapping[str, Any]:
    lead = math.sin(math.pi * clamp(ctx.u))
    return {"head_yaw": 0.25 * ctx.sf * lead, "gaze_x": 0.5 * ctx.sf * lead}


def _wave(ctx: ActionContext) -> Mapping[str, Any]:
    side = ctx.side()
    swing = 26 * math.sin(math.tau * 2.4 * ctx.clock_s)
    return {
        f"hand_{side}": (ctx.sf * 58 + swing, -300 + 6 * abs(swing) / 26),
        "smile": 0.9,
        "brow_l": 0.3,
        "brow_r": 0.3,
        "head_roll": 4 * ctx.sf,
    }


def _welcome(ctx: ActionContext) -> Mapping[str, Any]:
    side = ctx.side()
    other = ctx.other(side)
    open_u = _seg(ctx.u, 0.0, 0.35)
    return {
        f"hand_{side}": (
            ctx.sf * 150 * open_u + ctx.sf * 20,
            -80 + 12 * math.sin(ctx.u * 6),
        ),
        f"hand_{other}": (ctx.sf * 70, -30),
        "smile": 0.9,
        "brow_l": 0.3,
        "brow_r": 0.3,
        "head_roll": -4 * ctx.sf,
        "lean": 3.0,
    }


def _point(ctx: ActionContext) -> Mapping[str, Any]:
    side = ctx.side()
    return {
        f"hand_{side}": (ctx.sf * 210, -150),
        "head_yaw": 0.3 * ctx.sf,
        "smile": 0.7,
        "lean": 2.0,
    }


def _nod(ctx: ActionContext) -> Mapping[str, Any]:
    return {"head_nod": 0.85 * max(0.0, math.sin(math.tau * 1.5 * ctx.u)), "smile": 0.6}


def _shrug(ctx: ActionContext) -> Mapping[str, Any]:
    return {
        "shrug": 1.0,
        "hand_left": (112, -62),
        "hand_right": (-112, -62),
        "head_roll": 6 * ctx.sf,
        "brow_l": 0.5,
        "brow_r": 0.5,
        "smile": 0.05,
    }


def _sigh(ctx: ActionContext) -> Mapping[str, Any]:
    inhale = _seg(ctx.u, 0.0, 0.35)
    exhale = _seg(ctx.u, 0.35, 0.75)
    return {
        "shrug": 0.45 * inhale - 0.55 * exhale,
        "dy": -3 * inhale + 7 * exhale,
        "head_nod": 0.55 * exhale,
        "lean": 4 * exhale,
        "eye_open": 1.0 - 0.45 * exhale,
        "mouth_open": 0.35 * _seg(ctx.u, 0.4, 0.6) * (1 - _seg(ctx.u, 0.85, 1.0)),
        "mouth_wide": -1.0,
        "smile": -0.2,
        "brow_tilt": 0.7,
        "gaze_y": 0.4 * exhale,
    }


def _wipe_counter(ctx: ActionContext) -> Mapping[str, Any]:
    side = ctx.side()
    swipes = float(ctx.action.params.get("swipes", 3))
    counter = ctx.param_prop() or next(
        (p for p in ctx.scene.props.values() if p.kind == "counter"), None
    )
    surface_y = counter.top[1] if counter else ctx.origin[1] - 250
    cx = counter.x if counter else ctx.origin[0] + ctx.sf * 120
    centre = ctx.origin[0] + ctx.sf * 130
    if counter:
        centre = clamp(centre, cx - 150, cx + 150)
    sweep = 62 * math.sin(math.tau * swipes * ctx.u * 0.5)
    target = ctx.local((centre + sweep, surface_y - 4))
    return {
        f"hand_{side}": target,
        "lean": 7.0,
        "head_nod": 0.45,
        "gaze_y": 0.6,
        "smile": 0.2,
    }


def _hold_pose(ctx: ActionContext, prop: PropRuntime, side: str) -> Vec:
    return ctx.carry_hand_local(prop.kind)


ARM_REACH: Final[float] = 0.92 * (RIG.upper_arm + RIG.forearm)


def _bent_reach(ctx: ActionContext, side: str, goal: Vec) -> Vec:
    """``goal`` pulled in to 92% of the arm length from the shoulder, so the
    elbow stays visibly bent instead of locking straight."""
    angle = math.radians(clamp(ctx.yaw, -1.0, 1.0) * MAX_YAW_DEG)
    sign = 1.0 if side == "left" else -1.0
    shoulder = (sign * RIG.shoulder_w * math.cos(angle), -RIG.torso_h)
    dx, dy = goal[0] - shoulder[0], goal[1] - shoulder[1]
    distance = math.hypot(dx, dy)
    if distance <= ARM_REACH:
        return goal
    scale = ARM_REACH / distance
    return (shoulder[0] + dx * scale, shoulder[1] + dy * scale)


def _reach_take(ctx: ActionContext) -> Mapping[str, Any]:
    prop = ctx.param_prop()
    side = ctx.side("right")
    if prop is None:
        return {}
    grab = prop.grab_point()
    if ctx.choreo.prop_state(ctx.scene.scene_id, prop.id, ctx.action.start_ms).holder:
        grab = ctx.prop_world(prop.id)
    reach = ctx.local(grab)
    hold = _hold_pose(ctx, prop, side)
    take = _seg(ctx.u, 0.0, 0.5)
    back = _seg(ctx.u, 0.58, 1.0)
    # ease out with a slight overshoot, then settle on the prop; the elbow stays
    # bent all the way (the hand stops a few pixels short of a far prop)
    target = _bent_reach(
        ctx,
        side,
        _mix(_mix(ctx.rest(side), reach, ease_out_back(take)), hold, back),
    )
    return {
        f"hand_{side}": target,
        "lean": 6.0 * math.sin(math.pi * clamp(ctx.u)),
        "head_nod": 0.1,
        "gaze_x": ctx.sf * 0.5,
        "gaze_y": clamp(reach[1] / -300.0, -0.5, 0.3) * 0.0 - 0.25 * take,
        "head_yaw": 0.2 * ctx.sf * take,
    }


def _stroke_prop(ctx: ActionContext) -> Mapping[str, Any]:
    prop = ctx.param_prop()
    holder_side = "right"
    for hold in ctx.scene.holds:
        if prop and hold.prop_id == prop.id and hold.character == ctx.character:
            holder_side = hold.hand
    side = ctx.other(holder_side)
    anchor = ctx.carry_anchor_world(prop.kind if prop else "ladle")
    along = 0.5 + 0.5 * math.sin(math.tau * 1.2 * ctx.clock_s - 1.2)
    stroke = (anchor[0] + ctx.sf * 6, anchor[1] + lerp(-12, 92, along))
    return {
        f"hand_{side}": ctx.local(stroke),
        "head_nod": 0.45,
        "head_roll": 3 * ctx.sf,
        "eye_open": 0.72,
        "gaze_y": 0.55,
        "smile": 0.6,
        "blush": 0.3,
        "brow_tilt": 0.35,
    }


def _stir(ctx: ActionContext) -> Mapping[str, Any]:
    side = ctx.side()
    pot = ctx.param_prop() or next(
        (p for p in ctx.scene.props.values() if p.kind == "pot"), None
    )
    if pot is None:
        return {}
    angle = math.tau * 1.3 * ctx.clock_s
    centre = (pot.x + 14 * math.cos(angle), pot.y - POT_H - 6 + 7 * math.sin(angle))
    return {
        f"hand_{side}": ctx.local(centre),
        "tool": "ladle",
        "tool_hand": side,
        "tool_angle": -ctx.sf * (6 + 8 * math.cos(angle)),
        "lean": 5.0,
        "head_nod": 0.3,
        "gaze_y": 0.5,
    }


def _ladle_keys(ctx: ActionContext) -> tuple[Vec, Vec, Vec]:
    ladle = ctx.scene.props[str(ctx.action.params.get("from_prop", "ladle"))]
    bowl_name = str(ctx.action.params.get("to_prop", "bowl"))
    pot = next((p for p in ctx.scene.props.values() if p.kind == "pot"), None)
    grip = ladle.base
    rim = (pot.x, pot.y - POT_H) if pot else (grip[0], grip[1] - 40)
    bowl_kind = (
        ctx.scene.props[bowl_name].kind if bowl_name in ctx.scene.props else "bowl_pho"
    )
    bowl = ctx.carry_anchor_world(bowl_kind)
    return grip, rim, bowl


def ladle_angle_at(u: float, sf: float) -> float:
    """Rotation of the ladle during ladle_to_bowl (degrees, mirrored by ``sf``)."""
    return scalar_path(
        u,
        [
            (0.0, sf * LADLE_REST_ANGLE),
            (0.22, sf * LADLE_REST_ANGLE),
            (0.3, 0.0),
            (0.42, 0.0),
            (0.58, -sf * 35.0),
            (0.76, -sf * 108.0),
            (0.84, -sf * 108.0),
            (0.94, sf * LADLE_REST_ANGLE),
            (1.0, sf * LADLE_REST_ANGLE),
        ],
    )


def _ladle_to_bowl(ctx: ActionContext) -> Mapping[str, Any]:
    side = ctx.side()
    grip, rim, bowl = _ladle_keys(ctx)
    sf = ctx.sf
    keys = [
        (0.0, grip),
        (0.2, grip),
        (0.3, (rim[0] - sf * 4, rim[1] - 52)),
        (0.37, (rim[0] - sf * 4, rim[1] - 6)),
        (0.46, (rim[0] - sf * 4, rim[1] - 62)),
        (0.6, (bowl[0] - sf * 24, bowl[1] - 138)),
        (0.8, (bowl[0] - sf * 14, bowl[1] - 128)),
        (0.93, grip),
        (1.0, grip),
    ]
    return {
        f"hand_{side}": ctx.local(path(ctx.u, keys)),
        "lean": 5.0,
        "head_nod": 0.3,
        "gaze_y": 0.5,
        "gaze_x": sf * 0.4,
        "smile": 0.4,
    }


def _add_topping(ctx: ActionContext) -> Mapping[str, Any]:
    side = ctx.side()
    topping = str(ctx.action.params.get("topping", "herbs"))
    bowl_prop = ctx.param_prop()
    bowl_kind = bowl_prop.kind if bowl_prop else "bowl_pho"
    bowl = ctx.carry_anchor_world(bowl_kind)
    sf = ctx.sf
    pick = (ctx.origin[0] + sf * 92, ctx.origin[1] - 215)
    above = (bowl[0] - sf * 6, bowl[1] - 110)
    keys = [
        (0.0, ctx.world(ctx.rest(side))),
        (0.3, pick),
        (0.42, pick),
        (0.62, above),
        (0.75, above),
        (1.0, ctx.world(ctx.rest(side))),
    ]
    holding = ctx.u < 0.64
    return {
        f"hand_{side}": ctx.local(path(ctx.u, keys)),
        "tool": ("tongs_beef" if topping == "beef" else "herb_pinch")
        if holding
        else "",
        "tool_hand": side,
        "tool_angle": -sf * 18.0,
        "head_nod": 0.35,
        "gaze_y": 0.5,
        "lean": 3.0,
    }


def _carry_bowl(ctx: ActionContext) -> Mapping[str, Any]:
    prop = ctx.param_prop()
    kind = prop.kind if prop else "bowl_pho"
    holder = "left"
    for hold in ctx.scene.holds:
        if prop and hold.prop_id == prop.id and hold.character == ctx.character:
            holder = hold.hand
    return {
        f"hand_{holder}": ctx.carry_hand_local(kind),
        f"hand_{ctx.other(holder)}": ctx.carry_hand_local(kind, support=True),
        "lean": -2.0,
        "head_nod": 0.25,
        "gaze_y": 0.3,
        "smile": 0.5,
    }


def _place_keys(ctx: ActionContext) -> tuple[Point, Point, float]:
    prop = ctx.param_prop()
    host = ctx.param_prop("to_prop")
    kind = prop.kind if prop else "bowl_pho"
    start = ctx.carry_anchor_world(kind)
    if host is not None:
        end = (host.x + float(ctx.action.params.get("dx", 0.0)), host.top[1])
    else:
        end = (start[0] + ctx.sf * 60, start[1] + 60)
    release = float(ctx.action.params.get("release", 0.7))
    return start, end, release


def _place_bowl(ctx: ActionContext) -> Mapping[str, Any]:
    prop = ctx.param_prop()
    kind = prop.kind if prop else "bowl_pho"
    start, end, release = _place_keys(ctx)
    lift = 18 * math.sin(math.pi * _seg(ctx.u, 0.0, release))
    anchor = _mix(start, end, _seg(ctx.u, 0.05, release))
    anchor = (anchor[0], anchor[1] - lift)
    holder = "left"
    for hold in ctx.scene.holds:
        if prop and hold.prop_id == prop.id and hold.character == ctx.character:
            holder = hold.hand
    spec = HOLDS.get(kind, DEFAULT_HOLD)
    sf = ctx.sf
    retreat = _seg(ctx.u, release, 1.0)
    grip_main = ctx.local((anchor[0] + sf * spec.grip[0], anchor[1] + spec.grip[1]))
    support = spec.support or spec.grip
    grip_other = ctx.local((anchor[0] + sf * support[0], anchor[1] + support[1]))
    return {
        f"hand_{holder}": _mix(grip_main, ctx.rest(holder), retreat),
        f"hand_{ctx.other(holder)}": _mix(
            grip_other, ctx.rest(ctx.other(holder)), retreat
        ),
        "lean": 10 * math.sin(math.pi * _seg(ctx.u, 0.0, release)) * (1 - retreat),
        "head_nod": 0.3 * (1 - retreat),
        "gaze_y": 0.4,
        "smile": 0.6,
    }


def _sit(ctx: ActionContext) -> Mapping[str, Any]:
    near = ctx.near
    return {
        f"hand_{near}": (ctx.sf * 96, 6),
        f"hand_{ctx.other(near)}": (ctx.sf * 38, 10),
        "lean": -2.0,
    }


def _shiver(ctx: ActionContext) -> Mapping[str, Any]:
    """Hugging his own arms, shoulders hunched, with a small fast tremble."""
    t = ctx.clock_s
    tremble = math.sin(math.tau * 19 * t)
    flutter = math.sin(math.tau * 23 * t + 1.3)
    return {
        "dx": 1.5 * math.sin(math.tau * 15 * t),
        "dy": 1.1 * math.sin(math.tau * 21 * t + 1),
        # each hand grips the opposite upper arm, forearms flat across the chest
        "hand_left": (-54 + 1.6 * tremble, -122 + 1.2 * flutter),
        "hand_right": (48 - 1.6 * flutter, -104 + 1.2 * tremble),
        "shrug": 0.9,
        "squint": 0.35,
        "brow_tilt": 0.9,
        "brow_l": 0.2,
        "brow_r": 0.2,
        "lean": 9.0,
        "head_nod": 0.3,
        "head_roll": 3 * ctx.sf,
        "smile": -0.1,
        "mouth_open": 0.16 + 0.08 * math.sin(math.tau * 14 * t),
        "mouth_wide": 1.0,
    }


def _sniff(ctx: ActionContext) -> Mapping[str, Any]:
    pulse = max(0.0, math.sin(math.tau * 2.0 * ctx.u)) ** 2
    return {
        "head_nod": -0.3 - 0.12 * pulse,
        "eye_open": 0.12,
        "smile": 0.55,
        "brow_l": 0.55,
        "brow_r": 0.55,
        "lean": -3.0 - 2.0 * pulse,
        "dy": -2.0 * pulse,
        "blush": 0.2,
    }


SPOON_BOWL_UNITS: Final[float] = 54.0  # draw_spoon: bowl centre below the grip
# Angles are toward the eater's back (``sf *``). The soup sits just under the
# chin in a close-up, so a handle pointing back would lie across the face: at
# the dip the bowl is down and back (the handle up and away from the face), and
# it turns up through "bowl to the chest, handle forward" to the lips.
SPOON_DIP_DEG: Final[float] = 30.0
SPOON_MOUTH_DEG: Final[float] = 154.0  # bowl up and back, at the lips


def _spoon_offset(angle_deg: float, length: float) -> Vec:
    """Grip-to-bowl vector of a spoon turned like ``blit_prop`` turns it."""
    a = math.radians(angle_deg)
    return (-length * math.sin(a), length * math.cos(a))


def _taste(ctx: ActionContext) -> Mapping[str, Any]:
    side = ctx.side()
    bowl = ctx.param_prop()
    base = bowl.base if bowl else (ctx.origin[0] + ctx.sf * 120, ctx.origin[1] - 230)
    sf = ctx.sf
    # the spoon is drawn from the grip (the hand) and its bowl is one spoon
    # length away, so the hand stops that far short of the soup and of the lips
    length = SPOON_BOWL_UNITS * PROP_SCALE / ctx.scale
    dip_angle, mouth_angle = sf * SPOON_DIP_DEG, sf * SPOON_MOUTH_DEG
    soup = ctx.local((base[0], base[1] - BOWL_H * 0.7))
    dip_off = _spoon_offset(dip_angle, length)
    dip = (soup[0] - dip_off[0], soup[1] - dip_off[1])
    lips = (sf * (18 + 18 * abs(ctx.yaw)), RIG.head_center_y + 58)
    mouth_off = _spoon_offset(mouth_angle, length)
    mouth = (lips[0] - mouth_off[0], lips[1] - mouth_off[1])
    keys = [
        (0.0, ctx.rest(side)),
        (0.26, dip),
        (0.34, dip),
        (0.58, mouth),
        (0.82, mouth),
        (1.0, ctx.rest(side)),
    ]
    eyes = 1.0 - _seg(ctx.u, 0.52, 0.64) * (1 - _seg(ctx.u, 0.86, 0.98))
    return {
        f"hand_{side}": path(ctx.u, keys),
        "tool": "spoon",
        "tool_hand": side,
        "tool_angle": lerp(dip_angle, mouth_angle, _seg(ctx.u, 0.3, 0.58)),
        "head_nod": 0.5 * (1 - _seg(ctx.u, 0.3, 0.55)) + 0.12,
        "lean": 8 * (1 - _seg(ctx.u, 0.3, 0.55)),
        "eye_open": max(0.04, eyes),
        "smile": 0.35 + 0.65 * _seg(ctx.u, 0.55, 0.7),
        "mouth_open": 0.45 * _seg(ctx.u, 0.5, 0.58) * (1 - _seg(ctx.u, 0.62, 0.7)),
        "mouth_wide": 0.4,
        "blush": 0.4 * _seg(ctx.u, 0.55, 0.9),
        "gaze_y": 0.5,
    }


def _smile_big(ctx: ActionContext) -> Mapping[str, Any]:
    return {
        "smile": 1.0,
        "squint": 0.55,
        "brow_l": 0.35,
        "brow_r": 0.35,
        "blush": 0.35,
        "mouth_open": 0.2,
        "mouth_wide": 0.6,
        "head_roll": 2 * ctx.sf,
    }


def _wipe_eye(ctx: ActionContext) -> Mapping[str, Any]:
    side = ctx.side()
    jitter = 7 * math.sin(math.tau * 3.0 * ctx.clock_s)
    # the knuckle dabs the cheek just under the eye (happy tears), eyes visible
    cheek = (
        ctx.sf * 44 + jitter,
        RIG.head_center_y + 46 + 3 * math.cos(math.tau * 3.0 * ctx.clock_s),
    )
    return {
        f"hand_{side}": cheek,
        "elbow_down": 1.0,
        "eye_open": 0.55,
        "smile": 0.55,
        "brow_tilt": 0.8,
        "head_roll": 5 * ctx.sf,
        "head_nod": 0.15,
        "blush": 0.3,
    }


def _thumbs_up(ctx: ActionContext) -> Mapping[str, Any]:
    side = ctx.side()
    bob = 5 * math.sin(math.tau * 1.6 * ctx.clock_s)
    return {
        f"hand_{side}": (ctx.sf * 92, -178 + bob),
        f"thumb_{side}": 1.0,
        "smile": 1.0,
        "squint": 0.3,
        "head_nod": 0.2 * max(0.0, math.sin(math.tau * 1.4 * ctx.u)),
        "brow_l": 0.2,
        "brow_r": 0.2,
    }


def _cover_mouth(ctx: ActionContext) -> Mapping[str, Any]:
    side = ctx.side()
    shake = math.sin(math.tau * 6.0 * ctx.clock_s)
    return {
        # the hand rests at the mouth corner, the elbow down: the eyes stay clear
        f"hand_{side}": (
            ctx.sf * (26 + 6 * ctx.yaw),
            RIG.head_center_y + 62 + 2 * shake,
        ),
        "elbow_down": 1.0,
        "smile": 1.0,
        "squint": 0.45,
        "blush": 0.6,
        "dy": 1.6 * shake,
        "head_roll": 5 * ctx.sf,
        "brow_l": 0.3,
        "brow_r": 0.3,
    }


def _blush(ctx: ActionContext) -> Mapping[str, Any]:
    return {
        "blush": 1.0,
        "gaze_y": 0.5,
        "smile": 0.6,
        "head_roll": 4 * ctx.sf,
        "brow_tilt": 0.5,
        "eye_open": 0.85,
        "head_nod": 0.2,
    }


def _idle_talk(ctx: ActionContext) -> Mapping[str, Any]:
    side = ctx.side()
    t = ctx.clock_s
    return {
        f"hand_{side}": (
            ctx.sf * (88 + 26 * math.sin(math.tau * 1.3 * t)),
            -72 + 18 * math.sin(math.tau * 0.9 * t + 1),
        ),
        "head_nod": 0.12 + 0.1 * math.sin(math.tau * 1.1 * t),
        "brow_l": 0.2,
        "brow_r": 0.2,
        "smile": 0.55,
    }


ACTION_REGISTRY: Final[dict[str, ActionFn]] = {
    "idle": _idle,
    "walk": _walk,
    "look_at": _look_at,
    "turn_body": _turn_body,
    "wave": _wave,
    "welcome": _welcome,
    "point": _point,
    "nod": _nod,
    "shrug": _shrug,
    "sigh": _sigh,
    "wipe_counter": _wipe_counter,
    "reach_take": _reach_take,
    "stroke_prop": _stroke_prop,
    "stir": _stir,
    "ladle_to_bowl": _ladle_to_bowl,
    "add_topping": _add_topping,
    "carry_bowl": _carry_bowl,
    "place_bowl": _place_bowl,
    "sit": _sit,
    "shiver": _shiver,
    "sniff": _sniff,
    "taste": _taste,
    "smile_big": _smile_big,
    "wipe_eye": _wipe_eye,
    "thumbs_up": _thumbs_up,
    "cover_mouth": _cover_mouth,
    "blush": _blush,
    "idle_talk": _idle_talk,
}
MOVING_KINDS: Final[frozenset[str]] = frozenset({"walk", "carry_bowl"})

if set(ACTION_REGISTRY) != set(ACTION_KINDS):  # pragma: no cover - import-time guard
    raise RuntimeError("ACTION_REGISTRY and storyboard.ACTION_KINDS differ")


# ---------------------------------------------------------------------------
# Idle layer
# ---------------------------------------------------------------------------


def _schedule(seed: str, low: float, high: float, horizon: float) -> tuple[float, ...]:
    rng = random.Random(seed)
    times: list[float] = []
    t = rng.uniform(low, high)
    while t < horizon:
        times.append(t)
        t += rng.uniform(low, high)
    return tuple(times)


def _blink_amount(times: Sequence[float], t: float) -> float:
    """0 open ... 1 closed: close in 55 ms, hold 40, open in 90."""
    index = bisect.bisect_right(times, t) - 1
    if index < 0:
        return 0.0
    s = t - times[index]
    if s < 55:
        return s / 55
    if s < 95:
        return 1.0
    if s < 185:
        return 1.0 - (s - 95) / 90
    return 0.0


# ---------------------------------------------------------------------------
# Choreography
# ---------------------------------------------------------------------------


def _target_x(params: Mapping[str, Any], current: float) -> float:
    to = params.get("to")
    if isinstance(to, Mapping) and "x" in to:
        return float(to["x"]) * STAGE_W
    return current


def _target_y(params: Mapping[str, Any], current: float) -> float:
    to = params.get("to")
    if isinstance(to, Mapping) and "y" in to:
        return float(to["y"]) * STAGE_H
    return current


class Choreography:
    """The whole film's motion as pure functions of time.

    ``voice_characters`` maps a voice id of the storyboard to the character
    whose mouth moves for it (a narrator maps to nobody); ``envelopes`` give the
    loudness of lines by id.
    """

    def __init__(
        self,
        timeline: ResolvedTimeline,
        *,
        voice_characters: Mapping[str, str] | None = None,
        envelopes: Mapping[str, Envelope] | None = None,
    ) -> None:
        self.timeline = timeline
        self.voice_characters = dict(VOICE_CHARACTERS | dict(voice_characters or {}))
        self.envelopes = dict(envelopes or {})
        horizon = timeline.total_ms + 2000.0
        self._blinks: dict[str, tuple[float, ...]] = {}
        self._glances: dict[str, tuple[float, ...]] = {}
        self._horizon = horizon
        self.scenes: dict[str, SceneRuntime] = {}
        for resolved in timeline.scenes:
            self.scenes[resolved.id] = self._build_scene(resolved.id)

    # --- construction ----------------------------------------

    def _scene_props(self, scene_id: str) -> dict[str, PropRuntime]:
        scene = next(s for s in self.timeline.scenes if s.id == scene_id).scene
        placements = list(scene.props)
        surfaces = [p for p in placements if p.kind in SURFACE_TOP]

        def resolve(p: PropPlacement) -> PropRuntime:
            x, y = p.at[0] * STAGE_W, p.at[1] * STAGE_H
            host = None
            angle = 0.0
            if p.kind in RESTING_KINDS:
                for surface in surfaces:
                    if surface.location not in (None, p.location) and p.location:
                        continue
                    sx = surface.at[0] * STAGE_W
                    if abs(x - sx) <= SURFACE_HALF_W[surface.kind]:
                        y = surface.at[1] * STAGE_H - SURFACE_TOP[surface.kind]
                        host = surface.id
                        break
            if p.kind == "ladle":
                pot = next(
                    (
                        q
                        for q in placements
                        if q.kind == "pot" and abs(q.at[0] * STAGE_W - x) < 150
                    ),
                    None,
                )
                if pot is not None:
                    pot_x, pot_y = self._resolved_pot(pot, surfaces)
                    x, y = pot_x + 40.0, pot_y - POT_H + 6.0
                    angle = LADLE_REST_ANGLE
            return PropRuntime(p.id, p.kind, x, y, p.state, p.location, angle, host)

        return {p.id: resolve(p) for p in placements}

    @staticmethod
    def _resolved_pot(pot: PropPlacement, surfaces: list[PropPlacement]) -> Point:
        x, y = pot.at[0] * STAGE_W, pot.at[1] * STAGE_H
        for surface in surfaces:
            if abs(x - surface.at[0] * STAGE_W) <= SURFACE_HALF_W[surface.kind]:
                return x, surface.at[1] * STAGE_H - SURFACE_TOP[surface.kind]
        return x, y

    def _build_scene(self, scene_id: str) -> SceneRuntime:
        resolved = next(s for s in self.timeline.scenes if s.id == scene_id)
        scene = resolved.scene
        props = self._scene_props(scene_id)
        actions_by_char: dict[str, list[ResolvedAction]] = {}
        for action in self.timeline.actions_of(scene_id):
            actions_by_char.setdefault(action.character, []).append(action)
        tracks: dict[str, CharacterTrack] = {}
        for cast in scene.cast:
            tracks[cast.character] = self._build_track(
                cast,
                props,
                sorted(
                    actions_by_char.get(cast.character, []), key=lambda a: a.start_ms
                ),
            )
        holds, visuals, pours = self._build_props(
            scene_id, props, actions_by_char, tracks
        )
        return SceneRuntime(
            scene_id=scene_id,
            location=scene.location,
            tracks=tracks,
            props=props,
            actions={
                key: tuple(sorted(value, key=lambda a: a.start_ms))
                for key, value in actions_by_char.items()
            },
            holds=tuple(holds),
            visuals=tuple(visuals),
            pours=tuple(pours),
            start_ms=float(resolved.render_start_ms),
            end_ms=float(resolved.render_end_ms),
        )

    def _build_track(
        self,
        cast: CastEntry,
        props: Mapping[str, PropRuntime],
        actions: Sequence[ResolvedAction],
    ) -> CharacterTrack:
        x, y = cast.x * STAGE_W, cast.y_ground * STAGE_H
        yaw0 = FACING_YAW.get(cast.facing, 0.0)
        sit0 = 1.0 if cast.state == "sitting" else 0.0
        if sit0:
            stool = self._nearest_stool(props, x)
            if stool is not None:
                x, y = stool.x, stool.y
        segments: list[MoveSegment] = []
        yaw_events: list[YawEvent] = []
        sit_events: list[SitEvent] = []
        cur_x, cur_y, cur_yaw = x, y, yaw0
        for action in actions:
            t0, t1 = float(action.start_ms), float(action.end_ms)
            if action.kind in MOVING_KINDS:
                tx = _target_x(action.params, cur_x)
                ty = _target_y(action.params, cur_y)
                distance = math.hypot(tx - cur_x, ty - cur_y)
                if distance < 4:
                    continue
                speed = distance / max(0.001, (t1 - t0) / 1000)
                stride, steps = plan_walk(distance, stride_for_speed(speed))
                a = float(action.ease_in_ms) or min(
                    DEFAULT_WALK_RAMP_MS, (t1 - t0) * 0.3
                )
                b = float(action.ease_out_ms) or min(
                    DEFAULT_WALK_RAMP_MS, (t1 - t0) * 0.3
                )
                seg = MoveSegment(
                    action.id,
                    action.kind,
                    t0,
                    t1,
                    a,
                    b,
                    cur_x,
                    tx,
                    cur_y,
                    ty,
                    stride,
                    steps,
                )
                segments.append(seg)
                direction = seg.direction
                if abs(tx - cur_x) > 8:
                    target_yaw = direction * WALK_YAW
                    yaw_events.append(YawEvent(t0, t0 + 260, cur_yaw, target_yaw))
                    relaxed = direction * IDLE_YAW
                    yaw_events.append(YawEvent(t1, t1 + 480, target_yaw, relaxed))
                    cur_yaw = relaxed
                cur_x, cur_y = tx, ty
            elif action.kind == "turn_body":
                target_yaw = self._turn_target(action, cur_x, cur_yaw)
                end = min(t1, t0 + TURN_MS)
                yaw_events.append(YawEvent(t0, max(end, t0 + 120), cur_yaw, target_yaw))
                cur_yaw = target_yaw
            elif action.kind == "sit":
                stool = props.get(
                    str(action.params.get("prop", ""))
                ) or self._nearest_stool(props, cur_x)
                tx, ty = (stool.x, stool.y) if stool else (cur_x, cur_y)
                sit_events.append(SitEvent(t0, t1, cur_x, tx, ty))
                cur_x, cur_y = tx, ty
        return CharacterTrack(
            cast=cast,
            rig=cast.character,
            x0=x,
            y0=y,
            yaw0=yaw0,
            sit0=sit0,
            segments=tuple(segments),
            yaw_events=tuple(yaw_events),
            sit_events=tuple(sit_events),
        )

    @staticmethod
    def _nearest_stool(
        props: Mapping[str, PropRuntime], x: float
    ) -> PropRuntime | None:
        stools = [p for p in props.values() if p.kind == "stool"]
        return min(stools, key=lambda p: abs(p.x - x), default=None)

    @staticmethod
    def _turn_target(action: ResolvedAction, x: float, yaw: float) -> float:
        value = action.params.get("to", action.params.get("target", "front"))
        if isinstance(value, str) and value in FACING_YAW:
            return FACING_YAW[value]
        return yaw

    def _build_props(
        self,
        scene_id: str,
        props: Mapping[str, PropRuntime],
        actions_by_char: Mapping[str, list[ResolvedAction]],
        tracks: Mapping[str, CharacterTrack] | None = None,
    ) -> tuple[list[HoldInterval], list[VisualEvent], list[PourWindow]]:
        holds: list[HoldInterval] = []
        visuals: list[VisualEvent] = []
        pours: list[PourWindow] = []
        every = sorted(
            (a for items in actions_by_char.values() for a in items),
            key=lambda a: a.start_ms,
        )
        # initial visual state of the bowls
        for prop in props.values():
            if prop.kind == "bowl_pho" and prop.state == "full":
                for name in ("broth", "beef", "herbs"):
                    visuals.append(VisualEvent(prop.id, name, -1e9, -1e9, 1.0, 1.0))
        for action in every:
            t0, t1 = float(action.start_ms), float(action.end_ms)
            dur = t1 - t0
            who = action.character
            name = action.params.get("prop")
            if action.kind == "reach_take" and name in props:
                hand = str(action.params.get("hand", "right"))
                holds.append(
                    HoldInterval(str(name), who, hand, t0 + 0.55 * dur, None, action.id)
                )
            elif action.kind == "ladle_to_bowl":
                ladle = str(action.params.get("from_prop", "ladle"))
                bowl = str(action.params.get("to_prop", "bowl"))
                if ladle in props:
                    # the same hand _ladle_to_bowl moves: the near hand at the start
                    track = (tracks or {}).get(who)
                    yaw = track.yaw(t0) if track is not None else IDLE_YAW
                    hand = str(action.params.get("hand", near_side(yaw)))
                    rest = props[ladle]
                    holds.append(
                        HoldInterval(
                            ladle,
                            who,
                            hand,
                            t0 + 0.2 * dur,
                            t0 + 0.95 * dur,
                            action.id,
                            release=rest.base,
                            release_angle=rest.angle,
                        )
                    )
                    visuals.append(
                        VisualEvent(
                            ladle, "scoop", t0 + 0.3 * dur, t0 + 0.42 * dur, 0.0, 1.0
                        )
                    )
                    visuals.append(
                        VisualEvent(
                            ladle, "scoop", t0 + 0.64 * dur, t0 + 0.84 * dur, 1.0, 0.0
                        )
                    )
                    if bowl in props:
                        visuals.append(
                            VisualEvent(
                                bowl,
                                "broth",
                                t0 + 0.66 * dur,
                                t0 + 0.86 * dur,
                                0.0,
                                1.0,
                            )
                        )
                        pours.append(
                            PourWindow(
                                action.id,
                                who,
                                t0 + 0.62 * dur,
                                t0 + 0.84 * dur,
                                ladle,
                                bowl,
                            )
                        )
            elif action.kind == "add_topping":
                topping = str(action.params.get("topping", "herbs"))
                if name in props and topping in ("beef", "herbs"):
                    visuals.append(
                        VisualEvent(
                            str(name),
                            topping,
                            t0 + 0.62 * dur,
                            t0 + 0.72 * dur,
                            0.0,
                            1.0,
                        )
                    )
            elif action.kind == "place_bowl" and name in props:
                release = float(action.params.get("release", 0.7))
                host = props.get(str(action.params.get("to_prop", "")))
                spot = (
                    (host.x + float(action.params.get("dx", 0.0)), host.top[1])
                    if host
                    else None
                )
                for i, hold in enumerate(holds):
                    if hold.prop_id == name and hold.t1 is None:
                        holds[i] = replace(
                            hold,
                            t1=t0 + release * dur,
                            release=spot,
                            release_host=host.id if host else None,
                        )
        return holds, visuals, pours

    # --- queries ----------------------------------------

    def scene(self, scene_id: str) -> SceneRuntime:
        return self.scenes[scene_id]

    def character_position(self, scene_id: str, character: str, t: float) -> Point:
        return self.scenes[scene_id].tracks[character].position(t)

    def visible_characters(self, scene_id: str, location: str) -> list[str]:
        scene = self.timeline.storyboard
        cast = next(s for s in scene.scenes if s.id == scene_id).cast
        return [
            entry.character
            for entry in cast
            if not entry.visible_in or location in entry.visible_in
        ]

    def prop_state(self, scene_id: str, prop_id: str, t: float) -> PropState:
        runtime = self.scenes[scene_id]
        prop = runtime.props[prop_id]
        current: HoldInterval | None = None
        for hold in runtime.holds:
            if hold.prop_id == prop_id and hold.t0 <= t:
                current = hold
        x, y, angle = prop.x, prop.y, prop.angle
        holder = hand = None
        location = prop.location
        blend = 1.0
        if current is not None:
            if current.t1 is None or t < current.t1:
                holder, hand = current.character, current.hand
                blend = smoothstep(current.t0, current.t0 + HOLD_BLEND_MS, t)
            elif current.release is not None:
                x, y = current.release
                angle = current.release_angle
                if current.release_host:
                    location = runtime.props[current.release_host].location
        values = {"broth": 0.0, "beef": 0.0, "herbs": 0.0, "scoop": 0.0}
        for event in runtime.visuals:
            if event.prop_id != prop_id or t < event.t0:
                continue
            span = max(1.0, event.t1 - event.t0)
            values[event.field] = lerp(
                event.start, event.end, smoothstep(0, 1, (t - event.t0) / span)
            )
        return PropState(
            prop=prop,
            x=x,
            y=y,
            angle=angle,
            holder=holder,
            hand=hand,
            location=location,
            visible=True,
            broth=values["broth"],
            beef=values["beef"],
            herbs=values["herbs"],
            scoop=values["scoop"],
            hold_blend=blend,
        )

    def character_world_hand(
        self, scene_id: str, character: str, hand: str, t: float
    ) -> Point:
        frame = self.character_frame(scene_id, character, t)
        from ai_youtube_agent.animation.rig import solve_skeleton

        skeleton = solve_skeleton(frame.pose)
        local = skeleton.hand(hand)
        return (frame.x + local[0] * frame.scale, frame.y + local[1] * frame.scale)

    def held_angle(
        self, scene_id: str, prop_id: str, t: float, character: str
    ) -> float:
        """Rotation of a prop in a hand (degrees)."""
        runtime = self.scenes[scene_id]
        sf = facing_sign(runtime.tracks[character].yaw(t))
        for hold in runtime.holds:
            if (
                hold.prop_id == prop_id
                and hold.t0 <= t
                and (hold.t1 is None or t < hold.t1)
            ):
                action = next(
                    (
                        a
                        for a in runtime.actions.get(character, ())
                        if a.id == hold.action_id
                    ),
                    None,
                )
                if action is not None and action.kind == "ladle_to_bowl":
                    u = clamp(
                        (t - action.start_ms) / max(1, action.end_ms - action.start_ms)
                    )
                    return ladle_angle_at(u, sf)
                kind = runtime.props[prop_id].kind
                return sf * HOLDS.get(kind, DEFAULT_HOLD).angle
        return runtime.props[prop_id].angle

    # --- the pose ----------------------------------------

    def _idle_pose(
        self,
        scene: SceneRuntime,
        track: CharacterTrack,
        t: float,
        yaw: float,
        sit: float,
    ) -> Pose:
        style = get_style(track.rig)
        seed = track.character
        if seed not in self._blinks:
            self._blinks[seed] = _schedule(f"{seed}-blink", 2300, 4700, self._horizon)
            self._glances[seed] = _schedule(f"{seed}-glance", 1100, 2600, self._horizon)
        s = t / 1000
        phase = (sum(map(ord, seed)) % 17) * 0.37
        closed = _blink_amount(self._blinks[seed], t)
        glance = bisect.bisect_right(self._glances[seed], t)
        grng = random.Random(f"{seed}-g{glance}")
        gaze_x = (grng.random() - 0.5) * 0.5
        gaze_y = (grng.random() - 0.5) * 0.25
        return Pose(
            dx=1.2 * math.sin(math.tau * 0.11 * s + phase),
            dy=2.0 * math.sin(math.tau * 0.27 * s + phase) * (1 - 0.5 * sit),
            lean=style.stoop,
            yaw=yaw,
            sit=sit,
            hand_l=rest_hand("left", yaw),
            hand_r=rest_hand("right", yaw),
            head_yaw=0.04 * math.sin(math.tau * 0.17 * s + phase),
            head_roll=0.9 * math.sin(math.tau * 0.23 * s + phase * 2),
            eye_open=1.0 - closed,
            gaze_x=gaze_x + 0.25 * facing_sign(yaw) * abs(yaw),
            gaze_y=gaze_y,
            smile=style.default_smile,
            brow_tilt=style.default_brow_tilt,
        )

    def _locomotion(
        self, pose: Pose, track: CharacterTrack, walk: WalkSample | None
    ) -> Pose:
        if walk is None:
            return pose
        seg = walk.segment
        frame = walk_cycle(walk.phase, seg.stride)
        direction = seg.direction
        env = walk.envelope
        lift_ramp = min(1.0, env * 2.5)
        carrying = seg.kind == "carry_bowl"
        swing_gain = 0.0 if carrying else 30.0 * env
        # stride and travel are stage pixels, the pose is in local units
        local = 1.0 / get_style(track.rig).scale
        left = (direction * frame.foot_a[0] * local, -frame.foot_a[1] * lift_ramp)
        right = (direction * frame.foot_b[0] * local, -frame.foot_b[1] * lift_ramp)
        rest_l, rest_r = pose.hand_l, pose.hand_r
        return replace(
            pose,
            foot_l=left,
            foot_r=right,
            dy=pose.dy * (1 - env) + frame.bob * env,
            lean=pose.lean + 3.0 * env,
            sway=2.2 * math.sin(math.tau * walk.phase) * env,
            hand_l=(
                rest_l[0] + direction * frame.swing_a * swing_gain,
                rest_l[1] - abs(frame.swing_a) * 5 * env,
            ),
            hand_r=(
                rest_r[0] + direction * frame.swing_b * swing_gain,
                rest_r[1] - abs(frame.swing_b) * 5 * env,
            ),
        )

    def _hold_layers(
        self, scene: SceneRuntime, character: str, t: float, ctx_base: ActionContext
    ) -> list[tuple[dict[str, Any], float]]:
        layers: list[tuple[dict[str, Any], float]] = []
        for hold in scene.holds:
            if hold.character != character or t < hold.t0:
                continue
            if hold.t1 is not None and t >= hold.t1 + HOLD_BLEND_MS:
                continue
            kind = scene.props[hold.prop_id].kind
            target = ctx_base.carry_hand_local(kind)
            weight = smoothstep(hold.t0, hold.t0 + HOLD_BLEND_MS, t)
            if hold.t1 is not None and t >= hold.t1:
                weight *= 1 - smoothstep(hold.t1, hold.t1 + HOLD_BLEND_MS, t)
            channels: dict[str, Any] = {f"hand_{hold.hand}": target}
            spec = HOLDS.get(kind, DEFAULT_HOLD)
            if spec.support is not None:
                other = "left" if hold.hand == "right" else "right"
                channels[f"hand_{other}"] = ctx_base.carry_hand_local(
                    kind, support=True
                )
            layers.append((channels, weight))
        return layers

    def _body_pose(
        self, scene: SceneRuntime, character: str, t: float
    ) -> tuple[Pose, Point, float, WalkSample | None, CharacterTrack]:
        track = scene.tracks[character]
        x, y = track.position(t)
        yaw = track.yaw(t)
        sit = track.sit(t)
        style = get_style(track.rig)
        pose = self._idle_pose(scene, track, t, yaw, sit)
        walk = track.walk_sample(t)
        pose = self._locomotion(pose, track, walk)
        if sit > 0:
            sf = facing_sign(yaw)
            near_hand, far_hand = (sf * 96.0, 6.0), (sf * 38.0, 10.0)
            if near_side(yaw) == "left":
                targets = {"hand_l": near_hand, "hand_r": far_hand}
            else:
                targets = {"hand_l": far_hand, "hand_r": near_hand}
            pose = replace(
                pose,
                hand_l=_mix(pose.hand_l, targets["hand_l"], sit),
                hand_r=_mix(pose.hand_r, targets["hand_r"], sit),
                lean=pose.lean - 3 * sit,
            )
        return pose, (x, y), style.scale, walk, track

    def character_frame(
        self, scene_id: str, character: str, t: float
    ) -> CharacterFrame:
        scene = self.scenes[scene_id]
        pose, (x, y), scale, walk, track = self._body_pose(scene, character, t)
        yaw = pose.yaw
        base_ctx = ActionContext(
            action=ResolvedAction(
                "", scene_id, character, "idle", int(t), int(t) + 1, 0, 0, {}
            ),
            t=t,
            u=0.0,
            yaw=yaw,
            scale=scale,
            origin=(x, y),
            pose=pose,
            scene=scene,
            choreo=self,
            character=character,
        )
        for channels, weight in self._hold_layers(scene, character, t, base_ctx):
            pose = pose.blended(channels, weight)
        for action in scene.actions.get(character, ()):
            weight = ramp_weight(action, t)
            if weight <= 0.0:
                continue
            ctx = ActionContext(
                action=action,
                t=t,
                u=clamp(
                    (t - action.start_ms) / max(1, action.end_ms - action.start_ms)
                ),
                yaw=yaw,
                scale=scale,
                origin=(x, y),
                pose=base_ctx.pose,
                scene=scene,
                choreo=self,
                character=character,
            )
            pose = pose.blended(ACTION_REGISTRY[action.kind](ctx), weight)
        speaking = False
        for line in self.timeline.lines:
            if (
                line.scene_id != scene_id
                or self.voice_characters.get(line.voice) != character
            ):
                continue
            mouth = speech_mouth(line, t, self.envelopes.get(line.line_id))
            if mouth is not None and mouth[0] > 0.0:
                speaking = True
                pose = pose.blended(
                    {
                        "mouth_open": mouth[0] * 0.8,
                        "mouth_wide": mouth[1],
                        "brow_l": max(pose.brow_l, 0.18 * mouth[0]),
                        "brow_r": max(pose.brow_r, 0.18 * mouth[0]),
                        "head_nod": pose.head_nod + 0.12 * mouth[0],
                    },
                    1.0,
                )
        pose = self._secondary(pose, track, t)
        planted = (True, True)
        if walk is not None:
            wf = walk_cycle(walk.phase, walk.segment.stride)
            planted = (wf.planted_a, wf.planted_b)
        return CharacterFrame(
            character, track.rig, x, y, scale, pose, planted, speaking, pose.sit
        )

    def _secondary(self, pose: Pose, track: CharacterTrack, t: float) -> Pose:
        """Hair and cloth lag a little behind the body (follow-through)."""
        dt = 90.0
        x_now, _ = track.position(t)
        x_then, _ = track.position(t - dt)
        velocity = (x_now - x_then) / (dt / 1000)
        s = t / 1000
        flutter = math.sin(math.tau * 1.1 * s)
        return replace(
            pose,
            hair_x=clamp(-velocity * 0.018, -12.0, 12.0)
            + 1.2 * flutter
            - 5.0 * pose.head_yaw,
            hair_y=clamp(pose.dy * -0.6, -6.0, 6.0)
            + 0.8 * math.sin(math.tau * 1.7 * s),
            cloth_x=clamp(-velocity * 0.012, -9.0, 9.0) + 0.8 * flutter,
            cloth_y=clamp(pose.dy * -0.4, -4.0, 4.0),
        )


# ---------------------------------------------------------------------------
# Footsteps
# ---------------------------------------------------------------------------


def footstep_events(
    timeline: ResolvedTimeline, choreography: Choreography | None = None
) -> list[SfxEvent]:
    """One ``footstep`` SFX at every foot landing of every walk and carry."""
    choreo = choreography or Choreography(timeline)
    events: list[SfxEvent] = []
    for scene in choreo.scenes.values():
        for track in scene.tracks.values():
            for seg in track.segments:
                for index, distance in enumerate(
                    foot_contacts(seg.distance, seg.stride)
                ):
                    t = seg.time_at_distance(distance)
                    events.append(
                        SfxEvent("footstep", round(t), f"{seg.action_id}:step{index}")
                    )
    return sorted(events, key=lambda e: (e.t_ms, e.source))


__all__ = [
    "ACTION_REGISTRY",
    "ActionContext",
    "CharacterFrame",
    "Choreography",
    "Envelope",
    "LIFT_PX",
    "DEFAULT_STRIDE",
    "footstep_events",
    "load_envelope",
    "speech_mouth",
]
