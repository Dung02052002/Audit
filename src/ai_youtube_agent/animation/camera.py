"""Virtual camera: framing, moves, follow spring, handheld shake (pure).

The camera looks at the 1080x1920 stage. ``CameraState.cx, cy`` is the stage
point at the centre of the view, ``zoom`` how many times the stage is magnified
(1 shows all of it) and ``shake`` an extra offset in stage pixels. A shot's
camera is precomputed frame by frame (``build_shot_camera``), so rendering a
frame is a pure lookup and the result is the same on every run.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from ai_youtube_agent.animation.easing import (
    clamp,
    critically_damped,
    get_easing,
    lerp,
)
from ai_youtube_agent.animation.errors import AnimationError
from ai_youtube_agent.animation.storyboard import Framing
from ai_youtube_agent.animation.timeline import ResolvedMove, ResolvedShot

STAGE_W: Final[int] = 1080
STAGE_H: Final[int] = 1920
FRAMING_ZOOM: Final[dict[Framing, float]] = {
    Framing.WIDE: 1.3,
    Framing.MEDIUM: 1.7,
    Framing.CLOSE_UP: 2.7,
    Framing.EXTREME_CLOSE: 3.6,
}
# WIDE is 1.3x: both characters fill the frame (a 1.0 stage leaves the top 45%
# empty facade). The head sits at 40% of the frame height, so faces stay above
# the captions.
HEAD_FRAME_Y: Final[float] = 0.40
FOLLOW_SMOOTH_S: Final[float] = 0.30
PARALLAX_FACTOR: Final[float] = 0.45
HANDHELD_AMPLITUDE_PX: Final[float] = 3.0
Point = tuple[float, float]


@dataclass(frozen=True)
class CameraState:
    cx: float
    cy: float
    zoom: float
    shake: tuple[float, float] = (0.0, 0.0)


def framing_zoom(framing: Framing | str) -> float:
    return FRAMING_ZOOM[Framing(framing)]


def stage_point(nx: float, ny: float) -> Point:
    """Normalised stage coordinates (0..1) to stage pixels."""
    return nx * STAGE_W, ny * STAGE_H


def visible_world_size(zoom: float) -> tuple[float, float]:
    return STAGE_W / zoom, STAGE_H / zoom


def clamp_center(cx: float, cy: float, zoom: float) -> Point:
    """Keep the view inside the stage (at zoom 1 the centre is fixed)."""
    half_w, half_h = STAGE_W / (2 * zoom), STAGE_H / (2 * zoom)
    return (
        clamp(cx, half_w, STAGE_W - half_w),
        clamp(cy, half_h, STAGE_H - half_h),
    )


def headroom_center(
    focus: Point, zoom: float, head_frame_y: float = HEAD_FRAME_Y
) -> Point:
    """Camera centre that puts ``focus`` at ``head_frame_y`` of the frame height.

    The horizontal position is centred on the focus. The result is clamped so
    the view never leaves the stage.
    """
    _, visible_h = visible_world_size(zoom)
    return clamp_center(focus[0], focus[1] + (0.5 - head_frame_y) * visible_h, zoom)


def visible_world_rect(state: CameraState) -> tuple[float, float, float, float]:
    """``(x0, y0, x1, y1)`` of the stage area in view, shake included."""
    width, height = visible_world_size(state.zoom)
    cx, cy = state.cx + state.shake[0], state.cy + state.shake[1]
    return cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2


def world_to_screen(
    state: CameraState, point: Point, screen_size: tuple[int, int] = (STAGE_W, STAGE_H)
) -> Point:
    """Where a stage point lands on a ``screen_size`` frame."""
    x0, y0, x1, y1 = visible_world_rect(state)
    return (
        (point[0] - x0) / (x1 - x0) * screen_size[0],
        (point[1] - y0) / (y1 - y0) * screen_size[1],
    )


def screen_to_world(
    state: CameraState, point: Point, screen_size: tuple[int, int] = (STAGE_W, STAGE_H)
) -> Point:
    x0, y0, x1, y1 = visible_world_rect(state)
    return (
        x0 + point[0] / screen_size[0] * (x1 - x0),
        y0 + point[1] / screen_size[1] * (y1 - y0),
    )


def parallax_zoom(zoom: float) -> float:
    """Zoom of the background plate: it moves less than the foreground."""
    return 1.0 + (zoom - 1.0) * PARALLAX_FACTOR


def background_blur_px(zoom: float) -> float:
    """Depth-of-field blur (stage pixels) of the background; none when wide."""
    return 0.0 if zoom <= 1.2 else round(2.2 * (zoom - 1.2), 3)


def handheld_offset(
    t_s: float, seed: int, amplitude_px: float = HANDHELD_AMPLITUDE_PX
) -> Point:
    """Smooth low-frequency shake: a few sines with seeded frequencies/phases."""
    rng = random.Random(seed)
    x = y = 0.0
    for weight in (1.0, 0.55, 0.3):
        fx, fy = 0.35 + rng.random() * 0.9, 0.35 + rng.random() * 0.9
        px, py = rng.random() * math.tau, rng.random() * math.tau
        x += weight * math.sin(math.tau * fx * t_s + px)
        y += weight * math.sin(math.tau * fy * t_s + py)
    scale = amplitude_px / 1.85
    return x * scale, y * scale


@dataclass(frozen=True)
class ShotCamera:
    """The precomputed camera of one shot, one state per frame."""

    start_ms: int
    fps: int
    states: tuple[CameraState, ...]

    def state_at(self, t_ms: float) -> CameraState:
        index = round((t_ms - self.start_ms) * self.fps / 1000)
        return self.states[int(clamp(index, 0, len(self.states) - 1))]


FocusAt = Callable[[float], Point]


def _zoom_value(values: Mapping[str, Any] | None, fallback: float) -> float:
    if not values:
        return fallback
    if "zoom" in values:
        return float(values["zoom"])
    if "framing" in values:
        return framing_zoom(str(values["framing"]))
    return fallback


def _zoom_at(moves: Sequence[ResolvedMove], t_ms: float, base: float) -> float:
    previous = base
    for move in moves:
        if move.kind not in ("push_in", "pull_back"):
            continue
        target = _zoom_value(move.to, previous)
        origin = _zoom_value(move.from_, previous)
        if t_ms >= move.t1_ms:
            previous = target
            continue
        if t_ms < move.t0_ms:
            return origin
        span = max(1, move.t1_ms - move.t0_ms)
        progress = (t_ms - move.t0_ms) / span
        return lerp(origin, target, get_easing(move.ease)(progress))
    return previous


def _desired_point(
    moves: Sequence[ResolvedMove], t_ms: float, focus_at: FocusAt, start_ms: int
) -> Point:
    """The stage point the camera aims at."""
    anchor = focus_at(start_ms)
    for move in moves:
        if move.kind == "pan_to":
            target = stage_point(float(move.to["x"]), float(move.to["y"]))
            origin = (
                stage_point(float(move.from_["x"]), float(move.from_["y"]))
                if move.from_ and "x" in move.from_ and "y" in move.from_
                else anchor
            )
            if t_ms >= move.t1_ms:
                anchor = target
            elif t_ms >= move.t0_ms:
                span = max(1, move.t1_ms - move.t0_ms)
                e = get_easing(move.ease)((t_ms - move.t0_ms) / span)
                return lerp(origin[0], target[0], e), lerp(origin[1], target[1], e)
            else:
                return anchor
        elif move.kind == "follow":
            if move.t0_ms <= t_ms <= move.t1_ms:
                return focus_at(t_ms)
            if t_ms > move.t1_ms:
                anchor = focus_at(move.t1_ms)
    return anchor


def build_shot_camera(
    shot: ResolvedShot, focus_at: FocusAt, fps: int, *, seed: int = 0
) -> ShotCamera:
    """Precompute the camera of ``shot`` for every frame.

    ``focus_at(t_ms)`` gives the stage point to frame (for a character, the head)
    at an absolute time; ``follow`` moves chase it with a critically damped
    spring, other moves ease between values. The zoom starts at the framing's
    zoom; ``push_in``/``pull_back`` change it, ``pan_to`` moves the aim point,
    ``handheld`` adds seeded shake. Deterministic for the same inputs.
    """
    if fps <= 0:
        raise AnimationError("fps must be positive")
    base = framing_zoom(shot.framing)
    frames = max(1, math.ceil((shot.end_ms - shot.start_ms) * fps / 1000) + 1)
    has_follow = any(move.kind == "follow" for move in shot.moves)
    dt = 1 / fps
    states: list[CameraState] = []
    position: Point | None = None
    velocity = (0.0, 0.0)
    for frame in range(frames):
        t_ms = shot.start_ms + frame * 1000 / fps
        zoom = _zoom_at(shot.moves, t_ms, base)
        aim = _desired_point(shot.moves, t_ms, focus_at, shot.start_ms)
        target = headroom_center(aim, zoom)
        if position is None:
            position = target
        elif has_follow:
            nx, vx = critically_damped(
                position[0], velocity[0], target[0], FOLLOW_SMOOTH_S, dt
            )
            ny, vy = critically_damped(
                position[1], velocity[1], target[1], FOLLOW_SMOOTH_S, dt
            )
            position, velocity = (nx, ny), (vx, vy)
        else:
            position = target
        cx, cy = clamp_center(position[0], position[1], zoom)
        states.append(CameraState(cx, cy, zoom, _shake_at(shot.moves, t_ms, seed)))
    return ShotCamera(shot.start_ms, fps, tuple(states))


def _shake_at(moves: Sequence[ResolvedMove], t_ms: float, seed: int) -> Point:
    for move in moves:
        if move.kind == "handheld" and move.t0_ms <= t_ms <= move.t1_ms:
            amplitude = float(move.to.get("amplitude", HANDHELD_AMPLITUDE_PX))
            ramp = min(1.0, (t_ms - move.t0_ms) / 300, (move.t1_ms - t_ms) / 300)
            return handheld_offset(t_ms / 1000, seed, amplitude * clamp(ramp))
    return 0.0, 0.0
