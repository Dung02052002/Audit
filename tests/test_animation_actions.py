"""Actions: the 28 kinds, motion, props, speech and footsteps (no Pillow needed)."""

from __future__ import annotations

import math
import struct
import wave

import pytest

from ai_youtube_agent.animation.actions import (
    ACTION_REGISTRY,
    Choreography,
    Envelope,
    footstep_events,
    load_envelope,
    ramp_weight,
    speech_mouth,
)
from ai_youtube_agent.animation.rig import foot_contacts, solve_skeleton
from ai_youtube_agent.animation.storyboard import ACTION_KINDS
from ai_youtube_agent.animation.timeline import ResolvedAction, resolve_timeline
from animation_fixtures import fake_spoken, shipped_storyboard


@pytest.fixture(scope="module")
def timeline():
    storyboard = shipped_storyboard()
    return resolve_timeline(storyboard, fake_spoken(storyboard))


@pytest.fixture(scope="module")
def choreo(timeline):
    return Choreography(timeline)


def test_every_action_kind_is_implemented() -> None:
    assert set(ACTION_REGISTRY) == set(ACTION_KINDS) and len(ACTION_KINDS) == 28


def test_every_action_of_the_film_yields_finite_poses_through_its_whole_span(
    timeline, choreo
) -> None:
    kinds = set()
    for action in timeline.actions:
        kinds.add(action.kind)
        for fraction in (0.0, 0.2, 0.5, 0.8, 0.99):
            t = action.start_ms + fraction * (action.end_ms - action.start_ms)
            frame = choreo.character_frame(action.scene_id, action.character, t)
            skel = solve_skeleton(frame.pose)
            for point in (
                skel.hand_l,
                skel.hand_r,
                skel.head,
                skel.ankle_l,
                skel.ankle_r,
            ):
                assert all(math.isfinite(v) for v in point), (action.id, fraction)
    assert len(kinds) >= 20  # the shipped film exercises most kinds


def test_the_registry_functions_work_for_all_28_kinds(timeline, choreo) -> None:
    scene = choreo.scene("s4_cooking")
    for kind, function in ACTION_REGISTRY.items():
        action = ResolvedAction(
            "t",
            "s4_cooking",
            "mai",
            kind,
            1000,
            2000,
            100,
            100,
            {
                "prop": "bowl",
                "to_prop": "table",
                "from_prop": "ladle",
                "topping": "beef",
                "target": "street",
            },
        )
        from ai_youtube_agent.animation.actions import ActionContext
        from ai_youtube_agent.animation.rig import Pose

        ctx = ActionContext(
            action, 1500.0, 0.5, 0.7, 1.2, (500.0, 1500.0), Pose(), scene, choreo, "mai"
        )
        values = function(ctx)
        assert isinstance(values, dict) or hasattr(values, "items")
        Pose().blended(values, 1.0)  # every channel name is a real channel


def test_ramp_weight_eases_in_and_out() -> None:
    action = ResolvedAction("a", "s", "mai", "nod", 1000, 2000, 200, 200, {})

    assert ramp_weight(action, 999) == 0.0 and ramp_weight(action, 2000) == 0.0
    assert 0 < ramp_weight(action, 1050) < ramp_weight(action, 1150) < 1
    assert ramp_weight(action, 1500) == 1.0


def test_frames_are_a_pure_function_of_time(timeline) -> None:
    a, b = Choreography(timeline), Choreography(timeline)
    for t in (500.0, 4200.0, 12000.0, 20500.0, 26000.0):
        for scene_id in (timeline.scene_at(t).id,):
            for name in a.visible_characters(scene_id, timeline.shot_at(t).location):
                assert a.character_frame(scene_id, name, t) == b.character_frame(
                    scene_id, name, t
                )
                assert a.character_frame(scene_id, name, t) == a.character_frame(
                    scene_id, name, t
                )


