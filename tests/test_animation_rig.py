"""Puppet rig: proportions, IK, walk cycle, visemes, skeleton (no Pillow needed)."""

from __future__ import annotations

import math

import pytest

from ai_youtube_agent.animation.rig import (
    CHU_BA,
    MAI,
    OUTLINE_RGB,
    RIG,
    Pose,
    bend_toward,
    foot_contacts,
    get_style,
    near_side,
    plan_walk,
    rest_hand,
    shade_of,
    solve_skeleton,
    solve_two_bone_ik,
    stride_for_speed,
    viseme_for_syllable,
    walk_cycle,
)


def dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def test_the_head_is_about_a_third_of_the_height() -> None:
    assert RIG.head_h / RIG.height == pytest.approx(1 / 3.2, abs=0.03)


def test_two_characters_share_the_outline_and_the_shade_rule() -> None:
    assert MAI.outline == CHU_BA.outline == OUTLINE_RGB
    assert MAI.outline_w == CHU_BA.outline_w
    assert CHU_BA.scale == pytest.approx(MAI.scale * 0.92)
    assert get_style("mai") is MAI and get_style("chu_ba") is CHU_BA
    with pytest.raises(KeyError):
        get_style("nobody")
    shaded = shade_of(MAI.top)
    assert shaded != MAI.top
    assert all(abs(a - b) < 70 for a, b in zip(shaded, MAI.top, strict=True))


@pytest.mark.parametrize("bend", [1, -1])
def test_ik_keeps_bone_lengths_and_reaches_the_target(bend: int) -> None:
    joint, end = solve_two_bone_ik((0, 0), (120, 90), 100, 92, bend)

    assert end == pytest.approx((120, 90))
    assert dist((0, 0), joint) == pytest.approx(100)
    assert dist(joint, end) == pytest.approx(92)


def test_ik_clamps_an_unreachable_target_and_chooses_the_bend_side() -> None:
    joint, end = solve_two_bone_ik((0, 0), (900, 0), 100, 92)
    assert dist((0, 0), end) < 192
    assert end[1] == pytest.approx(0, abs=1e-6)

    up, _ = solve_two_bone_ik(
        (0, 0), (120, 0), 100, 92, bend_toward((0, 0), (120, 0), (0, -1))
    )
    down, _ = solve_two_bone_ik(
        (0, 0), (120, 0), 100, 92, bend_toward((0, 0), (120, 0), (0, 1))
    )
    assert up[1] < 0 < down[1]


def test_plan_walk_ends_after_a_whole_number_of_steps() -> None:
    stride, steps = plan_walk(500, 330)

    assert steps >= 1
    assert stride * steps / 2 == pytest.approx(500)
    assert stride_for_speed(10) == 150 and stride_for_speed(10_000) == 360


def test_a_planted_foot_never_moves_in_the_world() -> None:
    """Feet are planted between landings: hip moves, foot offset cancels it."""
    stride = 330.0
    previous: dict[str, float] = {}
    for step in range(400):
        distance = step * 1.0  # hip x
        frame = walk_cycle(0.25 + distance / stride, stride)
        for name, foot, planted in (
            ("a", frame.foot_a, frame.planted_a),
            ("b", frame.foot_b, frame.planted_b),
        ):
            world = distance + foot[0]
            if planted and previous.get(name + "_planted"):
                assert world == pytest.approx(previous[name], abs=1e-6)
            previous[name] = world
            previous[name + "_planted"] = planted
        assert (
            frame.planted_a != frame.planted_b or frame.planted_a
        )  # never both airborne


def test_the_walk_lifts_only_the_swinging_foot() -> None:
    frame = walk_cycle(0.25, 330)
    lifted = [
        f
        for f, p in ((frame.foot_a, frame.planted_a), (frame.foot_b, frame.planted_b))
        if not p
    ]
    assert len(lifted) == 1 and lifted[0][1] > 10
    assert frame.bob < 0  # the body is highest at the passing pose


def test_foot_contacts_end_when_the_walk_stops() -> None:
    contacts = foot_contacts(400, 200)

    assert contacts == sorted(contacts)
    assert contacts[0] == pytest.approx(50)
    assert contacts[-1] == pytest.approx(400)
    assert all(
        b - a <= 100 + 1e-6 for a, b in zip(contacts, contacts[1:], strict=False)
    )


@pytest.mark.parametrize(
    ("syllable", "viseme"),
    [
        ("bác", "wide"),
        ("ơi", "round"),
        ("nhé", "spread"),
        ("Mười", "round"),
        ("ứ", "round"),
        ("ngh", "closed"),
    ],
)
def test_visemes_follow_the_first_vowel(syllable: str, viseme: str) -> None:
    assert viseme_for_syllable(syllable) == viseme


def test_pose_blending_moves_channels_and_rejects_unknown_ones() -> None:
    pose = Pose()
    blended = pose.blended(
        {"smile": 1.0, "hand_left": (0.0, 0.0), "tool": "spoon"}, 0.5
    )

    assert blended.smile == pytest.approx(0.675)
    assert blended.hand_l == pytest.approx((40.0, 18.0))
    assert blended.tool == "spoon"  # switches at weight 0.5
    assert pose.blended({"smile": 1.0}, 0.0) is pose
    with pytest.raises(KeyError):
        pose.blended({"wings": 1.0}, 1.0)


def test_the_skeleton_keeps_bone_lengths_and_the_hands_follow_their_targets() -> None:
    pose = Pose(yaw=0.7, hand_r=(60.0, -240.0), hand_l=rest_hand("left", 0.7))
    skel = solve_skeleton(pose)

    assert dist(skel.shoulder_r, skel.elbow_r) == pytest.approx(RIG.upper_arm)
    assert dist(skel.elbow_r, skel.hand_r) == pytest.approx(RIG.forearm)
    target = (skel.pelvis[0] + 60.0, skel.pelvis[1] - 240.0)
    assert dist(skel.hand_r, target) < 1.0
    assert skel.near == near_side(0.7) == "right"
    assert skel.facing == 1.0
    assert skel.ankle_l[1] == pytest.approx(skel.ankle_r[1])  # both feet on the ground


def test_the_torso_narrows_as_the_body_turns() -> None:
    front = solve_skeleton(Pose(yaw=0.0))
    turned = solve_skeleton(Pose(yaw=0.9))

    front_width = abs(front.shoulder_l[0] - front.shoulder_r[0])
    turned_width = abs(turned.shoulder_l[0] - turned.shoulder_r[0])
    assert turned_width < front_width * 0.8


def test_sitting_lowers_the_pelvis_and_puts_the_feet_forward() -> None:
    standing = solve_skeleton(Pose(yaw=0.7))
    seated = solve_skeleton(Pose(yaw=0.7, sit=1.0))

    assert seated.pelvis[1] > standing.pelvis[1] + 60
    assert seated.ankle_l[0] > standing.ankle_l[0] + 30
    assert seated.knee_l[1] < seated.ankle_l[1]  # knees above the feet
