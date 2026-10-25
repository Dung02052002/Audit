"""Storyboard parsing and validation (pure; the shipped storyboard must be valid)."""

from __future__ import annotations

import json

import pytest

from ai_youtube_agent.animation.errors import StoryboardError
from ai_youtube_agent.animation.storyboard import (
    ACTION_KINDS,
    Framing,
    estimate_total_ms,
    load_storyboard,
    syllable_count,
    text_tokens,
)
from animation_fixtures import SHIPPED, scene_of, shipped_dict, shipped_storyboard

VERBATIM_LINES = {
    "n1": "Sáng nay, quán của Mai vắng khách.",
    "n2": "Chiếc muôi cũ này là của mẹ để lại.",
    "n3": "Mười năm rồi, Mai vẫn nấu theo công thức ấy.",
    "n4": "Bỗng một bác lớn tuổi bước vào, run vì lạnh.",
    "c1": "Cháu ơi, cho bác một bát phở nóng nhé!",
    "n5": "Mai chan nước dùng nóng hổi, thêm thịt, thêm hành.",
    "n6": "Bác nếm thử, rồi mỉm cười.",
    "c2": "Ngon như ngày xưa đấy!",
    "n7": "Quán nhỏ thôi, nhưng ấm cả buổi sáng.",
}


def issues_of(data: dict) -> tuple[str, ...]:
    with pytest.raises(StoryboardError) as info:
        load_storyboard(data)
    return info.value.issues


def has(issues: tuple[str, ...], fragment: str) -> bool:
    return any(fragment in issue for issue in issues)


# --- the shipped storyboard ---------------------------------------------------


def test_shipped_storyboard_parses_and_validates() -> None:
    storyboard = shipped_storyboard()

    assert storyboard.id == "quan_an_nho_shorts"
    assert [scene.id for scene in storyboard.scenes] == [
        "s1_hook",
        "s2_memory",
        "s3_arrival",
        "s4_cooking",
        "s5_payoff",
    ]
    assert (storyboard.format.width, storyboard.format.height) == (1080, 1920)
    assert storyboard.format.fps == 30


def test_shipped_lines_are_verbatim_and_use_the_decided_voices() -> None:
    storyboard = shipped_storyboard()

    assert {line.id: line.text for line in storyboard.all_lines()} == VERBATIM_LINES
    narrator = storyboard.voice("narrator")
    customer = storyboard.voice("customer")
    assert (narrator.voice_id, narrator.rate, narrator.pitch) == (
        "vi-VN-HoaiMyNeural",
        "-6%",
        "+0Hz",
    )
    assert (customer.voice_id, customer.rate, customer.pitch) == (
        "vi-VN-NamMinhNeural",
        "-10%",
        "-4Hz",
    )
    voices = {line.id: line.voice for line in storyboard.all_lines()}
    assert voices["c1"] == voices["c2"] == "customer"
    assert {v for k, v in voices.items() if k.startswith("n")} == {"narrator"}


def test_shipped_locations_and_framings() -> None:
    storyboard = shipped_storyboard()

    assert [location.id for location in storyboard.locations] == ["front", "kitchen"]
    framings = {shot.framing for s in storyboard.scenes for shot in s.shots}
    assert framings == set(Framing)
    assert estimate_total_ms(storyboard) in range(*storyboard.format.duration_ms_range)


def test_shipped_actions_use_only_known_kinds() -> None:
    storyboard = shipped_storyboard()
    kinds = {a.kind for s in storyboard.scenes for a in s.actions}

    assert kinds <= set(ACTION_KINDS)
    assert {"reach_take", "ladle_to_bowl", "place_bowl", "sit", "taste"} <= kinds


def test_shipped_storyboard_matches_the_scene_plan() -> None:
    scenes = {s.id: s for s in shipped_storyboard().scenes}

    assert [s.min_duration_ms for s in scenes.values()][1:3] == [6100, 7100]
    assert scenes["s1_hook"].shots[0].framing is Framing.EXTREME_CLOSE
    assert [s.framing for s in scenes["s5_payoff"].shots] == [
        Framing.CLOSE_UP,
        Framing.MEDIUM,
        Framing.WIDE,
    ]
    assert scenes["s4_cooking"].shots[1].location == "front"
    assert scenes["s1_hook"].transition_out.kind == "steam_dissolve"
    assert scenes["s2_memory"].transition_out.kind == "whip_pan"
    assert scenes["s5_payoff"].transition_out is None