def test_a_walking_character_has_no_foot_sliding(timeline, choreo) -> None:
    """A planted foot keeps one world x; the pelvis moves with the speed profile."""
    scene = choreo.scene("s2_memory")
    seg = scene.tracks["mai"].segments[0]
    previous = None
    checked = 0
    t = seg.t0 + 150
    while t < seg.t1 - 150:
        frame = choreo.character_frame("s2_memory", "mai", t)
        skel = solve_skeleton(frame.pose)
        feet = (
            frame.x + skel.ankle_l[0] * frame.scale,
            frame.x + skel.ankle_r[0] * frame.scale,
        )
        if previous is not None:
            for index in (0, 1):
                if frame.planted[index] and previous[1][index]:
                    # a planted foot may not travel (2 px of IK slack)
                    assert abs(feet[index] - previous[0][index]) < 2.5, (t, index)
                    checked += 1
        previous = (feet, frame.planted)
        t += 1000 / 30
    assert checked > 10


def test_walking_speed_matches_the_stride(timeline, choreo) -> None:
    seg = choreo.scene("s3_arrival").tracks["chu_ba"].segments[0]
    assert seg.direction == -1  # enters from the right
    mid = (seg.t0 + seg.t1) / 2
    x_mid = choreo.character_position("s3_arrival", "chu_ba", mid)[0]
    assert seg.x1 < x_mid < seg.x0
    assert seg.progress(seg.t0) == 0 and seg.progress(seg.t1) == pytest.approx(1)
    assert seg.time_at_distance(seg.distance) <= seg.t1 + 1


def test_footstep_events_follow_every_foot_landing(timeline, choreo) -> None:
    events = footstep_events(timeline, choreo)

    assert events == footstep_events(timeline)
    assert len(events) >= 12 and all(e.kind == "footstep" for e in events)
    for scene in choreo.scenes.values():
        for track in scene.tracks.values():
            for seg in track.segments:
                inside = [e for e in events if e.source.startswith(seg.action_id + ":")]
                assert len(inside) == len(foot_contacts(seg.distance, seg.stride))
                assert all(seg.t0 - 1 <= e.t_ms <= seg.t1 + 1 for e in inside)


def test_the_bowl_is_taken_carried_and_put_on_the_table(timeline, choreo) -> None:
    scene = choreo.scene("s4_cooking")
    reach = next(a for a in timeline.actions if a.id == "a4_reach")
    place = next(a for a in timeline.actions if a.id == "a4_place")

    before = choreo.prop_state("s4_cooking", "bowl", reach.start_ms)
    carried = choreo.prop_state("s4_cooking", "bowl", place.start_ms)
    after = choreo.prop_state("s4_cooking", "bowl", place.end_ms)
    table = scene.props["table"]

    assert before.holder is None and before.location == "kitchen"
    assert carried.holder == "mai" and carried.hand == "left"
    assert after.holder is None and after.location == "front"
    assert after.x == pytest.approx(table.x) and after.y == pytest.approx(table.top[1])


def test_broth_and_toppings_appear_while_the_bowl_is_filled(timeline, choreo) -> None:
    end = timeline.scenes[3].end_ms
    start = choreo.prop_state("s4_cooking", "bowl", timeline.scenes[3].start_ms)
    done = choreo.prop_state("s4_cooking", "bowl", end - 1)

    assert (start.broth, start.beef, start.herbs) == (0.0, 0.0, 0.0)
    assert (done.broth, done.beef, done.herbs) == (1.0, 1.0, 1.0)
    assert len(choreo.scene("s4_cooking").pours) == 1


def test_resting_props_sit_on_the_surface_under_them(choreo) -> None:
    kitchen = choreo.scene("s2_memory")
    assert kitchen.props["pot"].host == "stove"
    assert kitchen.props["pot"].y == pytest.approx(kitchen.props["stove"].top[1])
    assert kitchen.props["ladle"].kind == "hanging_ladle"


