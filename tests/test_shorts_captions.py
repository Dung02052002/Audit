"""I-097 Shorts captions (Prompt Pack v8, prompt #097).

Rules settled on 2026-10-17 (design section "CaptionOverlay (I-097)"):

- a ``CaptionOverlay`` has an id ``c<n>``, a scene, NFC trimmed text, absolute
  times, a position (anchor + offset) and a style; decoding is strict; the
  timeline keeps captions ordered, without overlap and inside their scene;
- ``wrap`` is a greedy word wrap into ``max_lines`` lines of
  ``max_chars_per_line`` characters, on NFC text;
- ``check_captions`` judges display time, wrapping, the format having captions
  and the safe area (caption box estimate and overlay boxes); more than 20
  characters per second is only a warning;
- ``plan_captions`` cuts each scene's section text into phrases timed by word
  weight; ``ShortsProjectService.caption`` saves them;
- ``ready`` with captions on needs valid captions covering every scene; saving
  refuses ``domain.shorts_caption_invalid`` and ``domain.shorts_caption_unsafe``;
  a caption font must be a font asset passing the asset checks.
"""

import dataclasses
import unicodedata

import pytest

from ai_youtube_agent.content.asset import AssetCategory, AssetKind
from ai_youtube_agent.content.script import Script, ScriptSection, SectionKind
from ai_youtube_agent.content.shorts_captions import (
    MAX_CHARS_PER_SECOND,
    IssueKind,
    PixelBox,
    caption_box,
    check_captions,
    covers_every_scene,
    plan_captions,
    wrap,
)
from ai_youtube_agent.content.shorts_config import CaptionRules, ShortsConfig
from ai_youtube_agent.content.shorts_project import ShortsProject, ShortsProjectStatus
from ai_youtube_agent.content.shorts_service import (
    USER_MESSAGES,
    readiness,
)
from ai_youtube_agent.content.shorts_service import (
    ShortsProjectErrorCode as Code,
)
from ai_youtube_agent.content.shorts_timeline import (
    MAX_CAPTION_TEXT,
    AssetRole,
    Box,
    CaptionAlign,
    CaptionAnchor,
    CaptionOverlay,
    CaptionPosition,
    CaptionStyle,
    Scene,
    SceneAsset,
    Timeline,
    TimingSource,
)
from ai_youtube_agent.content.strategy import Resolution, ShortsFormat
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository
from factories import T0, USER, at, make_channel
from test_shorts_planner import add_sectioned_script, words
from test_shorts_project import World, entity_args, refusal
from test_shorts_scene import Stage

CONFIG = ShortsConfig.from_format(ShortsFormat(15, 60))
OFF = ShortsConfig.from_format(ShortsFormat(15, 60, captions=False))
RULES = CONFIG.caption_rules
SAFE = CONFIG.safe_rect
SMALL = CaptionStyle(size_px=30)


def cap(
    number: int = 1,
    scene: str = "s1",
    text: str = "Hello there",
    start: int = 0,
    end: int = 1500,
    **kwargs,
) -> CaptionOverlay:
    return CaptionOverlay(f"c{number}", scene, text, start, end, **kwargs)


def two_scenes(*assets: SceneAsset) -> tuple[Scene, Scene]:
    return (
        Scene("s1", 0, 0, 6000, None, tuple(assets)),
        Scene("s2", 1, 6000, 9000),
    )


def timeline_of(*captions: CaptionOverlay, scenes=None) -> Timeline:
    return Timeline(
        1, 9000, TimingSource.ESTIMATED, scenes or two_scenes(), tuple(captions)
    )


def errors(config: ShortsConfig, timeline: Timeline, kind: IssueKind | None = None):
    check = check_captions(config, timeline)
    return check.errors if kind is None else check.errors_of(kind)


# Value objects


def test_defaults_of_position_and_style() -> None:
    assert CaptionPosition() == CaptionPosition(CaptionAnchor.BOTTOM, 0)
    style = CaptionStyle()
    assert style.font_asset_id is None and style.size_px == 64
    assert style.color == "#FFFFFF" and style.stroke_color == "#000000"
    assert style.stroke_px == 4 and style.align is CaptionAlign.CENTER
    assert style.background is None


def test_a_caption_is_frozen_and_has_a_duration() -> None:
    caption = cap(start=100, end=1100)

    assert caption.duration_ms == 1000
    with pytest.raises(dataclasses.FrozenInstanceError):
        caption.text = "x"  # type: ignore[misc]


def test_to_dict_lists_every_key() -> None:
    assert cap().to_dict() == {
        "id": "c1",
        "scene_id": "s1",
        "text": "Hello there",
        "start_ms": 0,
        "end_ms": 1500,
        "position": {"anchor": "bottom", "offset_px": 0},
        "style": {
            "font_asset_id": None,
            "size_px": 64,
            "color": "#FFFFFF",
            "stroke_color": "#000000",
            "stroke_px": 4,
            "align": "center",
            "background": None,
        },
    }


def test_a_caption_round_trips_through_its_dict() -> None:
    caption = cap(
        position=CaptionPosition(CaptionAnchor.TOP, -20),
        style=CaptionStyle(
            "f1", 40, "#AABBCC", "#112233", 0, CaptionAlign.LEFT, "#00000080"
        ),
    )

    assert CaptionOverlay.from_dict(caption.to_dict()) == caption


