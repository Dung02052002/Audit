"""Shorts timeline (Prompt Pack v8, prompt #094), context C8 Shorts Production.

``Timeline`` is the plan of what a Shorts video shows and when. It is stored as
the ``timeline_json`` column of ``shorts_projects`` (migration 0027) and its
canonical JSON (``to_json``) and checksum (``sha256``) are what the later steps
(subtitles #098, audio mix #099, render #100, bundle #103) bind to. All times
are whole milliseconds and the voice is the master clock: a timeline is always
as long as the audio (``duration_ms``).

Layout of schema version 1::

    {"schema_version": 1, "duration_ms": 30250,
     "timing_source": "audio_chunks" | "estimated",
     "scenes": [], "captions": [], "music": []}

``captions`` and ``music`` may be left out when decoding and then default to
empty. A key that is not known is an error and so is a schema version that this
program does not know (a higher one, or any other): ``from_dict`` dispatches on
the version through ``_DECODERS``. A change that only adds an optional key keeps
version 1; a breaking change adds version 2 and an upgrader for version 1.

Scenes (I-095). A ``Scene`` is a stretch of the audio that shows one script
section: ``{"id": "s1", "section_index": 0, "start_ms": 0, "end_ms": 3200,
"title": null, "assets": []}``. Scenes are ordered, their ids are ``"s"`` and the
1-based position, the first starts at 0, each starts where the one before ended,
the last ends at ``duration_ms``, each lasts at least ``MIN_SCENE_MS``, there are
at most ``MAX_SCENES`` and ``section_index`` never goes back. That every script
section appears is checked by the service against the script. A timeline
without scenes is the empty one of a draft project.

Assets (I-096). A ``SceneAsset`` places one registered asset in a scene and an
``AssetPlacement`` places a music asset in the whole timeline::

    {"asset_id": "...", "role": "background" | "b_roll" | "overlay" | "sfx",
     "start_ms": 0, "end_ms": null, "source_in_ms": null, "source_out_ms": null,
     "fit": "cover" | "contain", "box": null | {"x", "y", "width", "height"},
     "gain_db": null, "fade_in_ms": 0, "fade_out_ms": 0, "provenance_id": null}

Times of a scene asset are relative to its scene, those of music are absolute;
``end_ms`` null means the end of the scene (or of the timeline). ``0 <= start <
end <= span``, ``source_in_ms < source_out_ms`` when both are set, the fades
are not negative and together fit the placement, ``gain_db`` is from -60 to
+12. ``gain_db`` and the fades belong to audio roles (sfx, music) and ``box``
(permille of the canvas) to visual roles (background, b_roll, overlay). A scene
has at most ``MAX_SCENE_ASSETS`` assets and one background; there are at most
``MAX_MUSIC`` music placements. ``ROLE_KINDS`` maps a role to the ``AssetKind``
values it accepts; the service checks that, the ownership and the rights
category, and fills ``provenance_id`` (this module only validates the shape).
Decoding is strict (unknown keys are errors, optional keys default).

Captions (I-097). A ``CaptionOverlay`` is a text shown over the video::

    {"id": "c1", "scene_id": "s1", "text": "...", "start_ms": 0, "end_ms": 2400,
     "position": {"anchor": "bottom", "offset_px": 0},
     "style": {"font_asset_id": null, "size_px": 64, "color": "#FFFFFF",
               "stroke_color": "#000000", "stroke_px": 4, "align": "center",
               "background": null}}

The text is NFC, trimmed and not empty. Times are absolute. A timeline keeps
its captions ordered, without overlap (touching is fine), each inside its scene
(so there are no captions without scenes), with the ids ``"c"`` and the 1-based
position, at most ``MAX_CAPTIONS``. ``position`` and ``style`` may be left out
when decoding. Colours are ``#RRGGBB`` in capitals (the background may also be
``#RRGGBBAA``); ``offset_px`` moves the box down when positive. The rules that
need the configuration (display time, wrapping, the safe area, the format having
captions at all) are in ``content/shorts_captions.py``.
"""

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ai_youtube_agent.content.asset import AssetKind

