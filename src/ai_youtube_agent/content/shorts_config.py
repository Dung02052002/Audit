"""Shorts configuration (Prompt Pack v8, prompt #094), context C8 Shorts Production.

``ShortsConfig`` is the production setup of one Shorts project, copied from the
channel strategy's ``ShortsFormat`` (#049) when the project is made, so a later
strategy change never rewrites what a project was made for. It is stored as the
``format_json`` column of ``shorts_projects`` (migration 0027):

- the vertical canvas: ``aspect_ratio`` (always 9:16), ``resolution`` and the
  matching ``width`` x ``height`` (``VERTICAL_SIZES``), and the frame rate;
- ``min_seconds`` and ``max_seconds`` of the Shorts format, and ``captions``;
- ``SafeArea``: the part of the canvas the YouTube Shorts interface leaves free,
  in permille of the canvas (top 130, bottom 250, left 60 and right 120 by
  default). ``rect(width, height)`` gives it in pixels; at 1080x1920 it spans
  x 65 to 950 and y 250 to 1440;
- ``CaptionRules``: at most 2 lines of 32 characters, shown for 700 to 6000 ms
  by default. I-097 reads them to wrap and time the captions.

``to_dict`` and ``from_dict`` are strict: a key that is not known is an error
and a missing optional key takes its default. The ``config_version`` key
(``SHORTS_CONFIG_VERSION``) is 1; a higher version is refused, so an older
program never reads a newer layout half way.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any

from ai_youtube_agent.content.strategy import (
    SHORTS_MAX_SECONDS,
    SHORTS_MIN_SECONDS,
    AspectRatio,
    Resolution,
    ShortsFormat,
)

SHORTS_CONFIG_VERSION = 1
FPS = 30
MAX_FPS = 120
PERMILLE = 1000
VERTICAL_SIZES: Mapping[Resolution, tuple[int, int]] = MappingProxyType(
    {
        Resolution.HD_720: (720, 1280),
        Resolution.FULL_HD_1080: (1080, 1920),
        Resolution.UHD_2160: (2160, 3840),
    }
)
# Caption rule limits (I-094 default decision D3, 2026-10-17).
MAX_CAPTION_LINES = 4
MIN_CHARS_PER_LINE = 10
MAX_CHARS_PER_LINE = 60
MIN_DISPLAY_FLOOR_MS = 100
MAX_DISPLAY_CEILING_MS = 30_000


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
    """``data`` as a mapping, or ValueError when a key is unknown or missing."""
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


def _scaled(size: int, permille: int) -> int:
    """``size * permille / 1000`` in whole pixels, halves rounded up."""
    return (size * permille + PERMILLE // 2) // PERMILLE


@dataclass(frozen=True)
class Rect:
    """A rectangle in whole pixels: left and top corner, width and height."""

    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        _whole("x", self.x, 0, 1_000_000)
        _whole("y", self.y, 0, 1_000_000)
        _whole("width", self.width, 1, 1_000_000)
        _whole("height", self.height, 1, 1_000_000)

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def contains(self, other: "Rect") -> bool:
        """True when ``other`` lies fully inside this rectangle (edges touch)."""
        return (
            self.x <= other.x
            and self.y <= other.y
            and other.right <= self.right
            and other.bottom <= self.bottom
        )


SAFE_AREA_KEYS = frozenset({"top", "bottom", "left", "right"})


@dataclass(frozen=True)
class SafeArea:
    """The margins the Shorts interface covers, in permille of the canvas."""

    top: int = 130
    bottom: int = 250
    left: int = 60
    right: int = 120

    def __post_init__(self) -> None:
        for name in ("top", "bottom", "left", "right"):
            _whole(f"safe area {name}", getattr(self, name), 0, PERMILLE - 1)
        if self.top + self.bottom >= PERMILLE:
            raise ValueError("safe area top and bottom must leave some height")
        if self.left + self.right >= PERMILLE:
            raise ValueError("safe area left and right must leave some width")

    def rect(self, width: int, height: int) -> Rect:
        """The free part of a ``width`` x ``height`` canvas, in whole pixels."""
        left = _scaled(width, self.left)
        right = _scaled(width, self.right)
        top = _scaled(height, self.top)
        bottom = _scaled(height, self.bottom)
        return Rect(left, top, width - left - right, height - top - bottom)

    def to_dict(self) -> dict[str, int]:
        return {
            "top": self.top,
            "bottom": self.bottom,
            "left": self.left,
            "right": self.right,
        }

    @classmethod
    def from_dict(cls, data: object) -> "SafeArea":
        values = _keys("safe_area", data, SAFE_AREA_KEYS)
        return cls(**values)


CAPTION_RULE_KEYS = frozenset(
    {"max_lines", "max_chars_per_line", "min_display_ms", "max_display_ms"}
)


@dataclass(frozen=True)
class CaptionRules:
    """How a caption is wrapped and how long it stays on screen (I-097)."""

    max_lines: int = 2
    max_chars_per_line: int = 32
    min_display_ms: int = 700
    max_display_ms: int = 6000

    def __post_init__(self) -> None:
        _whole("caption max_lines", self.max_lines, 1, MAX_CAPTION_LINES)
        _whole(
            "caption max_chars_per_line",
            self.max_chars_per_line,
            MIN_CHARS_PER_LINE,
            MAX_CHARS_PER_LINE,
        )
        _whole(
            "caption min_display_ms",
            self.min_display_ms,
            MIN_DISPLAY_FLOOR_MS,
            MAX_DISPLAY_CEILING_MS,
        )
        _whole(
            "caption max_display_ms",
            self.max_display_ms,
            MIN_DISPLAY_FLOOR_MS,
            MAX_DISPLAY_CEILING_MS,
        )
        if self.min_display_ms > self.max_display_ms:
            raise ValueError("caption min_display_ms must not exceed max_display_ms")

    def to_dict(self) -> dict[str, int]:
        return {
            "max_lines": self.max_lines,
            "max_chars_per_line": self.max_chars_per_line,
            "min_display_ms": self.min_display_ms,
            "max_display_ms": self.max_display_ms,
        }

    @classmethod
    def from_dict(cls, data: object) -> "CaptionRules":
        values = _keys("caption_rules", data, CAPTION_RULE_KEYS)
        return cls(**values)


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
        "safe_area",
        "caption_rules",
    }
)
REQUIRED_CONFIG_KEYS = frozenset({"min_seconds", "max_seconds"})


@dataclass(frozen=True)
class ShortsConfig:
    min_seconds: int
    max_seconds: int
    resolution: Resolution
    width: int
    height: int
    fps: int
    captions: bool
    aspect_ratio: AspectRatio
    safe_area: SafeArea
    caption_rules: CaptionRules

    def __post_init__(self) -> None:
        _whole("min_seconds", self.min_seconds, SHORTS_MIN_SECONDS, SHORTS_MAX_SECONDS)
        _whole("max_seconds", self.max_seconds, SHORTS_MIN_SECONDS, SHORTS_MAX_SECONDS)
        if self.min_seconds > self.max_seconds:
            raise ValueError("min_seconds must not be greater than max_seconds")
        if not isinstance(self.resolution, Resolution):
            raise TypeError("resolution must be a Resolution")
        if self.aspect_ratio is not AspectRatio.VERTICAL:
            raise ValueError("a Shorts canvas is 9:16")
        for name in ("width", "height"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be a whole number")
        if (self.width, self.height) != VERTICAL_SIZES[self.resolution]:
            width, height = VERTICAL_SIZES[self.resolution]
            raise ValueError(
                f"a {self.resolution.value} Shorts canvas is {width}x{height}"
            )
        _whole("fps", self.fps, 1, MAX_FPS)
        if not isinstance(self.captions, bool):
            raise TypeError("captions must be true or false")
        if not isinstance(self.safe_area, SafeArea):
            raise TypeError("safe_area must be a SafeArea")
        if not isinstance(self.caption_rules, CaptionRules):
            raise TypeError("caption_rules must be CaptionRules")

    @classmethod
    def from_format(cls, shorts: ShortsFormat) -> "ShortsConfig":
        """The configuration of a project made under this strategy format."""
        width, height = VERTICAL_SIZES[shorts.resolution]
        return cls(
            min_seconds=shorts.min_seconds,
            max_seconds=shorts.max_seconds,
            resolution=shorts.resolution,
            width=width,
            height=height,
            fps=FPS,
            captions=shorts.captions,
            aspect_ratio=shorts.aspect_ratio,
            safe_area=SafeArea(),
            caption_rules=CaptionRules(),
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
            "config_version": SHORTS_CONFIG_VERSION,
            "aspect_ratio": self.aspect_ratio.value,
            "resolution": self.resolution.value,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "min_seconds": self.min_seconds,
            "max_seconds": self.max_seconds,
            "captions": self.captions,
            "safe_area": self.safe_area.to_dict(),
            "caption_rules": self.caption_rules.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: object) -> "ShortsConfig":
        """Decode a stored configuration; ValueError when it is not valid."""
        values = _keys("format", data, CONFIG_KEYS, REQUIRED_CONFIG_KEYS)
        version = values.get("config_version", SHORTS_CONFIG_VERSION)
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version != SHORTS_CONFIG_VERSION
        ):
            raise ValueError(
                f"format config_version {version!r} is not supported, this "
                f"program reads version {SHORTS_CONFIG_VERSION}"
            )
        resolution = _enum(
            Resolution,
            values.get("resolution", Resolution.FULL_HD_1080.value),
            "resolution",
        )
        default_width, default_height = VERTICAL_SIZES[resolution]
        return cls(
            min_seconds=values["min_seconds"],
            max_seconds=values["max_seconds"],
            resolution=resolution,
            width=values.get("width", default_width),
            height=values.get("height", default_height),
            fps=values.get("fps", FPS),
            captions=values.get("captions", True),
            aspect_ratio=_enum(
                AspectRatio,
                values.get("aspect_ratio", AspectRatio.VERTICAL.value),
                "aspect_ratio",
            ),
            safe_area=SafeArea.from_dict(values.get("safe_area", {})),
            caption_rules=CaptionRules.from_dict(values.get("caption_rules", {})),
        )