def test_position_and_style_may_be_left_out_when_decoding() -> None:
    data = {"id": "c1", "scene_id": "s1", "text": "Hi", "start_ms": 0, "end_ms": 900}

    caption = CaptionOverlay.from_dict(data)

    assert caption.position == CaptionPosition() and caption.style == CaptionStyle()
    assert CaptionPosition.from_dict({}) == CaptionPosition()
    assert CaptionStyle.from_dict({"size_px": 30}).size_px == 30


@pytest.mark.parametrize(
    "bad",
    [
        {"unknown": 1},
        {"position": {"anchor": "bottom", "extra": 1}},
        {"style": {"colour": "#FFFFFF"}},
        {"position": {"anchor": "left"}},
        {"style": {"align": "top"}},
        {"position": None},
        {"style": []},
    ],
)
def test_decoding_a_caption_is_strict(bad) -> None:
    data = {"id": "c1", "scene_id": "s1", "text": "Hi", "start_ms": 0, "end_ms": 900}

    with pytest.raises(ValueError):
        CaptionOverlay.from_dict(data | bad)


@pytest.mark.parametrize("key", ["id", "scene_id", "text", "start_ms", "end_ms"])
def test_decoding_needs_the_required_keys(key) -> None:
    data = {"id": "c1", "scene_id": "s1", "text": "Hi", "start_ms": 0, "end_ms": 900}
    del data[key]

    with pytest.raises(ValueError, match="missing keys"):
        CaptionOverlay.from_dict(data)


def test_decoding_needs_an_object() -> None:
    with pytest.raises(ValueError, match="object"):
        CaptionOverlay.from_dict([])


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "1"},
        {"id": "c0"},
        {"id": "c01"},
        {"id": "x1"},
        {"id": None},
        {"scene_id": ""},
        {"scene_id": " s1"},
        {"text": ""},
        {"text": "   "},
        {"text": " Hi"},
        {"text": "Hi "},
        {"text": 5},
        {"text": "x" * (MAX_CAPTION_TEXT + 1)},
        {"text": unicodedata.normalize("NFD", "tiếng Việt")},
        {"start_ms": -1},
        {"start_ms": True},
        {"end_ms": 0},
        {"end_ms": 1.5},
        {"start_ms": 900, "end_ms": 900},
        {"start_ms": 900, "end_ms": 800},
        {"position": {"anchor": "bottom"}},
        {"style": {}},
    ],
)
def test_bad_captions_are_refused(overrides) -> None:
    base = {"id": "c1", "scene_id": "s1", "text": "Hi", "start_ms": 0, "end_ms": 9}

    with pytest.raises((ValueError, TypeError)):
        CaptionOverlay(**(base | overrides))


def test_the_text_may_hold_composed_vietnamese() -> None:
    text = unicodedata.normalize("NFC", "tiếng Việt")

    assert cap(text=text).text == text


@pytest.mark.parametrize(
    "overrides",
    [
        {"size_px": 15},
        {"size_px": 401},
        {"size_px": True},
        {"size_px": "64"},
        {"color": "#fff"},
        {"color": "#ffffff"},
        {"color": "white"},
        {"color": None},
        {"stroke_color": "#12345G"},
        {"stroke_px": -1},
        {"stroke_px": 41},
        {"background": "#12345"},
        {"background": "#1234567"},
        {"background": "#aabbcc"},
        {"background": "red"},
        {"align": "center"},
        {"font_asset_id": ""},
        {"font_asset_id": " f"},
        {"font_asset_id": 3},
    ],
)
def test_bad_styles_are_refused(overrides) -> None:
    with pytest.raises((ValueError, TypeError)):
        CaptionStyle(**overrides)


def test_the_style_accepts_its_limits() -> None:
    assert CaptionStyle(size_px=16, stroke_px=0).size_px == 16
    assert CaptionStyle(size_px=400, stroke_px=40).stroke_px == 40
    assert CaptionStyle(background="#AABBCC").background == "#AABBCC"
    assert CaptionStyle(background="#AABBCC80").background == "#AABBCC80"


@pytest.mark.parametrize(
    "overrides",
    [
        {"anchor": "bottom"},
        {"offset_px": 4001},
        {"offset_px": -4001},
        {"offset_px": True},
        {"offset_px": 1.5},
    ],
)
def test_bad_positions_are_refused(overrides) -> None:
    with pytest.raises((ValueError, TypeError)):
        CaptionPosition(**overrides)


# The timeline holds the captions


def test_a_timeline_with_captions_round_trips_and_has_a_checksum() -> None:
    timeline = timeline_of(cap(), cap(2, "s2", "Second", 6000, 7500))

    decoded = Timeline.from_json(timeline.to_json())

    assert decoded == timeline
    assert decoded.sha256 == timeline.sha256
    assert decoded.sha256 != timeline_of(cap()).sha256
    assert decoded.to_dict()["captions"][1]["id"] == "c2"
    assert decoded.schema_version == 1


def test_canonical_json_keeps_vietnamese_text_unescaped() -> None:
    text = unicodedata.normalize("NFC", "Việt")

    assert text in timeline_of(cap(text=text)).to_json()


def test_captions_need_scenes() -> None:
    with pytest.raises(ValueError, match="need scenes"):
        Timeline(1, 9000, TimingSource.ESTIMATED, (), (cap(),))


def test_a_caption_must_name_a_scene_of_the_timeline() -> None:
    with pytest.raises(ValueError, match="unknown scene s9"):
        timeline_of(cap(scene="s9"))