TIMELINE_SCHEMA_VERSION = 1
MAX_SCENE_ASSETS = 20
MAX_MUSIC = 10
MIN_GAIN_DB = -60.0
MAX_GAIN_DB = 12.0
MAX_ID = 200
MAX_DURATION_MS = 4 * 60 * 60 * 1000
MIN_SCENE_MS = 500
MAX_SCENES = 60
MAX_SECTION_INDEX = 1000
MAX_TITLE = 200
MAX_CAPTIONS = 1000
MAX_CAPTION_TEXT = 500
MIN_FONT_SIZE = 16
MAX_FONT_SIZE = 400
MAX_STROKE_PX = 40
MAX_OFFSET_PX = 4000


class TimingSource(StrEnum):
    """Where the scene boundaries come from (I-095)."""

    AUDIO_CHUNKS = "audio_chunks"
    ESTIMATED = "estimated"


def _whole(name: str, value: object, low: int, high: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise ValueError(f"{name} must be a whole number from {low} to {high}")


class AssetRole(StrEnum):
    BACKGROUND = "background"
    B_ROLL = "b_roll"
    OVERLAY = "overlay"
    SFX = "sfx"
    MUSIC = "music"


class AssetFit(StrEnum):
    COVER = "cover"
    CONTAIN = "contain"


VISUAL_ROLES = frozenset({AssetRole.BACKGROUND, AssetRole.B_ROLL, AssetRole.OVERLAY})
AUDIO_ROLES = frozenset({AssetRole.SFX, AssetRole.MUSIC})
ROLE_KINDS: dict[AssetRole, frozenset[AssetKind]] = {
    AssetRole.BACKGROUND: frozenset({AssetKind.IMAGE, AssetKind.VIDEO_CLIP}),
    AssetRole.B_ROLL: frozenset({AssetKind.IMAGE, AssetKind.VIDEO_CLIP}),
    AssetRole.OVERLAY: frozenset(
        {AssetKind.IMAGE, AssetKind.VIDEO_CLIP, AssetKind.TEMPLATE}
    ),
    AssetRole.MUSIC: frozenset({AssetKind.MUSIC}),
    AssetRole.SFX: frozenset({AssetKind.AUDIO}),
}

BOX_KEYS = frozenset({"x", "y", "width", "height"})
PLACEMENT_KEYS = frozenset(
    {
        "asset_id",
        "role",
        "start_ms",
        "end_ms",
        "source_in_ms",
        "source_out_ms",
        "fit",
        "box",
        "gain_db",
        "fade_in_ms",
        "fade_out_ms",
        "provenance_id",
    }
)


def _trimmed_id(name: str, value: object) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > MAX_ID
    ):
        raise ValueError(f"{name} must be a trimmed text of 1-{MAX_ID} characters")


@dataclass(frozen=True)
class Box:
    """A rectangle in permille of the canvas (1000 = the full width or height)."""

    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        _whole("x", self.x, 0, 1000)
        _whole("y", self.y, 0, 1000)
        _whole("width", self.width, 1, 1000)
        _whole("height", self.height, 1, 1000)
        if self.x + self.width > 1000 or self.y + self.height > 1000:
            raise ValueError("a box must stay inside the canvas (1000 permille)")

    def to_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}

    @classmethod
    def from_dict(cls, data: object) -> "Box":
        if not isinstance(data, Mapping):
            raise ValueError("a box must be an object")
        if set(data) != BOX_KEYS:
            raise ValueError(
                f"a box has exactly the keys {', '.join(sorted(BOX_KEYS))}"
            )
        return cls(data["x"], data["y"], data["width"], data["height"])


