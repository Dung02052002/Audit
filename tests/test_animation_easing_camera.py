"""Easing functions and the virtual camera (pure, deterministic)."""

from __future__ import annotations

import math

import pytest

from ai_youtube_agent.animation.camera import (
    FRAMING_ZOOM,
    STAGE_H,
    STAGE_W,
    CameraState,
    background_blur_px,
    build_shot_camera,
    clamp_center,
    framing_zoom,
    handheld_offset,
    headroom_center,
    parallax_zoom,
    screen_to_world,
    stage_point,
    visible_world_rect,
    world_to_screen,
)
from ai_youtube_agent.animation.easing import (
    EASINGS,
    Track,
    clamp,
    critically_damped,
    ease_in_out_cubic,
    ease_out_back,
    get_easing,
    lerp,
    smoothstep,
)
from ai_youtube_agent.animation.errors import AnimationError
from ai_youtube_agent.animation.storyboard import FocusSpec, Framing
from ai_youtube_agent.animation.timeline import ResolvedMove, ResolvedShot

# --- easing --------------------------------------------------------------------


def test_clamp_lerp_and_smoothstep() -> None:
    assert clamp(-1) == 0 and clamp(2) == 1 and clamp(0.4) == 0.4
    assert clamp(5, 1, 3) == 3
    assert lerp(10, 20, 0.25) == 12.5
    assert smoothstep(0, 1, -1) == 0
    assert smoothstep(0, 1, 2) == 1
    assert smoothstep(0, 1, 0.5) == pytest.approx(0.5)
    assert smoothstep(2, 2, 1) == 0 and smoothstep(2, 2, 3) == 1


@pytest.mark.parametrize("name", sorted(EASINGS))
def test_every_easing_starts_at_zero_and_ends_at_one(name: str) -> None:
    easing = get_easing(name)

    assert easing(0.0) == pytest.approx(0.0, abs=1e-9)
    assert easing(1.0) == pytest.approx(1.0, abs=1e-9)


def test_cubic_easing_is_symmetric_and_monotonic() -> None:
    values = [ease_in_out_cubic(i / 20) for i in range(21)]

    assert values == sorted(values)
    assert ease_in_out_cubic(0.5) == pytest.approx(0.5)
    assert ease_in_out_cubic(0.25) + ease_in_out_cubic(0.75) == pytest.approx(1.0)


def test_ease_out_back_overshoots_then_settles() -> None:
    peak = max(ease_out_back(i / 100) for i in range(101))

    assert peak > 1.0
    assert ease_out_back(1.0) == pytest.approx(1.0)


def test_get_easing_default_and_unknown() -> None:
    assert get_easing(None) is ease_in_out_cubic
    with pytest.raises(ValueError, match="unknown easing"):
        get_easing("wobble")


def test_critically_damped_converges_without_overshoot() -> None:
    position, velocity = 0.0, 0.0
    trace = []
    for _ in range(120):
        position, velocity = critically_damped(position, velocity, 100.0, 0.3, 1 / 30)
        trace.append(position)

    assert max(trace) <= 100.0 + 1e-6
    assert trace == sorted(trace)
    assert trace[-1] == pytest.approx(100.0, abs=0.01)


def test_critically_damped_is_stable_for_a_huge_step_and_zero_smoothing() -> None:
    position, _ = critically_damped(0.0, 0.0, 10.0, 0.3, 100.0)

    assert position == pytest.approx(10.0, abs=1e-3)
    assert critically_damped(3.0, 5.0, 9.0, 0.0, 1 / 30) == (9.0, 0.0)


def test_track_interpolates_and_holds_the_ends() -> None:
    track = Track.from_pairs([(0.0, 10.0), (1.0, 20.0), (3.0, 0.0)], lambda t: t)

    assert track.value_at(-5) == 10.0
    assert track.value_at(0.5) == pytest.approx(15.0)
    assert track.value_at(2.0) == pytest.approx(10.0)
    assert track.value_at(9.0) == 0.0


def test_track_needs_increasing_keys() -> None:
    with pytest.raises(ValueError, match="at least one"):
        Track(())
    with pytest.raises(ValueError, match="strictly increasing"):
        Track(((0.0, 1.0), (0.0, 2.0)))


# --- framing and projection ----------------------------------------------------


def test_framing_zooms() -> None:
    assert FRAMING_ZOOM == {
        Framing.WIDE: 1.3,
        Framing.MEDIUM: 1.7,
        Framing.CLOSE_UP: 2.7,
        Framing.EXTREME_CLOSE: 3.6,
    }
    assert framing_zoom("CLOSE_UP") == 2.7