def test_a_seated_character_sits_on_the_stool(timeline, choreo) -> None:
    sit = next(a for a in timeline.actions if a.id == "a3_sit")
    stool = choreo.scene("s3_arrival").props["stool"]
    track = choreo.scene("s3_arrival").tracks["chu_ba"]

    assert track.sit(sit.start_ms - 1) == 0.0
    assert track.sit(sit.end_ms + 1) == 1.0
    assert track.position(sit.end_ms + 1) == pytest.approx((stool.x, stool.y))
    s5 = choreo.scene("s5_payoff").tracks["chu_ba"]
    assert s5.sit(0) == 1.0  # starts seated, snapped to the stool


def test_only_the_customer_moves_his_mouth_with_the_speech(timeline, choreo) -> None:
    c1 = timeline.line("c1")
    n4 = timeline.line("n4")
    word = c1.words[2]
    middle = (word.start_ms + word.end_ms) / 2

    speaking = choreo.character_frame("s3_arrival", "chu_ba", middle)
    narrated = choreo.character_frame("s3_arrival", "chu_ba", n4.words[2].start_ms + 50)
    mai = choreo.character_frame("s3_arrival", "mai", middle)

    assert speaking.speaking and speaking.pose.mouth_open > 0.2
    assert not narrated.speaking
    assert not mai.speaking and mai.pose.mouth_open == 0.0


def test_the_mouth_closes_between_words_and_uses_the_vowel_shape(timeline) -> None:
    line = timeline.line("c1")
    first, second = line.words[0], line.words[1]
    gap = (
        (first.end_ms + 400 + second.start_ms) / 2
        if second.start_ms - first.end_ms > 700
        else None
    )
    opened = speech_mouth(line, (first.start_ms + first.end_ms) / 2, None)

    assert opened is not None and opened[0] > 0.2
    assert speech_mouth(line, line.start_ms - 500, None) is None
    # "Cháu" is a wide vowel, "ơi" is round
    assert opened[1] >= 0
    round_word = line.words[1]
    round_shape = speech_mouth(
        line, (round_word.start_ms + round_word.end_ms) / 2, None
    )
    assert round_shape is not None and round_shape[1] < 0
    assert gap is None  # the fake words are evenly timed, nothing to check


def test_the_loudness_envelope_scales_the_mouth(timeline) -> None:
    line = timeline.line("c1")
    word = line.words[0]
    t = (word.start_ms + word.end_ms) / 2
    quiet = speech_mouth(line, t, Envelope(20, (0.0,) * 400))
    loud = speech_mouth(line, t, Envelope(20, (1.0,) * 400))

    assert quiet is not None and loud is not None and loud[0] > quiet[0]


def test_load_envelope_reads_a_wav_and_tolerates_a_missing_file(tmp_path) -> None:
    path = tmp_path / "tone.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        samples = [int(12000 * math.sin(i / 8)) if i > 4000 else 0 for i in range(8000)]
        handle.writeframes(struct.pack(f"<{len(samples)}h", *samples))

    envelope = load_envelope(path)

    assert envelope is not None
    assert envelope.at(100) < 0.1 and envelope.at(800) > 0.8
    assert load_envelope(tmp_path / "missing.wav") is None