@dataclass(frozen=True)
class _Placement:
    """The shape shared by ``SceneAsset`` and ``AssetPlacement``."""

    asset_id: str
    role: AssetRole
    start_ms: int = 0
    end_ms: int | None = None
    source_in_ms: int | None = None
    source_out_ms: int | None = None
    fit: AssetFit = AssetFit.COVER
    box: Box | None = None
    gain_db: int | float | None = None
    fade_in_ms: int = 0
    fade_out_ms: int = 0
    provenance_id: str | None = None

    _roles = frozenset(AssetRole)
    _required = frozenset({"asset_id", "role"})

    def __post_init__(self) -> None:
        _trimmed_id("asset_id", self.asset_id)
        if self.provenance_id is not None:
            _trimmed_id("provenance_id", self.provenance_id)
        if not isinstance(self.role, AssetRole) or self.role not in self._roles:
            allowed = ", ".join(sorted(role.value for role in self._roles))
            raise ValueError(f"the role must be one of: {allowed}")
        if not isinstance(self.fit, AssetFit):
            raise TypeError("fit must be an AssetFit")
        _whole("start_ms", self.start_ms, 0, MAX_DURATION_MS)
        if self.end_ms is not None:
            _whole("end_ms", self.end_ms, 1, MAX_DURATION_MS)
            if self.end_ms <= self.start_ms:
                raise ValueError("end_ms must be after start_ms")
        for name in ("source_in_ms", "source_out_ms"):
            if getattr(self, name) is not None:
                _whole(name, getattr(self, name), 0, MAX_DURATION_MS)
        if (
            self.source_in_ms is not None
            and self.source_out_ms is not None
            and self.source_in_ms >= self.source_out_ms
        ):
            raise ValueError("source_in_ms must be before source_out_ms")
        _whole("fade_in_ms", self.fade_in_ms, 0, MAX_DURATION_MS)
        _whole("fade_out_ms", self.fade_out_ms, 0, MAX_DURATION_MS)
        if self.gain_db is not None and (
            isinstance(self.gain_db, bool)
            or not isinstance(self.gain_db, int | float)
            or not MIN_GAIN_DB <= self.gain_db <= MAX_GAIN_DB
        ):
            raise ValueError(
                f"gain_db must be a number from {MIN_GAIN_DB:g} to {MAX_GAIN_DB:g}"
            )
        if self.box is not None and not isinstance(self.box, Box):
            raise TypeError("box must be a Box")
        if self.role in AUDIO_ROLES:
            if self.box is not None:
                raise ValueError(f"a {self.role.value} asset has no box")
        elif self.gain_db is not None or self.fade_in_ms or self.fade_out_ms:
            raise ValueError(f"gain and fades are for audio, not for {self.role.value}")

    def check_within(self, limit_ms: int, what: str) -> None:
        """The timing rules against the length of the scene or timeline."""
        end = limit_ms if self.end_ms is None else self.end_ms
        if end > limit_ms or self.start_ms >= end:
            raise ValueError(
                f"{self.role.value} asset {self.asset_id} must lie within the "
                f"{what} ({limit_ms} ms)"
            )
        if self.fade_in_ms + self.fade_out_ms > end - self.start_ms:
            raise ValueError(
                f"the fades of asset {self.asset_id} are longer than its "
                f"{end - self.start_ms} ms"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "role": self.role.value,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "source_in_ms": self.source_in_ms,
            "source_out_ms": self.source_out_ms,
            "fit": self.fit.value,
            "box": self.box.to_dict() if self.box is not None else None,
            "gain_db": self.gain_db,
            "fade_in_ms": self.fade_in_ms,
            "fade_out_ms": self.fade_out_ms,
            "provenance_id": self.provenance_id,
        }

    @classmethod
    def from_dict(cls, data: object):
        label = cls.__name__
        if not isinstance(data, Mapping):
            raise ValueError(f"{label} must be an object")
        unknown = sorted(str(key) for key in set(data) - PLACEMENT_KEYS)
        if unknown:
            raise ValueError(f"{label} has unknown keys: {', '.join(unknown)}")
        missing = sorted(cls._required - set(data))
        if missing:
            raise ValueError(f"{label} is missing keys: {', '.join(missing)}")
        values = dict(data)
        if "role" in values:
            try:
                values["role"] = AssetRole(values["role"])
            except (ValueError, TypeError):
                raise ValueError(f"role {values['role']!r} is not valid") from None
        if "fit" in values:
            try:
                values["fit"] = AssetFit(values["fit"])
            except (ValueError, TypeError):
                raise ValueError(f"fit {values['fit']!r} is not valid") from None
        if values.get("box") is not None:
            values["box"] = Box.from_dict(values["box"])
        return cls(**values)