def test_stage_point_and_center_clamp() -> None:
    assert stage_point(0.5, 0.25) == (540.0, 480.0)
    assert clamp_center(0, 0, 1.0) == (STAGE_W / 2, STAGE_H / 2)
    assert clamp_center(0, 0, 2.0) == (STAGE_W / 4, STAGE_H / 4)
    assert clamp_center(5000, 5000, 2.0) == (STAGE_W * 0.75, STAGE_H * 0.75)


def test_visible_rect_shrinks_with_zoom_and_includes_shake() -> None:
    wide = visible_world_rect(CameraState(540, 960, 1.0))
    close = visible_world_rect(CameraState(540, 960, 2.0, shake=(10.0, -4.0)))

    assert wide == (0, 0, STAGE_W, STAGE_H)
    assert close == (280.0, 476.0, 820.0, 1436.0)


def test_world_screen_round_trip() -> None:
    state = CameraState(600, 800, 2.7, shake=(3.0, 2.0))
    point = (640.0, 760.0)

    on_screen = world_to_screen(state, point)

    assert screen_to_world(state, on_screen) == pytest.approx(point)
    assert world_to_screen(CameraState(540, 960, 1.0), (540, 960)) == (540.0, 960.0)
    assert world_to_screen(state, point, (540, 960))[0] == pytest.approx(
        on_screen[0] / 2
    )


def test_headroom_puts_the_head_at_forty_percent_of_the_frame() -> None:
    head = (540.0, 800.0)
    zoom = 2.7
    cx, cy = headroom_center(head, zoom)
    state = CameraState(cx, cy, zoom)

    _, screen_y = world_to_screen(state, head)

    assert screen_y / STAGE_H == pytest.approx(0.40)
    assert world_to_screen(state, head)[0] == pytest.approx(STAGE_W / 2)


def test_headroom_is_clamped_inside_the_stage() -> None:
    cx, cy = headroom_center((20.0, 30.0), 3.6)

    assert cx == STAGE_W / (2 * 3.6)
    assert cy == STAGE_H / (2 * 3.6)


def test_parallax_and_depth_of_field() -> None:
    assert parallax_zoom(1.0) == 1.0
    assert parallax_zoom(3.6) == pytest.approx(1 + 2.6 * 0.45)
    assert background_blur_px(1.0) == 0.0
    assert background_blur_px(3.6) > background_blur_px(1.7) > 0.0


# --- handheld ------------------------------------------------------------------


def test_handheld_is_deterministic_small_and_seeded() -> None:
    a = [handheld_offset(i / 30, seed=1) for i in range(90)]
    b = [handheld_offset(i / 30, seed=1) for i in range(90)]
    other = [handheld_offset(i / 30, seed=2) for i in range(90)]

    assert a == b
    assert a != other
    assert all(abs(x) <= 3.0 and abs(y) <= 3.0 for x, y in a)
    assert max(abs(x) for x, _ in a) > 0.5
    steps = [abs(a[i + 1][0] - a[i][0]) for i in range(89)]
    assert max(steps) < 1.0  # smooth, not jittery


# --- shot cameras --------------------------------------------------------------


def make_shot(
    framing: Framing = Framing.MEDIUM,
    moves: tuple[ResolvedMove, ...] = (),
    start: int = 1000,
    end: int = 4000,
) -> ResolvedShot:
    return ResolvedShot(
        id="shot",
        scene_id="scene",
        start_ms=start,
        end_ms=end,
        framing=framing,
        focus=FocusSpec(character="mai"),
        moves=moves,
        location="front",
    )


def move(kind: str, t0: int, t1: int, to=None, from_=None, ease=None) -> ResolvedMove:
    return ResolvedMove(kind, t0, t1, to or {}, from_, ease)


def still(_t: float) -> tuple[float, float]:
    return 540.0, 800.0


def test_a_held_shot_keeps_its_framing_zoom_and_headroom() -> None:
    camera = build_shot_camera(make_shot(Framing.CLOSE_UP), still, 30)

    states = {camera.state_at(1000 + i * 100) for i in range(30)}

    assert len(states) == 1
    state = next(iter(states))
    assert state.zoom == 2.7
    assert (state.cx, state.cy) == headroom_center((540.0, 800.0), 2.7)
    assert len(camera.states) == math.ceil(3000 * 30 / 1000) + 1


