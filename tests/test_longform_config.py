import pytest

from ai_youtube_agent.content.longform_config import (
    DEFAULT_LONGFORM_CAPTION_RULES,
    DEFAULT_LONGFORM_SAFE_AREA,
    HORIZONTAL_SIZES,
    LongFormConfig,
)
from ai_youtube_agent.content.shorts_config import AspectRatio
from ai_youtube_agent.content.strategy import LongFormFormat


def test_longform_config_defaults_and_canvas_size() -> None:
    config = LongFormConfig()

    assert config.aspect_ratio is AspectRatio.HORIZONTAL
    assert config.canvas_size == (1920, 1080)
    assert (config.width, config.height) in HORIZONTAL_SIZES.values()
    assert config.safe_area == DEFAULT_LONGFORM_SAFE_AREA
    assert config.caption_rules == DEFAULT_LONGFORM_CAPTION_RULES
    assert config.chapters is True
    assert config.captions is True


def test_longform_config_from_format_and_round_trip() -> None:
    spec = LongFormFormat(
        min_seconds=360,
        max_seconds=900,
        captions=True,
        chapters=False,
    )
    config = LongFormConfig.from_format(spec)
    restored = LongFormConfig.from_dict(config.to_dict())

    assert restored == config
    assert restored.min_seconds == 360
    assert restored.max_seconds == 900
    assert restored.chapters is False


def test_longform_config_refuses_vertical_or_invalid_bounds() -> None:
    with pytest.raises(ValueError, match="16:9"):
        LongFormConfig(aspect_ratio=AspectRatio.VERTICAL)
    with pytest.raises(ValueError, match="16:9"):
        LongFormConfig(width=1080, height=1920)
    with pytest.raises(ValueError, match="181"):
        LongFormConfig(min_seconds=60, max_seconds=300)
    with pytest.raises(ValueError, match="14400"):
        LongFormConfig(min_seconds=300, max_seconds=20000)