@dataclass(frozen=True)
class SceneAsset(_Placement):
    """An asset shown or heard inside one scene (times relative to the scene)."""

    _roles = frozenset(
        {AssetRole.BACKGROUND, AssetRole.B_ROLL, AssetRole.OVERLAY, AssetRole.SFX}
    )


@dataclass(frozen=True)
class AssetPlacement(_Placement):
    """A music asset on the whole timeline (absolute times)."""

    role: AssetRole = AssetRole.MUSIC

    _roles = frozenset({AssetRole.MUSIC})
    _required = frozenset({"asset_id"})


SCENE_KEYS = frozenset({"id", "section_index", "start_ms", "end_ms", "title", "assets"})
SCENE_REQUIRED_KEYS = frozenset({"id", "section_index", "start_ms", "end_ms"})


@dataclass(frozen=True)
class Scene:
    id: str
    section_index: int
    start_ms: int
    end_ms: int
    title: str | None = None
    assets: tuple[SceneAsset, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("a scene id must not be empty")
        _whole("section_index", self.section_index, 0, MAX_SECTION_INDEX)
        _whole("start_ms", self.start_ms, 0, MAX_DURATION_MS)
        _whole("end_ms", self.end_ms, 1, MAX_DURATION_MS)
        if self.end_ms - self.start_ms < MIN_SCENE_MS:
            raise ValueError(f"scene {self.id} must last at least {MIN_SCENE_MS} ms")
        if self.title is not None and (
            not isinstance(self.title, str)
            or not self.title.strip()
            or self.title != self.title.strip()
            or len(self.title) > MAX_TITLE
        ):
            raise ValueError(
                f"a scene title is trimmed and at most {MAX_TITLE} characters"
            )
        if not isinstance(self.assets, tuple):
            raise TypeError("assets must be a tuple")
        if len(self.assets) > MAX_SCENE_ASSETS:
            raise ValueError(f"a scene has at most {MAX_SCENE_ASSETS} assets")
        backgrounds = 0
        for asset in self.assets:
            if not isinstance(asset, SceneAsset):
                raise TypeError("assets must be SceneAsset values")
            asset.check_within(self.duration_ms, f"scene {self.id}")
            backgrounds += asset.role is AssetRole.BACKGROUND
        if backgrounds > 1:
            raise ValueError(f"scene {self.id} has more than one background")

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "section_index": self.section_index,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "title": self.title,
            "assets": [asset.to_dict() for asset in self.assets],
        }

    @classmethod
    def from_dict(cls, data: object) -> "Scene":
        if not isinstance(data, Mapping):
            raise ValueError("a scene must be an object")
        unknown = sorted(str(key) for key in set(data) - SCENE_KEYS)
        if unknown:
            raise ValueError(f"scene has unknown keys: {', '.join(unknown)}")
        missing = sorted(SCENE_REQUIRED_KEYS - set(data))
        if missing:
            raise ValueError(f"scene is missing keys: {', '.join(missing)}")
        assets = data.get("assets", [])
        if not isinstance(assets, list):
            raise ValueError("assets must be a list")
        return cls(
            data["id"],
            data["section_index"],
            data["start_ms"],
            data["end_ms"],
            data.get("title"),
            tuple(SceneAsset.from_dict(item) for item in assets),
        )


class CaptionAnchor(StrEnum):
    """Where a caption box sits in the safe area (before ``offset_px``)."""

    TOP = "top"
    CENTER = "center"
    BOTTOM = "bottom"


class CaptionAlign(StrEnum):
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"


_RGB = re.compile(r"#[0-9A-F]{6}")
_RGB_OR_RGBA = re.compile(r"#[0-9A-F]{6}(?:[0-9A-F]{2})?")
_CAPTION_ID = re.compile(r"c[1-9][0-9]*")


