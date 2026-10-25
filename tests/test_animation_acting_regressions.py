"""Regressions found by auditing a real ANIM-01 run (Edge TTS voice, MP4 frames).

Each test pins a defect that was seen in the decoded MP4 of a full pipeline run
(``artifacts/animation_film/20261023-233039``), not only in the code:

* ``taste``: the handle of the spoon, not its bowl, went into the customer's
  mouth (the bowl stuck out in front of his face);
* ``ladle_to_bowl`` without an explicit hand: the ladle was always drawn in the
  right hand while a left-facing cook scooped with the empty left hand;
* the hook's ``look_at`` played while the camera was still on the extreme
  close-up of the pot, so Mai acted off-screen.
"""

from __future__ import annotations

import json
import math

import pytest

from ai_youtube_agent.animation.actions import PROP_SCALE, Choreography
from ai_youtube_agent.animation.rig import get_style, near_side, solve_skeleton
from ai_youtube_agent.animation.storyboard import load_storyboard
from ai_youtube_agent.animation.timeline import resolve_timeline
from animation_fixtures import fake_spoken, scene_of, shipped_dict, shipped_storyboard

# draw_spoon: the grip is the origin, the handle ends 70 units above it and the
# bowl is centred 54 units below it (prop units, PROP_SCALE stage px each).
SPOON_BOWL = 54.0
SPOON_HANDLE_END = -70.0


@pytest.fixture(scope="module")
def timeline():
    storyboard = shipped_storyboard()
    return resolve_timeline(storyboard, fake_spoken(storyboard))


@pytest.fixture(scope="module")
def choreo(timeline):
    return Choreography(timeline)


def _rotate_like_blit_prop(vec, angle_deg):
    """``blit_prop`` turns a prop with ``Image.rotate(-angle)``: clockwise on screen."""
    a = math.radians(angle_deg)
    return (
        vec[0] * math.cos(a) - vec[1] * math.sin(a),
        vec[0] * math.sin(a) + vec[1] * math.cos(a),
    )


def _spoon_points(frame, skel):
    """Where the spoon's bowl and handle end are (character local units)."""
    pose = frame.pose
    hand = skel.hand(pose.tool_hand)
    units = PROP_SCALE / get_style(frame.rig).scale  # local units per prop unit
    bowl = _rotate_like_blit_prop((0.0, SPOON_BOWL * units), pose.tool_angle)
    end = _rotate_like_blit_prop((0.0, SPOON_HANDLE_END * units), pose.tool_angle)
    return (
        (hand[0] + bowl[0], hand[1] + bowl[1]),
        (hand[0] + end[0], hand[1] + end[1]),
    )


def test_tasting_puts_the_bowl_of_the_spoon_in_the_mouth(timeline, choreo) -> None:
    action = next(a for a in timeline.actions if a.id == "a5_taste")
    for fraction in (0.64, 0.7, 0.76):
        t = action.start_ms + fraction * (action.end_ms - action.start_ms)
        frame = choreo.character_frame(action.scene_id, action.character, t)
        skel = solve_skeleton(frame.pose)
        assert frame.pose.tool == "spoon"
        bowl, handle_end = _spoon_points(frame, skel)
        mouth = skel.mouth()
        assert math.dist(bowl, mouth) < 32, (fraction, bowl, mouth)
        # the handle points away from the face, it is not what goes in
        assert math.dist(handle_end, mouth) > math.dist(bowl, mouth) + 60


def test_tasting_dips_the_bowl_of_the_spoon_into_the_soup(timeline, choreo) -> None:
    from ai_youtube_agent.animation.actions import BOWL_H

    action = next(a for a in timeline.actions if a.id == "a5_taste")
    t = action.start_ms + 0.3 * (action.end_ms - action.start_ms)
    frame = choreo.character_frame(action.scene_id, action.character, t)
    skel = solve_skeleton(frame.pose)
    spoon_bowl, _ = _spoon_points(frame, skel)
    soup = choreo.scene(action.scene_id).props["bowl"]
    # the soup surface in the skeleton's space (feet at the origin, local units)
    surface = (
        (soup.x - frame.x) / frame.scale,
        (soup.y - BOWL_H * 0.7 - frame.y) / frame.scale,
    )
    assert math.dist(spoon_bowl, surface) < 30, (spoon_bowl, surface)


def test_the_spoon_handle_never_sweeps_across_the_face(timeline, choreo) -> None:
    """Seen in run 20261023-235413 at 23.02-23.23 s: turning the bowl up through
    "forward" swung the long handle back across the customer's nose."""
    from ai_youtube_agent.animation.rig import RIG

    action = next(a for a in timeline.actions if a.id == "a5_taste")
    crossings = []
    for step in range(51):
        fraction = step / 50
        t = action.start_ms + fraction * (action.end_ms - action.start_ms)
        frame = choreo.character_frame(action.scene_id, action.character, t)
        if frame.pose.tool != "spoon":
            continue
        skel = solve_skeleton(frame.pose)
        hand = skel.hand(frame.pose.tool_hand)
        _, end = _spoon_points(frame, skel)
        head = skel.head
        # the outer 60 % of the handle must stay off the face (the inner oval
        # of the head: eyes, nose, glasses)
        for along in (0.4, 0.7, 1.0):
            point = (
                hand[0] + along * (end[0] - hand[0]),
                hand[1] + along * (end[1] - hand[1]),
            )
            inside = ((point[0] - head[0]) / (RIG.head_w * 0.4)) ** 2 + (
                (point[1] - head[1]) / (RIG.head_h * 0.4)
            ) ** 2
            if inside < 1.0:
                crossings.append((round(fraction, 2), along, round(inside, 2)))
    assert not crossings