def test_push_in_goes_from_the_start_zoom_to_the_target_with_easing() -> None:
    shot = make_shot(
        Framing.CLOSE_UP,
        (move("push_in", 1000, 3000, {"zoom": 2.6}, {"zoom": 2.2}, "linear"),),
    )
    camera = build_shot_camera(shot, still, 30)

    assert camera.state_at(1000).zoom == pytest.approx(2.2)
    assert camera.state_at(2000).zoom == pytest.approx(2.4, abs=0.01)
    assert camera.state_at(3000).zoom == pytest.approx(2.6)
    assert camera.state_at(3900).zoom == pytest.approx(2.6)


def test_pull_back_to_a_framing_name_and_chained_moves() -> None:
    shot = make_shot(
        Framing.EXTREME_CLOSE,
        (
            move("push_in", 1000, 1900, {"zoom": 3.6}, {"zoom": 3.4}),
            move("pull_back", 1900, 3800, {"framing": "WIDE"}),
        ),
        end=4800,
    )
    camera = build_shot_camera(shot, still, 30)

    assert camera.state_at(1000).zoom == pytest.approx(3.4)
    assert camera.state_at(1900).zoom == pytest.approx(3.6, abs=0.01)
    assert camera.state_at(3800).zoom == pytest.approx(FRAMING_ZOOM[Framing.WIDE])
    assert camera.state_at(4500).cx == 540.0
    zooms = [camera.state_at(1900 + i * 63).zoom for i in range(31)]
    assert zooms == sorted(zooms, reverse=True)


def test_pan_to_moves_the_aim_point() -> None:
    shot = make_shot(
        Framing.CLOSE_UP,
        (
            move(
                "pan_to",
                1000,
                3000,
                {"x": 0.7, "y": 0.5},
                {"x": 0.3, "y": 0.5},
                "linear",
            ),
        ),
    )
    camera = build_shot_camera(shot, still, 30)

    start, middle, end = (camera.state_at(t) for t in (1000, 2000, 3000))

    assert start.cx < middle.cx < end.cx
    assert middle.cx == pytest.approx(540.0, abs=2)
    assert end.cx == pytest.approx(0.7 * STAGE_W)


def test_follow_chases_a_moving_focus_with_a_lag() -> None:
    def walking(t_ms: float) -> tuple[float, float]:
        return 300.0 + (t_ms - 1000) * 0.2, 800.0

    shot = make_shot(Framing.MEDIUM, (move("follow", 1000, 3000),))
    camera = build_shot_camera(shot, walking, 30)

    early, late = camera.state_at(1300), camera.state_at(2900)
    target_late = headroom_center(walking(2900), 1.7)[0]

    assert early.cx < late.cx
    assert late.cx < target_late  # it trails behind the walker
    assert target_late - late.cx < 120
    xs = [camera.state_at(1000 + i * 33.4).cx for i in range(60)]
    assert xs == sorted(xs)


def test_follow_is_precomputed_and_deterministic() -> None:
    def walking(t_ms: float) -> tuple[float, float]:
        return 300.0 + t_ms * 0.1, 800.0

    shot = make_shot(Framing.MEDIUM, (move("follow", 1000, 3000),))

    assert build_shot_camera(shot, walking, 30) == build_shot_camera(shot, walking, 30)


def test_handheld_shakes_only_inside_its_window_and_with_the_seed() -> None:
    shot = make_shot(
        Framing.MEDIUM, (move("handheld", 1500, 3500, {"amplitude": 4.0}),)
    )
    camera = build_shot_camera(shot, still, 30, seed=3)
    same = build_shot_camera(shot, still, 30, seed=3)
    other = build_shot_camera(shot, still, 30, seed=4)

    assert camera.state_at(1200).shake == (0.0, 0.0)
    assert camera.state_at(3800).shake == (0.0, 0.0)
    assert camera.state_at(2500).shake != (0.0, 0.0)
    assert camera == same
    assert camera != other
    assert all(abs(s.shake[0]) <= 4.0 for s in camera.states)


def test_state_at_clamps_outside_the_shot() -> None:
    camera = build_shot_camera(make_shot(), still, 30)

    assert camera.state_at(-5000) is camera.states[0]
    assert camera.state_at(99999) is camera.states[-1]


def test_camera_needs_a_positive_fps() -> None:
    with pytest.raises(AnimationError, match="fps"):
        build_shot_camera(make_shot(), still, 0)
