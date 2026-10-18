"""I-094 Shorts configuration (Prompt Pack v8, prompt #094).

Rules the user accepted on 2026-10-17:

- a ``ShortsConfig`` is copied from the strategy's ``ShortsFormat``: a 9:16
  canvas whose size follows the resolution, 30 fps, the format's seconds and
  captions flag, the default safe area and the default caption rules;
- the safe area is in permille of the canvas (top 130, bottom 250, left 60,
  right 120) and is 65..950 by 250..1440 pixels at 1080x1920;
- the caption rules are 2 lines of 32 characters, shown 700 to 6000 ms;
- ``to_dict`` and ``from_dict`` are strict: an unknown key, an unknown
  version or an inconsistent canvas is a ``ValueError``; a missing optional
  key takes its default.
"""

import dataclasses

import pytest

from ai_youtube_agent.content.shorts_config import (
    FPS,
    SHORTS_CONFIG_VERSION,
    VERTICAL_SIZES,
    CaptionRules,
    Rect,
    SafeArea,
    ShortsConfig,
)
from ai_youtube_agent.content.strategy import AspectRatio, Resolution, ShortsFormat

DESIGN_DICT = {
    "config_version": 1,
    "aspect_ratio": "9:16",
    "resolution": "1080p",
    "width": 1080,
    "height": 1920,
    "fps": 30,
    "min_seconds": 15,
    "max_seconds": 60,
    "captions": True,
    "safe_area": {"top": 130, "bottom": 250, "left": 60, "right": 120},
    "caption_rules": {
        "max_lines": 2,
        "max_chars_per_line": 32,
        "min_display_ms": 700,
        "max_display_ms": 6000,
    },
}


def make_config(**overrides) -> ShortsConfig:
    config = ShortsConfig.from_format(ShortsFormat(15, 60))
    return dataclasses.replace(config, **overrides)


# From the strategy format


def test_from_format_copies_the_strategy_format() -> None:
    config = ShortsConfig.from_format(
        ShortsFormat(20, 45, Resolution.HD_720, captions=False)
    )

    assert config.min_seconds == 20
    assert config.max_seconds == 45
    assert config.resolution is Resolution.HD_720
    assert (config.width, config.height) == (720, 1280)
    assert config.captions is False
    assert config.aspect_ratio is AspectRatio.VERTICAL
    assert config.fps == FPS == 30


def test_from_format_uses_the_default_safe_area_and_caption_rules() -> None:
    config = make_config()

    assert config.safe_area == SafeArea(130, 250, 60, 120)
    assert config.caption_rules == CaptionRules(2, 32, 700, 6000)


@pytest.mark.parametrize(
    ("resolution", "size"),
    [
        (Resolution.HD_720, (720, 1280)),
        (Resolution.FULL_HD_1080, (1080, 1920)),
        (Resolution.UHD_2160, (2160, 3840)),
    ],
)
def test_every_resolution_has_a_9_16_canvas(resolution, size) -> None:
    config = ShortsConfig.from_format(ShortsFormat(15, 60, resolution))

    assert (config.width, config.height) == size == VERTICAL_SIZES[resolution]
    assert config.width * 16 == config.height * 9


def test_a_config_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        make_config().fps = 60  # type: ignore[misc]


# Safe area and rectangles


def test_the_safe_rect_at_1080x1920_is_the_design_rectangle() -> None:
    rect = make_config().safe_rect

    assert rect == Rect(65, 250, 885, 1190)
    assert (rect.x, rect.right) == (65, 950)
    assert (rect.y, rect.bottom) == (250, 1440)


def test_the_safe_rect_scales_with_the_canvas() -> None:
    rect = SafeArea().rect(720, 1280)

    assert rect.x == 43  # 720 * 60 / 1000 = 43.2, rounded
    assert rect.y == 166  # 1280 * 130 / 1000 = 166.4, rounded
    assert 0 < rect.right < 720
    assert 0 < rect.bottom < 1280


def test_a_rect_contains_what_lies_inside_and_on_its_edge() -> None:
    outer = Rect(10, 10, 100, 100)

    assert outer.contains(Rect(10, 10, 100, 100))
    assert outer.contains(Rect(20, 20, 10, 10))
    assert not outer.contains(Rect(9, 10, 50, 50))
    assert not outer.contains(Rect(10, 10, 101, 100))
    assert not outer.contains(Rect(10, 10, 100, 101))


@pytest.mark.parametrize(
    ("x", "y", "width", "height"),
    [(-1, 0, 1, 1), (0, -1, 1, 1), (0, 0, 0, 1), (0, 0, 1, 0), (True, 0, 1, 1)],
)
def test_a_bad_rect_is_refused(x, y, width, height) -> None:
    with pytest.raises(ValueError):
        Rect(x, y, width, height)