def test_caption_ids_follow_the_position() -> None:
    with pytest.raises(ValueError, match="id c1"):
        timeline_of(cap(2))
    with pytest.raises(ValueError, match="id c2"):
        timeline_of(cap(1), cap(3, start=2000, end=3000))


def test_captions_may_not_overlap_but_may_touch() -> None:
    touching = timeline_of(cap(), cap(2, start=1500, end=3000))

    assert len(touching.captions) == 2
    with pytest.raises(ValueError, match="before the previous caption ends"):
        timeline_of(cap(), cap(2, start=1499, end=3000))


def test_captions_must_be_in_time_order() -> None:
    with pytest.raises(ValueError, match="before the previous caption ends"):
        timeline_of(cap(start=3000, end=4000), cap(2, start=0, end=1000))


def test_a_caption_must_lie_within_its_scene() -> None:
    with pytest.raises(ValueError, match="within scene s1"):
        timeline_of(cap(start=5500, end=6500))
    with pytest.raises(ValueError, match="within scene s2"):
        timeline_of(cap(scene="s2", start=5900, end=6500))


def test_a_caption_must_end_within_the_audio() -> None:
    with pytest.raises(ValueError, match="after the audio"):
        timeline_of(cap(scene="s2", start=8500, end=9500))


def test_captions_must_be_caption_values() -> None:
    with pytest.raises(TypeError):
        timeline_of(object())  # type: ignore[arg-type]


# wrap


def test_wrap_keeps_a_short_text_on_one_line() -> None:
    assert wrap("Hello there", RULES) == ("Hello there",)


def test_wrap_breaks_greedily_at_the_line_width() -> None:
    lines = wrap("Most people never check bank fees and then wonder", RULES)

    assert lines == ("Most people never check bank", "fees and then wonder")
    assert all(len(line) <= 32 for line in lines)


def test_wrap_fills_a_line_up_to_the_exact_width() -> None:
    text = "a" * 15 + " " + "b" * 16  # 32 characters

    assert wrap(text, RULES) == (text,)
    assert wrap(text + "c", RULES) == ("a" * 15, "b" * 16 + "c")


def test_wrap_counts_composed_characters_for_decomposed_vietnamese() -> None:
    composed = "Xin chào các bạn, hôm nay chúng ta học tiếng Việt"
    decomposed = unicodedata.normalize("NFD", composed)
    assert len(decomposed) > len(composed)

    lines = wrap(decomposed, RULES)

    assert lines == wrap(composed, RULES)
    assert lines == ("Xin chào các bạn, hôm nay chúng", "ta học tiếng Việt")
    assert all(unicodedata.is_normalized("NFC", line) for line in lines)


def test_wrap_collapses_white_space() -> None:
    assert wrap("  a \n b\t c ", RULES) == ("a b c",)


def test_wrap_refuses_a_word_longer_than_the_line() -> None:
    with pytest.raises(ValueError, match="longer than 32"):
        wrap("short " + "x" * 33, RULES)


def test_wrap_refuses_more_lines_than_allowed() -> None:
    text = " ".join(letter * 30 for letter in "abc")

    with pytest.raises(ValueError, match="needs 3 lines"):
        wrap(text, RULES)
    assert len(wrap(text, CaptionRules(max_lines=3))) == 3


def test_wrap_needs_text() -> None:
    with pytest.raises(ValueError, match="needs text"):
        wrap("   ", RULES)


def test_wrap_follows_the_rules() -> None:
    rules = CaptionRules(max_lines=1, max_chars_per_line=10)

    assert wrap("one two", rules) == ("one two",)
    with pytest.raises(ValueError, match="needs 2 lines"):
        wrap("one two three", rules)


# check_captions: display time, wrapping, the format


def test_valid_captions_have_no_findings() -> None:
    check = check_captions(CONFIG, timeline_of(cap(), cap(2, "s2", "Two", 6000, 7000)))

    assert check.errors == () and check.warnings == () and check.ok


def test_a_timeline_without_captions_is_fine() -> None:
    assert check_captions(CONFIG, timeline_of()).ok
    assert check_captions(OFF, timeline_of()).ok


@pytest.mark.parametrize("end", [699, 1])
def test_a_caption_shown_too_briefly_is_invalid(end) -> None:
    found = errors(CONFIG, timeline_of(cap(end=end)))

    assert [issue.kind for issue in found] == [IssueKind.INVALID]
    assert found[0].target == "c1" and "700" in found[0].message


def test_the_display_limits_are_accepted() -> None:
    assert check_captions(CONFIG, timeline_of(cap(end=700))).ok
    assert check_captions(CONFIG, timeline_of(cap(end=6000))).ok


def test_a_caption_shown_too_long_is_invalid() -> None:
    config = dataclasses.replace(CONFIG, caption_rules=CaptionRules(2, 32, 700, 3000))

    assert check_captions(config, timeline_of(cap(end=3000))).ok
    found = errors(config, timeline_of(cap(end=3001)))
    assert [issue.kind for issue in found] == [IssueKind.INVALID]


def test_a_caption_that_does_not_wrap_is_invalid() -> None:
    found = errors(CONFIG, timeline_of(cap(text="x" * 33)))

    assert [issue.kind for issue in found] == [IssueKind.INVALID]
    assert "longer than 32" in found[0].message


def test_three_lines_are_invalid_with_two_allowed() -> None:
    text = " ".join(letter * 30 for letter in "abc")

    assert errors(CONFIG, timeline_of(cap(text=text)), IssueKind.INVALID)