def test_no_arm_swings_across_another_characters_face(timeline, choreo) -> None:
    """Seen in run 20261024-001410 at 15.25 s: standing at x 0.44, Mai's
    thumbs-up arm swung over the seated customer's glasses in the MEDIUM shot."""
    from ai_youtube_agent.animation.rig import RIG

    def arm(skel, side):
        joints = [getattr(skel, f"{j}_{side}") for j in ("shoulder", "elbow", "hand")]
        for a, b in zip(joints, joints[1:], strict=False):
            for i in range(7):
                k = i / 6
                yield (a[0] + k * (b[0] - a[0]), a[1] + k * (b[1] - a[1]))

    overlaps = []
    for scene in timeline.scenes:
        cast = [c.character for c in scene.scene.cast]
        for t in range(scene.start_ms, scene.end_ms, 33):
            frames = {w: choreo.character_frame(scene.id, w, t) for w in cast}
            skels = {w: solve_skeleton(f.pose) for w, f in frames.items()}
            for who, f in frames.items():
                for other, g in frames.items():
                    if other == who:
                        continue
                    head = skels[other].head
                    cx, cy = g.x + head[0] * g.scale, g.y + head[1] * g.scale
                    rx, ry = RIG.head_w * 0.5 * g.scale, RIG.head_h * 0.5 * g.scale
                    for side in ("l", "r"):
                        for p in arm(skels[who], side):
                            x, y = f.x + p[0] * f.scale, f.y + p[1] * f.scale
                            if ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 < 1.0:
                                overlaps.append((scene.id, t, who, side, other))
                                break
    assert not overlaps


def _storyboard_from(data, tmp_path):
    path = tmp_path / "storyboard.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return load_storyboard(path)


@pytest.mark.parametrize("facing", ["right", "left"])
def test_the_ladle_is_held_in_the_hand_that_scoops(tmp_path, facing) -> None:
    data = shipped_dict()
    scene_of(data, "s4_cooking")["cast"][0]["facing"] = facing
    storyboard = _storyboard_from(data, tmp_path)
    timeline = resolve_timeline(storyboard, fake_spoken(storyboard))
    choreo = Choreography(timeline)
    action = next(a for a in timeline.actions if a.id == "a4_ladle")
    assert "hand" not in action.params  # the default hand is what is tested
    runtime = choreo.scene("s4_cooking")
    hold = next(h for h in runtime.holds if h.prop_id == "ladle")

    t = action.start_ms + 0.55 * (action.end_ms - action.start_ms)
    moving = near_side(runtime.tracks["mai"].yaw(t))
    assert hold.hand == moving
    state = choreo.prop_state("s4_cooking", "ladle", t)
    assert state.holder == "mai" and state.hand == moving


def test_every_acting_beat_of_the_film_is_on_screen(timeline, fonts_or_skip) -> None:
    """At the middle of every action the character's head is inside the view."""
    from PIL import Image

    from ai_youtube_agent.animation.camera import visible_world_rect
    from ai_youtube_agent.animation.renderer import FrameRenderer, RenderAssets
    from ai_youtube_agent.animation.rig import RIG

    plate = Image.new("RGB", (1080, 1920), (150, 176, 170))
    renderer = FrameRenderer(
        timeline,
        RenderAssets({"front": plate, "kitchen": plate}, fonts_or_skip),
        size=(270, 480),
    )
    off_screen = []
    for action in timeline.actions:
        if action.kind in ("walk", "idle"):
            continue  # entering and leaving the frame is the point of a walk
        t = (action.start_ms + action.end_ms) / 2
        if timeline.transition_at(t) is not None:
            continue
        shot = renderer._shot_of(action.scene_id, t)
        if action.character not in renderer.choreo.visible_characters(
            action.scene_id, shot.location
        ):
            continue
        frame = renderer.choreo.character_frame(action.scene_id, action.character, t)
        skel = solve_skeleton(frame.pose)
        head = (
            frame.x + skel.head[0] * frame.scale,
            frame.y + skel.head[1] * frame.scale,
        )
        x0, y0, x1, y1 = visible_world_rect(renderer._camera(shot).state_at(t))
        margin = RIG.head_h * 0.25 * frame.scale  # most of the face must show
        if not (x0 + margin <= head[0] <= x1 - margin and y0 <= head[1] <= y1):
            off_screen.append(
                (action.id, shot.id, round(head[0]), (round(x0), round(x1)))
            )
    assert not off_screen


@pytest.fixture(scope="module")
def fonts_or_skip():
    from ai_youtube_agent.animation.fonts import resolve_font_files
    from ai_youtube_agent.animation.pipeline_steps import StepBlocked

    try:
        return resolve_font_files(None)
    except StepBlocked:
        pytest.skip("no Vietnamese-capable TrueType font on this machine")