def _enum_value(kind: type[StrEnum], value: object, name: str) -> Any:
    try:
        return kind(value)
    except (ValueError, TypeError):
        raise ValueError(f"{name} {value!r} is not valid") from None


def _strict_keys(
    name: str, data: object, allowed: frozenset[str], required: frozenset[str]
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


def _colour(name: str, value: object, pattern: re.Pattern[str], shape: str) -> None:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{name} must be {shape} with capital hex digits")


POSITION_KEYS = frozenset({"anchor", "offset_px"})


@dataclass(frozen=True)
class CaptionPosition:
    """The anchor in the safe area and a vertical offset (positive = down)."""

    anchor: CaptionAnchor = CaptionAnchor.BOTTOM
    offset_px: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.anchor, CaptionAnchor):
            raise TypeError("anchor must be a CaptionAnchor")
        _whole("offset_px", self.offset_px, -MAX_OFFSET_PX, MAX_OFFSET_PX)

    def to_dict(self) -> dict[str, Any]:
        return {"anchor": self.anchor.value, "offset_px": self.offset_px}

    @classmethod
    def from_dict(cls, data: object) -> "CaptionPosition":
        values = dict(_strict_keys("position", data, POSITION_KEYS, frozenset()))
        if "anchor" in values:
            values["anchor"] = _enum_value(CaptionAnchor, values["anchor"], "anchor")
        return cls(**values)


STYLE_KEYS = frozenset(
    {
        "font_asset_id",
        "size_px",
        "color",
        "stroke_color",
        "stroke_px",
        "align",
        "background",
    }
)


@dataclass(frozen=True)
class CaptionStyle:
    font_asset_id: str | None = None
    size_px: int = 64
    color: str = "#FFFFFF"
    stroke_color: str = "#000000"
    stroke_px: int = 4
    align: CaptionAlign = CaptionAlign.CENTER
    background: str | None = None

    def __post_init__(self) -> None:
        if self.font_asset_id is not None:
            _trimmed_id("font_asset_id", self.font_asset_id)
        _whole("size_px", self.size_px, MIN_FONT_SIZE, MAX_FONT_SIZE)
        _colour("color", self.color, _RGB, "#RRGGBB")
        _colour("stroke_color", self.stroke_color, _RGB, "#RRGGBB")
        _whole("stroke_px", self.stroke_px, 0, MAX_STROKE_PX)
        if not isinstance(self.align, CaptionAlign):
            raise TypeError("align must be a CaptionAlign")
        if self.background is not None:
            _colour("background", self.background, _RGB_OR_RGBA, "#RRGGBB or #RRGGBBAA")

    def to_dict(self) -> dict[str, Any]:
        return {
            "font_asset_id": self.font_asset_id,
            "size_px": self.size_px,
            "color": self.color,
            "stroke_color": self.stroke_color,
            "stroke_px": self.stroke_px,
            "align": self.align.value,
            "background": self.background,
        }

    @classmethod
    def from_dict(cls, data: object) -> "CaptionStyle":
        values = dict(_strict_keys("style", data, STYLE_KEYS, frozenset()))
        if "align" in values:
            values["align"] = _enum_value(CaptionAlign, values["align"], "align")
        return cls(**values)


CAPTION_KEYS = frozenset(
    {"id", "scene_id", "text", "start_ms", "end_ms", "position", "style"}
)
CAPTION_REQUIRED_KEYS = frozenset({"id", "scene_id", "text", "start_ms", "end_ms"})