def test_captions_in_a_format_without_captions_are_invalid() -> None:
    found = errors(OFF, timeline_of(cap()))

    assert [issue.kind for issue in found] == [IssueKind.INVALID]
    assert "captions off" in found[0].message


def test_check_never_raises_for_a_bad_caption() -> None:
    check = check_captions(CONFIG, timeline_of(cap(text="x" * 40, end=100)))

    assert len(check.errors) == 2


# Speed warning


def test_more_than_twenty_characters_per_second_is_only_a_warning() -> None:
    assert MAX_CHARS_PER_SECOND == 20
    caption = cap(text="a" * 30, end=1000, style=SMALL)
    check = check_captions(CONFIG, timeline_of(caption))

    assert check.errors == ()
    assert [issue.kind for issue in check.warnings] == [IssueKind.FAST]
    assert check.warnings[0].target == "c1"
    assert check.ok


def test_exactly_twenty_characters_per_second_is_not_a_warning() -> None:
    caption = cap(text="a" * 20, end=1000, style=SMALL)

    assert check_captions(CONFIG, timeline_of(caption)).warnings == ()


# Safe area


def test_the_safe_rect_of_1080x1920() -> None:
    assert (SAFE.x, SAFE.y, SAFE.right, SAFE.bottom) == (65, 250, 950, 1440)


