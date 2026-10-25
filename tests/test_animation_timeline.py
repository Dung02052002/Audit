"""Timeline resolution: absolute times, anchors, transitions, SFX (pure)."""

from __future__ import annotations

import math

import pytest

from ai_youtube_agent.animation.errors import StoryboardError
from ai_youtube_agent.animation.storyboard import load_storyboard
from ai_youtube_agent.animation.timeline import resolve_timeline
from animation_fixtures import (
    fake_spoken,
    scene_of,
    shipped_dict,
    shipped_storyboard,
)


@pytest.fixture(scope="module")
def timeline():
    storyboard = shipped_storyboard()
    return resolve_timeline(storyboard, fake_spoken(storyboard))


def resolve_changed(change) -> None:
    data = shipped_dict()
    change(data)
    storyboard = load_storyboard(data)
    return resolve_timeline(storyboard, fake_spoken(storyboard))


def failure_of(change, spoken_ms_per_word: int = 300) -> tuple[str, ...]:
    data = shipped_dict()
    change(data)
    storyboard = load_storyboard(data)
    with pytest.raises(StoryboardError) as info:
        resolve_timeline(
            storyboard, fake_spoken(storyboard, ms_per_word=spoken_ms_per_word)
        )
    return info.value.issues


# --- scenes and the clock ------------------------------------------------------


def test_scenes_are_contiguous_and_cover_the_whole_film(timeline) -> None:
    starts = [scene.start_ms for scene in timeline.scenes]
    ends = [scene.end_ms for scene in timeline.scenes]

    assert starts[0] == 0
    assert starts[1:] == ends[:-1]
    assert ends[-1] == timeline.total_ms
    assert 20000 <= timeline.total_ms <= 30000


def test_scene_lasts_the_longer_of_the_minimum_and_speech_plus_hold(timeline) -> None:
    s1, s2 = timeline.scenes[0], timeline.scenes[1]
    n3 = timeline.line("n3")

    assert s1.duration_ms == 3800  # the minimum wins
    assert s2.duration_ms == max(6100, n3.end_ms - s2.start_ms + 350)


def test_frame_count_is_the_ceiling_of_total_times_fps(timeline) -> None:
    assert timeline.fps == 30
    assert timeline.frame_count == math.ceil(timeline.total_ms * 30 / 1000)
    assert timeline.frame_time_ms(30) == pytest.approx(1000.0)


# --- lines ---------------------------------------------------------------------


def test_line_times_are_absolute_and_words_follow_the_audio(timeline) -> None:
    n1 = timeline.line("n1")
    n2 = timeline.line("n2")
    s2 = timeline.scenes[1]

    assert n1.start_ms == 350
    assert n2.start_ms == s2.start_ms + 400
    assert n1.words[0].start_ms == n1.start_ms + 30
    assert all(
        a.end_ms <= b.start_ms + 30
        for a, b in zip(n1.words, n1.words[1:], strict=False)
    )
    assert (
        n1.end_ms - n1.start_ms == fake_spoken(shipped_storyboard())["n1"].duration_ms
    )


def test_a_line_chained_to_a_line_end_waits_for_its_pause(timeline) -> None:
    n2, n3 = timeline.line("n2"), timeline.line("n3")

    assert n3.start_ms == n2.end_ms + n2.pause_after_ms


def test_overlapping_speech_is_an_error() -> None:
    def change(data):
        scene_of(data, "s3_arrival")["lines"][1]["at"] = {"scene_ms": 1000}

    assert any("still speaking" in issue for issue in failure_of(change))


def test_a_missing_spoken_line_is_an_error() -> None:
    storyboard = shipped_storyboard()
    spoken = fake_spoken(storyboard)
    del spoken["c2"]

    with pytest.raises(StoryboardError, match="no spoken audio for line 'c2'"):
        resolve_timeline(storyboard, spoken)


def test_spoken_lines_may_be_given_as_a_sequence() -> None:
    storyboard = shipped_storyboard()
    as_list = list(fake_spoken(storyboard).values())

    assert (
        resolve_timeline(storyboard, as_list).total_ms
        == resolve_timeline(storyboard, fake_spoken(storyboard)).total_ms
    )


# --- actions -------------------------------------------------------------------


def test_actions_resolve_every_anchor_kind(timeline) -> None:
    actions = {a.id: a for a in timeline.actions}
    s2, s3 = timeline.scenes[1], timeline.scenes[2]
    n2 = timeline.line("n2")

    assert actions["a1_look"].start_ms == 1600  # scene_ms
    assert actions["a2_smile"].start_ms == n2.words[3].start_ms  # line word
    assert actions["a5_wipe"].start_ms == actions["a5_cover"].end_ms  # after_action
    # Mai leaves only once the thumbs-up is done, so she never acts off-screen.
    assert actions["a3_leave"].start_ms == actions["a3_thumbs"].end_ms
    assert actions["a3_leave"].end_ms <= s3.end_ms
    assert actions["a2_stroke"].start_ms == s2.start_ms + 2900
    assert actions["a3_nod"].start_ms == timeline.line("c1").words[5].start_ms


def test_a_scene_end_anchor_counts_back_from_the_scene_end() -> None:
    def change(data):
        for action in scene_of(data, "s3_arrival")["actions"]:
            if action["id"] == "a3_leave":
                action["at"] = {"scene_end_ms": 1100}
                action["duration_ms"] = 1100

    timeline = resolve_changed(change)
    s3 = timeline.scenes[2]
    leave = next(a for a in timeline.actions if a.id == "a3_leave")

    assert leave.end_ms == s3.end_ms
    assert leave.start_ms == s3.end_ms - 1100


