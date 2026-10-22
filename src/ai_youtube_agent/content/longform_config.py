"""LongForm configuration (Prompt Pack v8, prompt J-105), context C8 Video Production.

``LongFormConfig`` is the production setup of one LongForm project, copied from
the channel strategy's ``LongFormFormat`` (#049) when the project is created or
re-bound, so a later strategy change never rewrites what a project was made for.
It is stored as the ``format_json`` column of ``longform_projects`` (migration
0028):

- the horizontal canvas: ``aspect_ratio`` (always 16:9), ``resolution`` and the
  matching ``width`` x ``height`` (``HORIZONTAL_SIZES``), and the frame rate;
- ``min_seconds`` and ``max_seconds`` of the LongForm format (181 to 14,400
  seconds), ``captions`` and ``chapters``;
- ``SafeArea``: margins in permille of the horizontal canvas (top 50, bottom
  100, left 50 and right 50 by default);
- ``CaptionRules``: at most 2 lines of 42 characters, shown for 1000 to 7000 ms
  by default.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any

from ai_youtube_agent.content.shorts_config import (
    FPS,
    MAX_FPS,
    CaptionRules,
    Rect,
    SafeArea,
)
from ai_youtube_agent.content.strategy import (
    LONGFORM_MAX_SECONDS,
    LONGFORM_MIN_SECONDS,
    AspectRatio,
    LongFormFormat,
    Resolution,
)

LONGFORM_CONFIG_VERSION = 1
DEFAULT_LONGFORM_SAFE_AREA = SafeArea(top=50, bottom=100, left=50, right=50)
DEFAULT_LONGFORM_CAPTION_RULES = CaptionRules(
    max_lines=2,
    max_chars_per_line=42,
    min_display_ms=1000,
    max_display_ms=7000,
)
HORIZONTAL_SIZES: Mapping[Resolution, tuple[int, int]] = MappingProxyType(
    {
        Resolution.HD_720: (1280, 720),
        Resolution.FULL_HD_1080: (1920, 1080),
        Resolution.UHD_2160: (3840, 2160),
    }
)

CONFIG_KEYS = frozenset(
    {
        "config_version",
        "aspect_ratio",
        "resolution",
        "width",
        "height",
        "fps",
        "min_seconds",
        "max_seconds",
        "captions",
        "chapters",
        "safe_area",
        "caption_rules",
    }
)
REQUIRED_CONFIG_KEYS = frozenset({"min_seconds", "max_seconds"})


def _whole(name: str, value: object, low: int, high: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise ValueError(f"{name} must be a whole number from {low} to {high}")


def _keys(
    name: str,
    data: object,
    allowed: frozenset[str],
    required: frozenset[str] = frozenset(),
) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise ValueError(f"{name} must be an object")
    unknown = sorted(str(key) for key in set(data) - allowed)
    if unknown:
        raise ValueError(f"{name} has unknown keys: {', '.join(unknown)}")
    missing = sorted(required - set(data))
    if missing:
        raise ValueError(f"{name} is missing keys: {', '.join(missing)}")
    return data


def _enum(kind: type[Enum], value: object, name: str) -> Any:
    try:
        return kind(value)
    except (ValueError, TypeError):
        raise ValueError(f"{name} {value!r} is not valid") from None


@dataclass(frozen=True)
class LongFormConfig:
    min_seconds: int = 181
    max_seconds: int = 900
    resolution: Resolution = Resolution.FULL_HD_1080
    width: int = 1920
    height: int = 1080
    fps: int = FPS
    captions: bool = True
    chapters: bool = True
    aspect_ratio: AspectRatio = AspectRatio.HORIZONTAL
    safe_area: SafeArea = DEFAULT_LONGFORM_SAFE_AREA
    caption_rules: CaptionRules = DEFAULT_LONGFORM_CAPTION_RULES

    def __post_init__(self) -> None:
        _whole(
            "min_seconds",
            self.min_seconds,
            LONGFORM_MIN_SECONDS,
            LONGFORM_MAX_SECONDS,
        )
        _whole(
            "max_seconds",
            self.max_seconds,
            LONGFORM_MIN_SECONDS,
            LONGFORM_MAX_SECONDS,
        )
        if self.min_seconds > self.max_seconds:
            raise ValueError("min_seconds must not be greater than max_seconds")
        if not isinstance(self.resolution, Resolution):
            raise TypeError("resolution must be a Resolution")
        if self.aspect_ratio is not AspectRatio.HORIZONTAL:
            raise ValueError("a LongForm canvas is 16:9")
        for name in ("width", "height"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be a whole number")
        if (self.width, self.height) != HORIZONTAL_SIZES[self.resolution]:
            width, height = HORIZONTAL_SIZES[self.resolution]
            raise ValueError(
                "a LongForm canvas is 16:9 "
                f"({width}x{height} for {self.resolution.value})"
            )
        _whole("fps", self.fps, 1, MAX_FPS)
        if not isinstance(self.captions, bool):
            raise TypeError("captions must be true or false")
        if not isinstance(self.chapters, bool):
            raise TypeError("chapters must be true or false")
        if not isinstance(self.safe_area, SafeArea):
            raise TypeError("safe_area must be a SafeArea")
        if not isinstance(self.caption_rules, CaptionRules):
            raise TypeError("caption_rules must be CaptionRules")

    @classmethod
    def from_format(cls, longform: LongFormFormat) -> "LongFormConfig":
        """The configuration of a project made under this strategy format."""
        if not isinstance(longform, LongFormFormat):
            raise TypeError("longform must be a LongFormFormat")
        width, height = HORIZONTAL_SIZES[longform.resolution]
        return cls(
            min_seconds=longform.min_seconds,
            max_seconds=longform.max_seconds,
            resolution=longform.resolution,
            width=width,
            height=height,
            fps=FPS,
            captions=longform.captions,
            chapters=longform.chapters,
            aspect_ratio=longform.aspect_ratio,
            safe_area=DEFAULT_LONGFORM_SAFE_AREA,
            caption_rules=DEFAULT_LONGFORM_CAPTION_RULES,
        )

    @property
    def canvas_size(self) -> tuple[int, int]:
        """The (width, height) of this canvas in pixels."""
        return (self.width, self.height)

    @property
    def safe_rect(self) -> Rect:
        """The safe area of this canvas in pixels."""
        return self.safe_area.rect(self.width, self.height)

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_version": LONGFORM_CONFIG_VERSION,
            "aspect_ratio": self.aspect_ratio.value,
            "resolution": self.resolution.value,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "min_seconds": self.min_seconds,
            "max_seconds": self.max_seconds,
            "captions": self.captions,
            "chapters": self.chapters,
            "safe_area": self.safe_area.to_dict(),
            "caption_rules": self.caption_rules.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: object) -> "LongFormConfig":
        """Decode a stored configuration; ValueError when it is not valid."""
        values = _keys("format", data, CONFIG_KEYS, REQUIRED_CONFIG_KEYS)
        version = values.get("config_version", LONGFORM_CONFIG_VERSION)
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version != LONGFORM_CONFIG_VERSION
        ):
            raise ValueError(
                f"format config_version {version!r} is not supported, this "
                f"program reads version {LONGFORM_CONFIG_VERSION}"
            )
        resolution = _enum(
            Resolution,
            values.get("resolution", Resolution.FULL_HD_1080.value),
            "resolution",
        )
        default_width, default_height = HORIZONTAL_SIZES[resolution]
        safe_area_raw = values.get("safe_area")
        caption_rules_raw = values.get("caption_rules")
        return cls(
            min_seconds=values["min_seconds"],
            max_seconds=values["max_seconds"],
            resolution=resolution,
            width=values.get("width", default_width),
            height=values.get("height", default_height),
            fps=values.get("fps", FPS),
            captions=values.get("captions", True),
            chapters=values.get("chapters", True),
            aspect_ratio=_enum(
                AspectRatio,
                values.get("aspect_ratio", AspectRatio.HORIZONTAL.value),
                "aspect_ratio",
            ),
            safe_area=(
                SafeArea.from_dict(safe_area_raw)
                if safe_area_raw is not None
                else DEFAULT_LONGFORM_SAFE_AREA
            ),
            caption_rules=(
                CaptionRules.from_dict(caption_rules_raw)
                if caption_rules_raw is not None
                else DEFAULT_LONGFORM_CAPTION_RULES
            ),
        )