def test_the_box_estimate() -> None:
    lines = wrap("Hello there", RULES)

    box = caption_box(lines, CaptionPosition(), CaptionStyle(), SAFE)

    # width 11 * 64 * 0.6 rounded up, height round(64 * 1.2) = 77 for one line.
    assert box == PixelBox(65 + (885 - 423) // 2, 1440 - 77, 423, 77)
    two = caption_box(("a", "b"), CaptionPosition(), CaptionStyle(), SAFE)
    assert two.height == 154


@pytest.mark.parametrize(
    ("align", "x"),
    [(CaptionAlign.LEFT, 65), (CaptionAlign.CENTER, 296), (CaptionAlign.RIGHT, 527)],
)
def test_the_alignment_places_the_box_in_the_safe_width(align, x) -> None:
    style = CaptionStyle(align=align)

    assert caption_box(("Hello there",), CaptionPosition(), style, SAFE).x == x


def test_a_bottom_caption_at_the_edge_is_safe() -> None:
    assert check_captions(CONFIG, timeline_of(cap())).ok


@pytest.mark.parametrize(
    ("anchor", "offset", "safe"),
    [
        (CaptionAnchor.BOTTOM, 0, True),
        (CaptionAnchor.BOTTOM, 1, False),
        (CaptionAnchor.BOTTOM, -500, True),
        (CaptionAnchor.TOP, 0, True),
        (CaptionAnchor.TOP, -1, False),
        (CaptionAnchor.TOP, 300, True),
        (CaptionAnchor.CENTER, 0, True),
        (CaptionAnchor.CENTER, -556, True),
        (CaptionAnchor.CENTER, -557, False),
        (CaptionAnchor.CENTER, 557, True),
        (CaptionAnchor.CENTER, 558, False),
    ],
)
def test_the_anchor_and_offset_must_keep_the_box_in_the_safe_area(
    anchor, offset, safe
) -> None:
    caption = cap(position=CaptionPosition(anchor, offset))

    found = errors(CONFIG, timeline_of(caption))

    assert (found == ()) is safe
    if not safe:
        assert [issue.kind for issue in found] == [IssueKind.UNSAFE]


def test_a_caption_too_wide_for_the_safe_area_is_unsafe() -> None:
    text = "a" * 32

    # 32 characters at 46 px are 884 px wide, at 47 px they are 903 px.
    fits = cap(text=text, style=CaptionStyle(size_px=46))
    assert check_captions(CONFIG, timeline_of(fits)).ok
    found = errors(
        CONFIG,
        timeline_of(cap(text=text, style=CaptionStyle(size_px=47))),
        IssueKind.UNSAFE,
    )
    assert len(found) == 1 and found[0].target == "c1"


def test_the_default_size_is_too_wide_for_a_full_line() -> None:
    assert errors(CONFIG, timeline_of(cap(text="a" * 32)), IssueKind.UNSAFE)


def test_the_safe_area_follows_the_canvas() -> None:
    config = ShortsConfig.from_format(
        ShortsFormat(15, 60, resolution=Resolution.HD_720)
    )
    text = "a" * 32

    # 720 px wide: the safe width is 591 px, so 30 px is the largest size.
    assert config.safe_rect.width == 591
    assert check_captions(config, timeline_of(cap(text=text, style=SMALL))).ok
    too_big = cap(text=text, style=CaptionStyle(size_px=31))
    assert errors(config, timeline_of(too_big), IssueKind.UNSAFE)


def test_unsafe_and_invalid_findings_are_kept_apart() -> None:
    caption = cap(text="a" * 32, end=100)

    found = errors(CONFIG, timeline_of(caption))

    assert {issue.kind for issue in found} == {IssueKind.INVALID, IssueKind.UNSAFE}


# Overlay boxes


def overlay(box: Box | None, role=AssetRole.OVERLAY, asset_id="o1") -> SceneAsset:
    return SceneAsset(asset_id, role, box=box)


def test_an_overlay_box_inside_the_safe_area_is_fine() -> None:
    scenes = two_scenes(overlay(Box(100, 200, 500, 500)))

    assert check_captions(CONFIG, timeline_of(scenes=scenes)).ok


def test_an_overlay_box_on_the_edge_of_the_safe_area_is_fine() -> None:
    # x 61 permille = 65.88 px (>= 65), right 879 = 949.3 px, y 131 = 251.5 px,
    # bottom 750 = 1440 px.
    scenes = two_scenes(overlay(Box(61, 131, 818, 619)))

    assert check_captions(CONFIG, timeline_of(scenes=scenes)).ok


@pytest.mark.parametrize(
    "box",
    [
        Box(60, 131, 819, 619),  # left of x 65 (64.8 px)
        Box(61, 130, 818, 620),  # above y 250 (249.6 px)
        Box(61, 131, 819, 619),  # right of x 950 (950.4 px)
        Box(61, 131, 818, 620),  # below y 1440 (1441.9 px)
        Box(0, 0, 1000, 1000),
    ],
)
def test_an_overlay_box_outside_the_safe_area_is_unsafe(box) -> None:
    scenes = two_scenes(overlay(box))

    found = errors(CONFIG, timeline_of(scenes=scenes), IssueKind.UNSAFE)

    assert len(found) == 1 and found[0].target == "s1:o1"


def test_a_full_bleed_background_is_allowed() -> None:
    scenes = two_scenes(overlay(Box(0, 0, 1000, 1000), AssetRole.BACKGROUND, "bg"))

    assert check_captions(CONFIG, timeline_of(scenes=scenes)).ok


def test_an_overlay_without_a_box_is_not_measured() -> None:
    assert check_captions(CONFIG, timeline_of(scenes=two_scenes(overlay(None)))).ok


def test_overlay_boxes_are_checked_even_when_captions_are_off() -> None:
    scenes = two_scenes(overlay(Box(0, 0, 500, 500)))

    assert errors(OFF, timeline_of(scenes=scenes), IssueKind.UNSAFE)


# Coverage and readiness


BACKGROUND = SceneAsset("bg", AssetRole.BACKGROUND)


def backed_scenes() -> tuple[Scene, Scene]:
    return (
        Scene("s1", 0, 0, 6000, None, (BACKGROUND,)),
        Scene("s2", 1, 6000, 9000, None, (BACKGROUND,)),
    )


def test_covers_every_scene() -> None:
    both = timeline_of(cap(), cap(2, "s2", "Two", 6000, 7000))

    assert covers_every_scene(both)
    assert not covers_every_scene(timeline_of(cap()))
    assert not covers_every_scene(timeline_of())
    assert not covers_every_scene(Timeline.empty(9000))


def test_ready_with_captions_on_needs_captions_in_every_scene() -> None:
    scenes = backed_scenes()
    both = timeline_of(cap(), cap(2, "s2", "Two", 6000, 7000), scenes=scenes)

    assert readiness(CONFIG, both) is ShortsProjectStatus.READY
    assert readiness(CONFIG, timeline_of(cap(), scenes=scenes)) is (
        ShortsProjectStatus.PLANNED
    )
    assert readiness(CONFIG, timeline_of(scenes=scenes)) is ShortsProjectStatus.PLANNED


def test_ready_needs_the_backgrounds_as_well() -> None:
    both = timeline_of(cap(), cap(2, "s2", "Two", 6000, 7000))

    assert readiness(CONFIG, both) is ShortsProjectStatus.PLANNED


def test_invalid_captions_are_never_ready() -> None:
    bad = timeline_of(
        cap(end=100), cap(2, "s2", "Two", 6000, 7000), scenes=backed_scenes()
    )

    assert readiness(CONFIG, bad) is ShortsProjectStatus.PLANNED


def test_ready_with_captions_off_needs_no_captions() -> None:
    assert readiness(OFF, timeline_of(scenes=backed_scenes())) is (
        ShortsProjectStatus.READY
    )
    with_captions = timeline_of(cap(), scenes=backed_scenes())
    assert readiness(OFF, with_captions) is ShortsProjectStatus.PLANNED


def test_readiness_without_scenes_is_a_draft() -> None:
    assert readiness(CONFIG, Timeline.empty(9000)) is ShortsProjectStatus.DRAFT


# plan_captions


def make_script(*texts: str) -> Script:
    sections = [ScriptSection.create(SectionKind.BODY, text) for text in texts]
    return Script.create("ci1", sections=sections, clock=at(T0))


def make_project(
    script: Script, duration_ms: int, config: ShortsConfig = CONFIG
) -> ShortsProject:
    return ShortsProject.create(
        "ci1",
        **entity_args(
            script_id=script.id,
            script_version=script.version,
            audio_duration_ms=duration_ms,
            config=config,
        ),
    )


def planned(texts, durations, config: ShortsConfig = CONFIG):
    """The captions of a script with one scene per text, and their timeline."""
    script = make_script(*texts)
    scenes = []
    start = 0
    for index, length in enumerate(durations):
        scenes.append(Scene(f"s{index + 1}", index, start, start + length))
        start += length
    timeline = Timeline(1, start, TimingSource.ESTIMATED, tuple(scenes))
    project = make_project(script, start, config)
    return plan_captions(project, script, timeline), timeline


def test_one_phrase_fills_its_scene() -> None:
    captions, _ = planned(["Alpha beta gamma delta."], [3000])

    assert [(c.id, c.scene_id, c.start_ms, c.end_ms, c.text) for c in captions] == [
        ("c1", "s1", 0, 3000, "Alpha beta gamma delta.")
    ]
    assert captions[0].position == CaptionPosition()
    assert captions[0].style == CaptionStyle()


def test_sentences_are_timed_by_word_weight_exactly() -> None:
    captions, _ = planned(["One two. Three four five six."], [6000])

    assert [(c.text, c.start_ms, c.end_ms) for c in captions] == [
        ("One two.", 0, 2000),
        ("Three four five six.", 2000, 6000),
    ]


def test_commas_split_a_sentence_and_the_remainder_is_largest_first() -> None:
    captions, _ = planned(["One, two three."], [5000])

    # 5000 ms over 1 and 2 words: 1666 + 3333, the spare ms goes to the first.
    assert [(c.text, c.start_ms, c.end_ms) for c in captions] == [
        ("One,", 0, 1667),
        ("two three.", 1667, 5000),
    ]


def test_a_short_phrase_is_merged_with_its_neighbour() -> None:
    captions, _ = planned(["Go. Now. Please stop it."], [3000])

    # 600 ms for "Go." is under the 700 ms minimum.
    assert [(c.text, c.start_ms, c.end_ms) for c in captions] == [
        ("Go. Now.", 0, 1200),
        ("Please stop it.", 1200, 3000),
    ]


def test_a_long_phrase_is_split_in_the_middle() -> None:
    captions, _ = planned(["one two three four five six seven eight"], [6400])

    assert [(c.text, c.start_ms, c.end_ms) for c in captions] == [
        ("one two three four", 0, 3200),
        ("five six seven eight", 3200, 6400),
    ]


def test_a_clause_that_does_not_wrap_is_cut_into_chunks() -> None:
    word = "abcdefghij"  # three words fit a line of 32 characters
    captions, _ = planned([" ".join([word] * 12)], [6000])

    assert [c.text for c in captions] == [" ".join([word] * 6)] * 2
    assert [(c.start_ms, c.end_ms) for c in captions] == [(0, 3000), (3000, 6000)]
    assert all(len(wrap(c.text, RULES)) == 2 for c in captions)


def test_every_scene_is_tiled_exactly_and_ids_run_on() -> None:
    texts = ["Most people never check bank fees, and that costs them. Do it today!"]
    texts += ["Short one.", "Another sentence here, with a comma, and more words."]
    durations = [7001, 2333, 9127]

    captions, timeline = planned(texts, durations)

    assert [c.id for c in captions] == [f"c{n}" for n in range(1, len(captions) + 1)]
    for scene in timeline.scenes:
        own = [c for c in captions if c.scene_id == scene.id]
        assert own[0].start_ms == scene.start_ms
        assert own[-1].end_ms == scene.end_ms
        assert all(a.end_ms == b.start_ms for a, b in zip(own, own[1:], strict=False))
        assert all(
            RULES.min_display_ms <= c.duration_ms <= RULES.max_display_ms for c in own
        )
    # The result is a valid timeline and passes every rule.
    full = dataclasses.replace(timeline, captions=captions)
    assert check_captions(CONFIG, full).errors == ()


def test_decomposed_script_text_gives_composed_captions() -> None:
    composed = "Xin chào các bạn, hôm nay chúng ta học tiếng Việt."
    decomposed = unicodedata.normalize("NFD", composed)

    from_nfc, _ = planned([composed], [6000])
    from_nfd, _ = planned([decomposed], [6000])

    assert from_nfd == from_nfc
    assert all(unicodedata.is_normalized("NFC", c.text) for c in from_nfd)
    # Weights count words, not combining marks: 4 words and 7 words.
    assert [c.text for c in from_nfd] == [
        "Xin chào các bạn,",
        "hôm nay chúng ta học tiếng Việt.",
    ]
    assert [c.end_ms - c.start_ms for c in from_nfd] == [2182, 3818]


def test_the_font_shrinks_to_keep_a_full_line_in_the_safe_width() -> None:
    captions, timeline = planned(["a" * 32], [3000])

    assert captions[0].style.size_px == 46
    assert check_captions(CONFIG, dataclasses.replace(timeline, captions=captions)).ok


def test_a_short_line_keeps_the_default_size() -> None:
    captions, _ = planned(["Hello there"], [3000])

    assert captions[0].style.size_px == 64


def test_the_font_is_smaller_on_a_smaller_canvas() -> None:
    config = ShortsConfig.from_format(
        ShortsFormat(15, 60, resolution=Resolution.HD_720)
    )

    captions, timeline = planned(["a" * 32], [3000], config)

    assert captions[0].style.size_px == 30
    assert check_captions(config, dataclasses.replace(timeline, captions=captions)).ok


def test_a_scene_shorter_than_the_minimum_display_cannot_be_captioned() -> None:
    with pytest.raises(ValueError, match="scene s1: .*cannot be merged"):
        planned(["Hello there"], [600])


def test_a_single_word_shown_too_long_cannot_be_split() -> None:
    with pytest.raises(ValueError, match="scene s1: .*cannot be split"):
        planned(["Hello"], [6500])


def test_a_word_longer_than_the_line_is_an_error() -> None:
    with pytest.raises(ValueError, match="scene s1: .*longer than 32"):
        planned(["x" * 33], [3000])


def test_the_second_scene_is_named_in_the_error() -> None:
    with pytest.raises(ValueError, match="scene s2"):
        planned(["Fine text here", "Hello there"], [3000, 600])


def test_planning_needs_captions_on() -> None:
    with pytest.raises(ValueError, match="captions off"):
        planned(["Hello there"], [3000], OFF)


def test_planning_needs_the_bound_script() -> None:
    script = make_script("Hello there")
    other = make_script("Hello there")
    timeline = Timeline(1, 3000, TimingSource.ESTIMATED, (Scene("s1", 0, 0, 3000),))

    with pytest.raises(ValueError, match="not the script version"):
        plan_captions(make_project(script, 3000), other, timeline)


def test_planning_needs_scenes() -> None:
    script = make_script("Hello there")

    with pytest.raises(ValueError, match="plan the scenes"):
        plan_captions(make_project(script, 3000), script, Timeline.empty(3000))


def test_planning_needs_one_scene_per_section() -> None:
    script = make_script("Hello there", "Second section")
    shared = Timeline(
        1,
        4000,
        TimingSource.ESTIMATED,
        (Scene("s1", 0, 0, 2000), Scene("s2", 0, 2000, 4000)),
    )
    outside = Timeline(
        1,
        4000,
        TimingSource.ESTIMATED,
        (Scene("s1", 0, 0, 2000), Scene("s2", 5, 2000, 4000)),
    )
    project = make_project(script, 4000)

    with pytest.raises(ValueError, match="one scene per section"):
        plan_captions(project, script, shared)
    with pytest.raises(ValueError, match="section the script lacks"):
        plan_captions(project, script, outside)


# The service


@pytest.fixture
def stage(database: Database) -> Stage:
    return Stage(database)


def captioned(stage: Stage):
    return stage.world.service.caption(
        stage.project.id, expected_version=stage.project.version, actor=USER
    )


def with_captions(stage: Stage, *captions: CaptionOverlay) -> Timeline:
    return dataclasses.replace(stage.project.timeline, captions=tuple(captions))


def background_scenes(stage: Stage) -> tuple[Scene, ...]:
    return tuple(
        dataclasses.replace(
            scene, assets=(SceneAsset(stage.add_asset().id, AssetRole.BACKGROUND),)
        )
        for scene in stage.project.timeline.scenes
    )


def test_caption_plans_and_saves_the_captions_of_every_scene(stage: Stage) -> None:
    before = stage.project

    saved = captioned(stage)

    # Scenes of 6050, 9075 and 15125 ms hold 2, 3 and 5 words: every word is one
    # caption of 3025 ms (a share over 6000 ms is split).
    assert saved.version == before.version + 1
    assert saved.timeline.scenes == before.timeline.scenes
    assert [c.scene_id for c in saved.timeline.captions] == (
        ["s1"] * 2 + ["s2"] * 3 + ["s3"] * 5
    )
    assert {(c.text, c.duration_ms) for c in saved.timeline.captions} == {
        ("word", 3025)
    }
    assert saved.timeline.captions[-1].end_ms == saved.audio_duration_ms
    assert saved.status is ShortsProjectStatus.PLANNED
    assert stage.world.project_of() == saved
    assert check_captions(saved.config, saved.timeline).ok


def test_caption_audits_counts_without_the_text(stage: Stage) -> None:
    captioned(stage)

    event = stage.world.events()[-1]
    assert event.action == "shorts.timeline_saved"
    assert event.metadata["caption_count"] == 10
    assert event.metadata["caption_warning_count"] == 0
    assert "word" not in repr(dict(event.metadata))


def test_caption_keeps_the_assets_and_the_project_becomes_ready(stage: Stage) -> None:
    stage.project = stage.save(
        dataclasses.replace(stage.project.timeline, scenes=background_scenes(stage))
    )

    saved = captioned(stage)

    assert saved.status is ShortsProjectStatus.READY
    assert all(len(scene.assets) == 1 for scene in saved.timeline.scenes)
    assert len(saved.timeline.captions) == 10


def test_captions_then_backgrounds_make_the_project_ready(stage: Stage) -> None:
    stage.project = captioned(stage)
    scenes = background_scenes(stage)

    saved = stage.save(dataclasses.replace(stage.project.timeline, scenes=scenes))

    assert saved.status is ShortsProjectStatus.READY
    assert saved.timeline.captions == stage.project.timeline.captions


def test_captioning_again_replaces_the_captions(stage: Stage) -> None:
    stage.project = captioned(stage)

    again = captioned(stage)

    assert again.version == stage.project.version + 1
    assert again.timeline.captions == stage.project.timeline.captions


def test_replanning_the_scenes_drops_the_captions(stage: Stage) -> None:
    stage.project = captioned(stage)

    replanned = stage.world.service.plan(
        stage.project.id, expected_version=stage.project.version, actor=USER
    )

    assert replanned.timeline.captions == ()


def test_caption_needs_the_current_version(stage: Stage) -> None:
    refusal(
        lambda: stage.world.service.caption(
            stage.project.id, expected_version=stage.project.version + 1, actor=USER
        ),
        Code.CONFLICT,
    )

    assert stage.world.project_of() == stage.project


def test_caption_of_a_draft_asks_for_scenes_first(database: Database) -> None:
    world = World(database)
    project = world.create()

    error = refusal(
        lambda: world.service.caption(
            project.id, expected_version=project.version, actor=USER
        ),
        Code.CAPTION_INVALID,
    )

    assert "plan the scenes" in str(error)
    assert world.project_of() == project


def test_caption_is_refused_when_the_format_has_captions_off(
    database: Database,
) -> None:
    stage = Stage(database, captions=False)

    refusal(lambda: captioned(stage), Code.CAPTION_INVALID)

    assert stage.world.project_of() == stage.project


def test_caption_of_a_scene_that_is_too_short_is_refused(
    database: Database,
) -> None:
    # Eleven 1-word sections of 500 ms each leave no room for a 700 ms caption.
    world = World(database, ShortsFormat(15, 60))
    add_sectioned_script(world, [words(1)] * 11, duration_ms=5500)
    project = world.service.create(world.item.id, actor=USER)
    project = world.service.plan(project.id, expected_version=1, actor=USER)

    refusal(
        lambda: world.service.caption(
            project.id, expected_version=project.version, actor=USER
        ),
        Code.CAPTION_INVALID,
    )

    assert world.project_of() == project


# The service: saving captions


def test_a_valid_caption_list_is_saved(stage: Stage) -> None:
    caption = cap(text="Hi there", start=0, end=3000)

    saved = stage.save(with_captions(stage, caption))

    assert saved.timeline.captions == (caption,)
    assert saved.status is ShortsProjectStatus.PLANNED


def test_saving_captions_audits_a_speed_warning_count(stage: Stage) -> None:
    stage.save(with_captions(stage, cap(text="a" * 30, end=1000, style=SMALL)))

    event = stage.world.events()[-1]
    assert event.metadata["caption_count"] == 1
    assert event.metadata["caption_warning_count"] == 1


def test_a_caption_shown_too_briefly_is_refused(stage: Stage) -> None:
    error = stage.refuse(with_captions(stage, cap(end=600)), Code.CAPTION_INVALID)

    assert "700" in str(error)


def test_a_caption_that_does_not_wrap_is_refused(stage: Stage) -> None:
    stage.refuse(with_captions(stage, cap(text="x" * 33)), Code.CAPTION_INVALID)


def test_a_caption_outside_the_safe_area_is_refused(stage: Stage) -> None:
    position = CaptionPosition(CaptionAnchor.BOTTOM, 10)

    stage.refuse(with_captions(stage, cap(position=position)), Code.CAPTION_UNSAFE)


def test_an_overlay_outside_the_safe_area_is_refused(stage: Stage) -> None:
    image = stage.add_asset()
    placed = SceneAsset(image.id, AssetRole.OVERLAY, box=Box(0, 0, 1000, 1000))

    stage.refuse(stage.timeline(placed), Code.CAPTION_UNSAFE)


def test_an_overlay_inside_the_safe_area_is_saved(stage: Stage) -> None:
    image = stage.add_asset()
    placed = SceneAsset(image.id, AssetRole.OVERLAY, box=Box(100, 200, 500, 500))

    assert stage.save(stage.timeline(placed)).timeline.scenes[0].assets[0].box


def test_invalid_is_reported_before_unsafe(stage: Stage) -> None:
    caption = cap(text="a" * 32, end=100)

    stage.refuse(with_captions(stage, caption), Code.CAPTION_INVALID)


def test_captions_are_refused_when_the_format_has_them_off(
    database: Database,
) -> None:
    stage = Stage(database, captions=False)

    stage.refuse(with_captions(stage, cap()), Code.CAPTION_INVALID)


def test_a_refused_caption_save_audits_nothing_new(stage: Stage) -> None:
    before = len(stage.world.events())

    stage.refuse(with_captions(stage, cap(end=600)), Code.CAPTION_INVALID)

    assert len(stage.world.events()) == before


def test_the_caption_error_codes_have_messages() -> None:
    assert Code.CAPTION_INVALID.value == "domain.shorts_caption_invalid"
    assert Code.CAPTION_UNSAFE.value == "domain.shorts_caption_unsafe"
    assert USER_MESSAGES[Code.CAPTION_INVALID]
    assert USER_MESSAGES[Code.CAPTION_UNSAFE]


# The service: caption fonts


def with_font(stage: Stage, font_id: str) -> Timeline:
    style = CaptionStyle(font_asset_id=font_id)
    return with_captions(stage, cap(text="Hi there", end=3000, style=style))


def test_a_font_asset_is_accepted(stage: Stage) -> None:
    font = stage.add_asset(AssetKind.FONT)

    saved = stage.save(with_font(stage, font.id))

    assert saved.timeline.captions[0].style.font_asset_id == font.id


def test_a_font_that_is_not_a_font_asset_is_refused(stage: Stage) -> None:
    image = stage.add_asset(AssetKind.IMAGE)

    stage.refuse(with_font(stage, image.id), Code.ASSET_KIND_MISMATCH)


def test_an_unknown_font_is_refused(stage: Stage) -> None:
    stage.refuse(with_font(stage, "missing"), Code.ASSET_NOT_FOUND)


def test_a_font_of_another_channel_is_refused(stage: Stage, database: Database) -> None:
    other = make_channel()
    with database.transaction() as connection:
        ChannelRepository(connection).add(other)
    foreign = stage.add_asset(AssetKind.FONT, channel_id=other.id, attach=False)

    stage.refuse(with_font(stage, foreign.id), Code.ASSET_OTHER_CHANNEL)


def test_a_font_not_attached_to_the_item_is_refused(stage: Stage) -> None:
    loose = stage.add_asset(AssetKind.FONT, attach=False)

    stage.refuse(with_font(stage, loose.id), Code.ASSET_NOT_ATTACHED)


def test_a_font_with_an_unknown_category_is_refused(stage: Stage) -> None:
    unknown = stage.add_asset(AssetKind.FONT, category=AssetCategory.UNKNOWN)

    stage.refuse(with_font(stage, unknown.id), Code.ASSET_RIGHTS_UNKNOWN)