@dataclass(frozen=True)
class CaptionOverlay:
    id: str
    scene_id: str
    text: str
    start_ms: int
    end_ms: int
    position: CaptionPosition = CaptionPosition()
    style: CaptionStyle = CaptionStyle()

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _CAPTION_ID.fullmatch(self.id):
            raise ValueError("a caption id is c and a position, like c1")
        _trimmed_id("scene_id", self.scene_id)
        if (
            not isinstance(self.text, str)
            or not self.text.strip()
            or self.text != self.text.strip()
        ):
            raise ValueError("caption text must be trimmed and not empty")
        if self.text != unicodedata.normalize("NFC", self.text):
            raise ValueError("caption text must be NFC normalised")
        if len(self.text) > MAX_CAPTION_TEXT:
            raise ValueError(f"caption text has at most {MAX_CAPTION_TEXT} characters")
        _whole("start_ms", self.start_ms, 0, MAX_DURATION_MS)
        _whole("end_ms", self.end_ms, 1, MAX_DURATION_MS)
        if self.end_ms <= self.start_ms:
            raise ValueError(f"caption {self.id} must end after it starts")
        if not isinstance(self.position, CaptionPosition):
            raise TypeError("position must be a CaptionPosition")
        if not isinstance(self.style, CaptionStyle):
            raise TypeError("style must be a CaptionStyle")

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scene_id": self.scene_id,
            "text": self.text,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "position": self.position.to_dict(),
            "style": self.style.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: object) -> "CaptionOverlay":
        values = dict(
            _strict_keys("caption", data, CAPTION_KEYS, CAPTION_REQUIRED_KEYS)
        )
        if "position" in values:
            values["position"] = CaptionPosition.from_dict(values["position"])
        if "style" in values:
            values["style"] = CaptionStyle.from_dict(values["style"])
        return cls(**values)


def _check_captions(
    captions: tuple[Any, ...], scenes: tuple[Any, ...], duration_ms: int
) -> None:
    """The shape rules of a non-empty list of captions against the scenes."""
    if len(captions) > MAX_CAPTIONS:
        raise ValueError(f"a timeline has at most {MAX_CAPTIONS} captions")
    if not scenes:
        raise ValueError("captions need scenes: plan the scenes first")
    by_id = {scene.id: scene for scene in scenes}
    previous_end = 0
    for position, caption in enumerate(captions, start=1):
        if not isinstance(caption, CaptionOverlay):
            raise TypeError("captions must be CaptionOverlay values")
        if caption.id != f"c{position}":
            raise ValueError(f"caption {position} must have the id c{position}")
        scene = by_id.get(caption.scene_id)
        if scene is None:
            raise ValueError(
                f"caption {caption.id} names unknown scene {caption.scene_id}"
            )
        if caption.end_ms > duration_ms:
            raise ValueError(
                f"caption {caption.id} ends at {caption.end_ms} ms, after the audio "
                f"({duration_ms} ms)"
            )
        if caption.start_ms < previous_end:
            raise ValueError(
                f"caption {caption.id} starts before the previous caption ends"
            )
        if caption.start_ms < scene.start_ms or caption.end_ms > scene.end_ms:
            raise ValueError(f"caption {caption.id} must lie within scene {scene.id}")
        previous_end = caption.end_ms


def _check_scenes(scenes: tuple[Any, ...], duration_ms: int) -> None:
    """The ordering and coverage rules of a non-empty list of scenes."""
    if len(scenes) > MAX_SCENES:
        raise ValueError(f"a timeline has at most {MAX_SCENES} scenes")
    expected_start = 0
    section_index = 0
    for position, scene in enumerate(scenes, start=1):
        if not isinstance(scene, Scene):
            raise TypeError("scenes must be Scene values")
        if scene.id != f"s{position}":
            raise ValueError(f"scene {position} must have the id s{position}")
        if scene.start_ms != expected_start:
            raise ValueError(
                f"scene {scene.id} must start at {expected_start} ms, where the "
                "previous scene ends"
            )
        if scene.section_index < section_index:
            raise ValueError(f"scene {scene.id} goes back to an earlier section")
        expected_start = scene.end_ms
        section_index = scene.section_index
    if expected_start != duration_ms:
        raise ValueError(
            f"the scenes end at {expected_start} ms but the audio is {duration_ms} ms"
        )