def test_tokens_and_syllables_of_vietnamese_text() -> None:
    assert text_tokens("Mai chan nước dùng nóng hổi, thêm thịt, thêm hành.") == [
        "Mai",
        "chan",
        "nước",
        "dùng",
        "nóng",
        "hổi,",
        "thêm",
        "thịt,",
        "thêm",
        "hành.",
    ]
    assert syllable_count("Ngon như ngày xưa đấy!") == 5
    assert syllable_count("... !") == 0


# --- loading ------------------------------------------------------------------


def test_load_accepts_a_path_a_string_and_a_dict() -> None:
    from_path = load_storyboard(SHIPPED)
    from_text = load_storyboard(str(SHIPPED))
    from_dict = load_storyboard(shipped_dict())

    assert from_path == from_text == from_dict


def test_load_reports_unreadable_and_malformed_files(tmp_path) -> None:
    with pytest.raises(StoryboardError, match="cannot read"):
        load_storyboard(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(StoryboardError, match="not valid JSON"):
        load_storyboard(bad)


# --- validation: every problem at once ----------------------------------------


def test_unknown_action_kind_is_rejected_with_its_name() -> None:
    data = shipped_dict()
    scene_of(data, "s1_hook")["actions"][0]["kind"] = "moonwalk"

    issues = issues_of(data)

    assert has(issues, "unknown action kind 'moonwalk'")


def test_all_issues_are_reported_together() -> None:
    data = shipped_dict()
    scene_of(data, "s1_hook")["actions"][0]["kind"] = "moonwalk"
    scene_of(data, "s2_memory")["lines"][0]["voice"] = "ghost"
    scene_of(data, "s3_arrival")["actions"][0]["character"] = "nobody"
    data["scenes"][3]["location"] = "attic"

    issues = issues_of(data)

    assert len(issues) >= 4
    assert has(issues, "moonwalk")
    assert has(issues, "unknown voice 'ghost'")
    assert has(issues, "'nobody' is not in the scene cast")
    assert has(issues, "unknown location 'attic'")


def test_unknown_keys_are_rejected() -> None:
    data = shipped_dict()
    data["scenes"][0]["colour"] = "red"

    assert has(issues_of(data), "unknown key 'colour'")


def test_word_index_must_be_inside_the_line() -> None:
    data = shipped_dict()
    scene_of(data, "s2_memory")["actions"][2]["at"]["word"] = 99

    assert has(issues_of(data), "word 99 is outside line 'n2'")


def test_anchors_must_exist() -> None:
    data = shipped_dict()
    s2 = scene_of(data, "s2_memory")
    s2["actions"][0]["at"] = {"line": "n9", "edge": "start"}
    s2["actions"][1]["at"] = {"after_action": "a_missing"}

    issues = issues_of(data)

    assert has(issues, "anchor line 'n9'")
    assert has(issues, "after_action 'a_missing'")


def test_a_line_must_be_anchored_to_an_earlier_line() -> None:
    data = shipped_dict()
    s2 = scene_of(data, "s2_memory")
    s2["lines"][0]["at"] = {"line": "n3", "edge": "end"}

    assert has(issues_of(data), "anchored to a line declared before it")


def test_a_line_cannot_start_after_an_action_or_before_the_scene_end() -> None:
    data = shipped_dict()
    s2 = scene_of(data, "s2_memory")
    s2["lines"][0]["at"] = {"after_action": "a2_walk"}
    s2["lines"][1]["at"] = {"scene_end_ms": 500}

    issues = issues_of(data)

    assert (
        sum("a line starts at scene_ms or after another line" in i for i in issues) == 2
    )


def test_ids_must_be_unique() -> None:
    data = shipped_dict()
    scene_of(data, "s2_memory")["actions"][0]["id"] = "a1_look"
    scene_of(data, "s3_arrival")["lines"][0]["id"] = "n1"

    issues = issues_of(data)

    assert has(issues, "action id 'a1_look' is used more than once")
    assert has(issues, "line id 'n1' is used more than once")


def test_scene_count_must_be_four_to_six() -> None:
    data = shipped_dict()
    data["scenes"] = data["scenes"][:3]

    assert has(issues_of(data), "needs 4 to 6 scenes, found 3")


def test_at_least_three_distinct_framings_are_required() -> None:
    data = shipped_dict()
    for scene in data["scenes"]:
        for shot in scene["shots"]:
            shot["framing"] = "MEDIUM" if shot["framing"] != "WIDE" else "WIDE"

    assert has(issues_of(data), "distinct framings")


def test_shots_start_at_zero_and_increase() -> None:
    data = shipped_dict()
    scene_of(data, "s2_memory")["shots"][0]["start_ms"] = 100
    scene_of(data, "s5_payoff")["shots"][1]["start_ms"] = 0

    issues = issues_of(data)

    assert has(issues, "the first shot must start at 0")
    assert has(issues, "strictly increasing")


def test_a_scene_needs_a_shot() -> None:
    data = shipped_dict()
    scene_of(data, "s3_arrival")["shots"] = []

    assert has(issues_of(data), "needs at least one shot")


def test_scene_one_needs_an_early_line_and_a_moving_hook() -> None:
    data = shipped_dict()
    s1 = scene_of(data, "s1_hook")
    s1["lines"][0]["at"] = {"scene_ms": 2500}
    s1["shots"][0]["moves"] = []

    issues = issues_of(data)

    assert has(issues, "needs a line starting by scene_ms 1500")
    assert has(issues, "moving hook")


def test_consecutive_scenes_must_not_look_the_same() -> None:
    data = shipped_dict()
    s3 = scene_of(data, "s3_arrival")
    text = json.dumps(s3)
    ids = (
        [s3["id"]]
        + [item["id"] for key in ("lines", "actions", "shots") for item in s3[key]]
        + ["s3_arrival"]
    )
    for item_id in ids:
        text = text.replace(f'"{item_id}"', f'"{item_id}_copy"')
    copy_of_s3 = json.loads(text)
    data["scenes"].insert(3, copy_of_s3)

    assert has(issues_of(data), "look the same")


def test_estimated_duration_must_fit_the_format_range() -> None:
    data = shipped_dict()
    data["format"]["duration_ms_range"] = [20000, 25000]

    assert has(issues_of(data), "estimated duration")


def test_transition_rules() -> None:
    data = shipped_dict()
    scene_of(data, "s1_hook")["transition_out"] = {"kind": "dip", "duration_ms": 400}
    scene_of(data, "s2_memory")["transition_out"] = {
        "kind": "whip_pan",
        "duration_ms": 300,
    }
    scene_of(data, "s3_arrival")["transition_out"] = {
        "kind": "cut",
        "duration_ms": 200,
    }
    scene_of(data, "s4_cooking").pop("transition_out")

    issues = issues_of(data)

    assert has(issues, "'dip' is only allowed on the last scene")
    assert has(issues, "whip_pan needs a direction")
    assert has(issues, "a cut has no duration")
    assert has(issues, "transition_out is required except on the last scene")


def test_params_must_point_at_props_targets_and_known_sfx() -> None:
    data = shipped_dict()
    s2 = scene_of(data, "s2_memory")
    s2["actions"][1]["params"]["prop"] = "spaceship"
    s2["actions"][5]["params"] = {"target": "moon", "sfx": "kazoo"}

    issues = issues_of(data)

    assert has(issues, "param prop 'spaceship' is not a prop of the scene")
    assert has(issues, "param target 'moon' is not a known target")
    assert has(issues, "unknown sfx 'kazoo'")


def test_location_grade_and_camera_values_are_checked() -> None:
    data = shipped_dict()
    scene_of(data, "s1_hook")["grade"] = "morning"
    data["locations"][0]["grades"] = ["warm", "dawn"]
    scene_of(data, "s5_payoff")["shots"][2]["moves"][0]["to"] = {}
    scene_of(data, "s5_payoff")["shots"][2]["moves"][1]["to"] = {"x": 0.5}
    scene_of(data, "s1_hook")["shots"][0]["moves"][0]["ease"] = "wobble"

    issues = issues_of(data)

    assert has(issues, "pull_back needs 'zoom' or 'framing'")
    assert has(issues, "pan_to needs 'x' and 'y'")
    assert has(issues, "'wobble' is not one of")


def test_voice_and_format_values_are_checked() -> None:
    data = shipped_dict()
    data["voices"]["narrator"]["rate"] = "slow"
    data["voices"]["customer"]["voice_id"] = "not a voice"
    data["format"]["duration_ms_range"] = [30000, 20000]
    data["schema_version"] = 2

    issues = issues_of(data)

    assert has(issues, "voices.narrator.rate")
    assert has(issues, "is not an Edge neural voice")
    assert has(issues, "duration_ms_range")
    assert has(issues, "only version 1 is supported")


def test_shipped_json_is_plain_utf8_json() -> None:
    text = SHIPPED.read_text(encoding="utf-8")

    assert json.loads(text)["language"] == "vi-VN"
    assert "Quán Ăn Nhỏ" in text