def test_actions_never_run_past_their_scene(timeline) -> None:
    scenes = {scene.id: scene for scene in timeline.scenes}

    for action in timeline.actions:
        scene = scenes[action.scene_id]
        assert scene.start_ms <= action.start_ms < action.end_ms <= scene.end_ms


def test_an_action_after_the_scene_end_is_an_error() -> None:
    def change(data):
        scene_of(data, "s1_hook")["actions"][0]["at"] = {"scene_ms": 99999}

    assert any("outside its scene" in issue for issue in failure_of(change))


def test_circular_action_anchors_are_an_error() -> None:
    def change(data):
        actions = scene_of(data, "s1_hook")["actions"]
        actions[0]["at"] = {"after_action": actions[1]["id"]}
        actions[1]["at"] = {"after_action": actions[0]["id"]}

    assert any("circular or missing anchor" in issue for issue in failure_of(change))


# --- transitions ---------------------------------------------------------------


def test_transitions_are_centred_on_the_boundary_and_keep_the_total(timeline) -> None:
    dissolve, whip, cut_a, cut_b = timeline.transitions
    s1, s2 = timeline.scenes[0], timeline.scenes[1]

    assert (dissolve.kind, dissolve.duration_ms) == ("steam_dissolve", 450)
    assert dissolve.boundary_ms == s1.end_ms == s2.start_ms
    assert dissolve.start_ms == s1.end_ms - 225
    assert dissolve.end_ms == s1.end_ms + 225
    assert (whip.kind, whip.direction, whip.duration_ms) == ("whip_pan", "right", 320)
    assert cut_a.kind == cut_b.kind == "cut"
    assert cut_a.duration_ms == 0


def test_scenes_are_drawn_half_a_transition_early_and_late(timeline) -> None:
    first, second, third = timeline.scenes[:3]

    assert first.render_start_ms == 0
    assert first.render_end_ms == first.end_ms + 225
    assert second.render_start_ms == second.start_ms - 225
    assert second.render_end_ms == second.end_ms + 160
    assert third.render_start_ms == third.start_ms - 160


def test_speech_may_not_run_into_a_transition() -> None:
    def change(data):
        scene_of(data, "s2_memory")["lines"][0]["at"] = {"scene_ms": 100}

    issues = failure_of(change)

    assert any(
        "steam_dissolve" in issue and "within 325 ms" in issue for issue in issues
    )


def test_transition_lookups(timeline) -> None:
    dissolve = timeline.transitions[0]

    assert timeline.transition_at(dissolve.boundary_ms) is dissolve
    assert timeline.transition_at(dissolve.start_ms - 1) is None
    assert timeline.transition_at(timeline.transitions[2].boundary_ms) is None  # cut


# --- total length --------------------------------------------------------------


def test_a_film_outside_the_range_is_an_error() -> None:
    assert any("outside 20000-30000" in i for i in failure_of(lambda d: None, 700))


# --- shots, effects, SFX -------------------------------------------------------


def test_shots_cover_their_scene_with_absolute_moves(timeline) -> None:
    s1, s2 = timeline.scenes[0], timeline.scenes[1]
    shots = {shot.id: shot for shot in timeline.shots}

    assert (shots["s1a"].start_ms, shots["s1a"].end_ms) == (0, s1.end_ms)
    assert shots["s2a"].end_ms == shots["s2b"].start_ms == s2.start_ms + 2900
    assert shots["s2b"].end_ms == s2.end_ms
    push = shots["s2b"].moves[0]
    assert (push.t0_ms, push.t1_ms) == (
        shots["s2b"].start_ms,
        shots["s2b"].start_ms + 3000,
    )
    assert shots["s4b"].location == "front"
    assert shots["s4a"].location == "kitchen"


def test_scene_and_shot_lookups(timeline) -> None:
    s2 = timeline.scenes[1]

    assert timeline.scene_at(0).id == "s1_hook"
    assert timeline.scene_at(s2.start_ms).id == "s2_memory"
    assert timeline.scene_at(timeline.total_ms + 5).id == "s5_payoff"
    assert timeline.shot_at(s2.start_ms + 100).id == "s2a"
    assert timeline.shot_at(s2.start_ms + 3000).id == "s2b"


def test_effects_are_resolved_with_anchors(timeline) -> None:
    effects = {e.id: e for e in timeline.effects}
    n5 = timeline.line("n5")

    assert effects["e4_splash"].start_ms == n5.words[3].start_ms
    s5 = timeline.scenes[4]
    assert effects["e5_lanterns"].start_ms == s5.start_ms + 4300
    assert effects["e5_lanterns"].end_ms == s5.end_ms  # clipped to the scene


def test_sfx_come_from_transitions_actions_and_params(timeline) -> None:
    kinds = {(e.kind, e.source) for e in timeline.sfx}
    times = [e.t_ms for e in timeline.sfx]

    assert ("steam_hiss", "transition:steam_dissolve") in kinds
    assert ("whoosh", "transition:whip_pan") in kinds
    assert ("clink", "a4_ladle") in kinds
    assert ("bowl_place", "a4_place") in kinds
    assert ("chair", "a3_sit") in kinds
    assert ("door_bell", "a2_door") in kinds
    assert times == sorted(times)
    whoosh = next(e for e in timeline.sfx if e.kind == "whoosh")
    assert whoosh.t_ms == timeline.transitions[1].boundary_ms - 160


def test_resolution_is_deterministic() -> None:
    first = resolve_changed(lambda data: None)
    second = resolve_changed(lambda data: None)

    assert first == second