@pytest.mark.parametrize(
    "margins",
    [
        {"top": -1},
        {"left": 1000},
        {"top": 500, "bottom": 500},
        {"left": 600, "right": 400},
        {"bottom": True},
        {"right": 1.5},
    ],
)
def test_a_bad_safe_area_is_refused(margins) -> None:
    with pytest.raises(ValueError):
        SafeArea(**margins)


# Caption rules


@pytest.mark.parametrize(
    "rules",
    [
        {"max_lines": 0},
        {"max_lines": 5},
        {"max_chars_per_line": 9},
        {"max_chars_per_line": 61},
        {"min_display_ms": 99},
        {"max_display_ms": 30_001},
        {"min_display_ms": 7000},
        {"max_lines": True},
    ],
)
def test_bad_caption_rules_are_refused(rules) -> None:
    with pytest.raises(ValueError):
        CaptionRules(**rules)


def test_caption_rules_accept_their_limits() -> None:
    assert CaptionRules(1, 10, 100, 100).max_lines == 1
    assert CaptionRules(4, 60, 30_000, 30_000).max_chars_per_line == 60


# The configuration itself


@pytest.mark.parametrize(
    "overrides",
    [
        {"min_seconds": 0},
        {"max_seconds": 181},
        {"min_seconds": 61},
        {"width": 1081},
        {"height": 1080},
        {"resolution": Resolution.HD_720},
        {"aspect_ratio": AspectRatio.HORIZONTAL},
        {"fps": 0},
        {"fps": 121},
        {"fps": True},
    ],
)
def test_an_inconsistent_config_is_refused(overrides) -> None:
    with pytest.raises(ValueError):
        make_config(**overrides)


def test_a_config_with_the_wrong_types_is_refused() -> None:
    with pytest.raises(TypeError):
        make_config(captions="yes")
    with pytest.raises(TypeError):
        make_config(safe_area={"top": 1})
    with pytest.raises(TypeError):
        make_config(caption_rules=None)
    with pytest.raises(TypeError):
        make_config(resolution="1080p")


# Encoding


def test_to_dict_is_the_design_layout() -> None:
    assert make_config().to_dict() == DESIGN_DICT
    assert DESIGN_DICT["config_version"] == SHORTS_CONFIG_VERSION


def test_from_dict_round_trips_every_resolution() -> None:
    for resolution in Resolution:
        config = ShortsConfig.from_format(ShortsFormat(10, 90, resolution, False))

        assert ShortsConfig.from_dict(config.to_dict()) == config


def test_from_dict_takes_defaults_for_missing_optional_keys() -> None:
    config = ShortsConfig.from_dict({"min_seconds": 15, "max_seconds": 60})

    assert config == make_config()


def test_from_dict_derives_the_canvas_from_the_resolution() -> None:
    config = ShortsConfig.from_dict(
        {"min_seconds": 15, "max_seconds": 60, "resolution": "720p"}
    )

    assert (config.width, config.height) == (720, 1280)


def test_from_dict_reads_a_partial_safe_area_and_caption_rules() -> None:
    config = ShortsConfig.from_dict(
        {
            "min_seconds": 15,
            "max_seconds": 60,
            "safe_area": {"top": 100},
            "caption_rules": {"max_lines": 3},
        }
    )

    assert config.safe_area == SafeArea(top=100)
    assert config.caption_rules == CaptionRules(max_lines=3)


@pytest.mark.parametrize(
    "change",
    [
        {"extra": 1},
        {"config_version": 2},
        {"config_version": 0},
        {"config_version": True},
        {"config_version": "1"},
        {"resolution": "480p"},
        {"aspect_ratio": "16:9"},
        {"aspect_ratio": "1:1"},
        {"width": 720},
        {"min_seconds": "15"},
        {"max_seconds": 15.5},
        {"fps": None},
        {"safe_area": {"top": 130, "extra": 1}},
        {"safe_area": {"top": 600, "bottom": 600}},
        {"safe_area": []},
        {"caption_rules": {"max_lines": 9}},
        {"caption_rules": {"unknown": 1}},
    ],
)
def test_from_dict_refuses_what_is_not_valid(change) -> None:
    with pytest.raises(ValueError):
        ShortsConfig.from_dict(DESIGN_DICT | change)


@pytest.mark.parametrize("missing", ["min_seconds", "max_seconds"])
def test_from_dict_needs_the_seconds(missing) -> None:
    data = {key: value for key, value in DESIGN_DICT.items() if key != missing}

    with pytest.raises(ValueError, match=missing):
        ShortsConfig.from_dict(data)


@pytest.mark.parametrize("data", [None, [], "config", 5])
def test_from_dict_needs_an_object(data) -> None:
    with pytest.raises(ValueError, match="object"):
        ShortsConfig.from_dict(data)