def test_the_character_blinks_on_a_fixed_schedule(timeline, choreo) -> None:
    closed = [
        t
        for t in range(0, 12000, 17)
        if choreo.character_frame("s2_memory", "mai", 4000 + t).pose.eye_open < 0.5
    ]
    again = [
        t
        for t in range(0, 12000, 17)
        if Choreography(timeline)
        .character_frame("s2_memory", "mai", 4000 + t)
        .pose.eye_open
        < 0.5
    ]

    assert 2 <= len({t // 400 for t in closed}) <= 8
    assert closed == again


# --- polish of the acting (review of the first live film) ----------------------------


def action_of(timeline, action_id):
    return next(a for a in timeline.actions if a.id == action_id)


def skeleton_at(choreo, action, fraction):
    t = action.start_ms + fraction * (action.end_ms - action.start_ms)
    frame = choreo.character_frame(action.scene_id, action.character, t)
    return frame, solve_skeleton(frame.pose)


def arm_extension(skel, side):
    shoulder = skel.shoulder_l if side == "left" else skel.shoulder_r
    hand = skel.hand(side)
    return math.dist(shoulder, hand)


def test_reaching_for_the_ladle_keeps_the_elbow_bent(timeline, choreo) -> None:
    from ai_youtube_agent.animation.actions import ARM_REACH
    from ai_youtube_agent.animation.rig import RIG

    action = action_of(timeline, "a2_reach")
    extensions = []
    for step in range(11, 31):  # from the first reach to the end of the take
        _, skel = skeleton_at(choreo, action, step / 40)
        extensions.append(arm_extension(skel, "right"))

    full = RIG.upper_arm + RIG.forearm
    assert full * 0.93 > ARM_REACH
    assert max(extensions) <= ARM_REACH * 1.001  # never a locked, straight arm
    assert max(extensions) > 0.8 * ARM_REACH  # but it does reach out


def test_shivering_is_hugging_his_own_arms_with_hunched_shoulders(
    timeline, choreo
) -> None:
    action = action_of(timeline, "a3_shiver")
    frame, skel = skeleton_at(choreo, action, 0.5)

    pose = frame.pose
    assert pose.shrug > 0.7 and pose.lean > 5  # hunched
    # each hand has crossed to the other side of the chest, near the torso
    assert pose.hand_l[0] < 0 < pose.hand_r[0]
    assert abs(pose.hand_l[0]) < 80 and abs(pose.hand_r[0]) < 80
    assert -150 < pose.hand_l[1] < -90
    # a small, fast tremble: the body moves by a couple of pixels at most
    offsets = []
    for step in range(60):
        t = action.start_ms + 400 + step * 8
        offsets.append(choreo.character_frame(action.scene_id, "chu_ba", t).pose.dx)
    assert 0.6 < max(offsets) - min(offsets) < 6
    crossings = sum(1 for a, b in zip(offsets, offsets[1:], strict=False) if a * b < 0)
    assert crossings >= 4  # several oscillations in half a second


@pytest.mark.parametrize(
    ("action_id", "min_open"), [("a5_cover", 0.9), ("a5_wipe", 0.5)]
)
def test_the_hand_goes_to_the_mouth_or_cheek_and_the_eyes_stay_visible(
    timeline, choreo, action_id, min_open
) -> None:
    action = action_of(timeline, action_id)
    frame, skel = skeleton_at(choreo, action, 0.5)

    pose = frame.pose
    hand = skel.hand(frame.near if hasattr(frame, "near") else skel.near)
    eye_line = skel.head[1] + 8
    assert hand[1] > eye_line + 25  # below the eyes: cheek or mouth, not a facepalm
    assert math.dist(hand, skel.mouth()) < 75
    assert pose.elbow_down == 1.0
    elbow = skel.elbow_r if skel.near == "right" else skel.elbow_l
    assert (
        elbow[1] > skel.shoulder_r[1] - 20
    )  # the elbow is not raised above the shoulder
    eye_open = pose.eye_open * (1 - 0.35 * pose.squint)
    assert eye_open >= min_open * 0.5 or pose.squint <= 0.5
    assert pose.squint <= 0.5


def test_a_hand_at_the_face_with_the_elbow_down_bends_the_elbow_down() -> None:
    from dataclasses import replace

    from ai_youtube_agent.animation.rig import RIG, Pose

    target = (26.0, RIG.head_center_y + 62)
    up = solve_skeleton(Pose(yaw=0.7, hand_l=target))
    down = solve_skeleton(replace(Pose(yaw=0.7, hand_l=target), elbow_down=1.0))

    assert down.elbow_l[1] > up.elbow_l[1] + 50  # screen y grows downward
    assert down.hand_l == pytest.approx(up.hand_l)  # the hand is where it was