@dataclass(frozen=True)
class Timeline:
    schema_version: int
    duration_ms: int
    timing_source: TimingSource
    scenes: tuple[Any, ...] = ()
    captions: tuple["CaptionOverlay", ...] = ()
    music: tuple[AssetPlacement, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != TIMELINE_SCHEMA_VERSION or isinstance(
            self.schema_version, bool
        ):
            raise ValueError(
                f"schema_version must be {TIMELINE_SCHEMA_VERSION} "
                "(the only version this program writes)"
            )
        _whole("duration_ms", self.duration_ms, 1, MAX_DURATION_MS)
        if not isinstance(self.timing_source, TimingSource):
            raise TypeError("timing_source must be a TimingSource")
        for name in ("scenes", "captions", "music"):
            if not isinstance(getattr(self, name), tuple):
                raise TypeError(f"{name} must be a tuple")
        if self.scenes:
            _check_scenes(self.scenes, self.duration_ms)
        if self.captions:
            _check_captions(self.captions, self.scenes, self.duration_ms)
        if len(self.music) > MAX_MUSIC:
            raise ValueError(f"a timeline has at most {MAX_MUSIC} music placements")
        for placement in self.music:
            if not isinstance(placement, AssetPlacement):
                raise TypeError("music must be AssetPlacement values")
            placement.check_within(self.duration_ms, "timeline")

    @classmethod
    def empty(cls, duration_ms: int) -> "Timeline":
        """A timeline of ``duration_ms`` with nothing planned yet."""
        return cls(TIMELINE_SCHEMA_VERSION, duration_ms, TimingSource.ESTIMATED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "duration_ms": self.duration_ms,
            "timing_source": self.timing_source.value,
            "scenes": [scene.to_dict() for scene in self.scenes],
            "captions": [caption.to_dict() for caption in self.captions],
            "music": [placement.to_dict() for placement in self.music],
        }

    def to_json(self) -> str:
        """The canonical JSON: sorted keys, compact separators, no ASCII escapes."""
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    @property
    def sha256(self) -> str:
        """The checksum of the canonical JSON, the binding of later steps."""
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_json(cls, text: str) -> "Timeline":
        """Decode canonical JSON text; ValueError when it is not a valid timeline."""
        return cls.from_dict(json.loads(text))

    @classmethod
    def from_dict(cls, data: object) -> "Timeline":
        """Decode a stored timeline; ValueError when it is not valid."""
        if not isinstance(data, Mapping):
            raise ValueError("a timeline must be an object")
        version = data.get("schema_version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValueError("a timeline needs a whole schema_version")
        decoder = _DECODERS.get(version)
        if decoder is None:
            raise ValueError(
                f"timeline schema_version {version} is not supported, this "
                f"program reads versions {', '.join(map(str, sorted(_DECODERS)))}"
            )
        return decoder(data)


V1_KEYS = frozenset(
    {"schema_version", "duration_ms", "timing_source", "scenes", "captions", "music"}
)
V1_REQUIRED_KEYS = frozenset(
    {"schema_version", "duration_ms", "timing_source", "scenes"}
)


def _decode_v1(data: Mapping[str, Any]) -> Timeline:
    unknown = sorted(str(key) for key in set(data) - V1_KEYS)
    if unknown:
        raise ValueError(f"timeline has unknown keys: {', '.join(unknown)}")
    missing = sorted(V1_REQUIRED_KEYS - set(data))
    if missing:
        raise ValueError(f"timeline is missing keys: {', '.join(missing)}")
    try:
        timing_source = TimingSource(data["timing_source"])
    except (ValueError, TypeError):
        raise ValueError(
            f"timing_source {data['timing_source']!r} is not valid"
        ) from None
    scenes = data["scenes"]
    if not isinstance(scenes, list):
        raise ValueError("scenes must be a list")
    captions = data.get("captions", [])
    if not isinstance(captions, list):
        raise ValueError("captions must be a list")
    music = data.get("music", [])
    if not isinstance(music, list):
        raise ValueError("music must be a list")
    return Timeline(
        TIMELINE_SCHEMA_VERSION,
        data["duration_ms"],
        timing_source,
        tuple(Scene.from_dict(item) for item in scenes),
        tuple(CaptionOverlay.from_dict(item) for item in captions),
        tuple(AssetPlacement.from_dict(item) for item in music),
    )


_DECODERS: dict[int, Callable[[Mapping[str, Any]], Timeline]] = {
    TIMELINE_SCHEMA_VERSION: _decode_v1,
}
