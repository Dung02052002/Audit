"""LongForm timeline (Prompt Pack v8, prompt J-106), context C8 Video Production.

``LongFormTimeline`` is the chapter, scene, caption and music plan of a
LongForm video. It is stored as the ``timeline_json`` column of
``longform_projects`` (migration 0028) and its canonical JSON (``to_json``) and
checksum (``sha256``) bind the later LongForm steps (chapters #107, B-roll #108,
subtitles #109, audio mix #110, render #111).

Layout of schema version 1::

    {"schema_version": 1, "duration_ms": 240000,
     "timing_source": "audio_chunks" | "estimated",
     "scenes": [], "chapters": [], "captions": [], "music": []}

- ``scenes``: ordered ``Scene`` values (``s1..sN``, up to
  ``MAX_LONGFORM_SCENES = 120``), contiguous from ``0`` to ``duration_ms``,
  non-decreasing ``section_index``.
- ``chapters``: ordered ``ChapterMarker`` values (``ch1..chK``, either empty or
  ``MIN_CHAPTER_COUNT = 3`` to ``MAX_CHAPTERS = 50``), contiguous from ``0`` to
  ``duration_ms``, each lasting at least ``MIN_CHAPTER_MS = 10_000`` ms (10s),
  each aligned to scene boundaries (``start_scene_id`` and ``end_scene_id``).
- ``captions``: ordered non-overlapping ``CaptionOverlay`` values (``c1..cM``,
  up to ``MAX_LONGFORM_CAPTIONS = 2000``), each lying inside its ``scene_id``.
- ``music``: up to ``MAX_MUSIC = 10`` ``AssetPlacement`` values within
  ``duration_ms``.
"""

import hashlib
import json
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from ai_youtube_agent.content.shorts_timeline import (
    AUDIO_ROLES,
    MAX_DURATION_MS,
    MAX_MUSIC,
    MAX_SCENE_ASSETS,
    MAX_SECTION_INDEX,
    MAX_TITLE,
    MIN_SCENE_MS,
    ROLE_KINDS,
    TIMELINE_SCHEMA_VERSION,
    VISUAL_ROLES,
    AssetFit,
    AssetPlacement,
    AssetRole,
    Box,
    CaptionAlign,
    CaptionAnchor,
    CaptionOverlay,
    CaptionPosition,
    CaptionStyle,
    Scene,
    SceneAsset,
    TimingSource,
)

MAX_LONGFORM_SCENES: Final[int] = 120
MAX_LONGFORM_CAPTIONS: Final[int] = 2000
MIN_CHAPTER_COUNT: Final[int] = 3
MAX_CHAPTERS: Final[int] = 50
MIN_CHAPTER_MS: Final[int] = 10_000
MAX_CHAPTER_TITLE: Final[int] = 100

CHAPTER_KEYS: Final[frozenset[str]] = frozenset(
    {
        "id",
        "title",
        "start_scene_id",
        "end_scene_id",
        "start_ms",
        "end_ms",
    }
)
LONGFORM_TIMELINE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "duration_ms",
        "timing_source",
        "scenes",
        "chapters",
        "captions",
        "music",
    }
)
LONGFORM_TIMELINE_REQUIRED_KEYS: Final[frozenset[str]] = frozenset(
    {"schema_version", "duration_ms", "timing_source", "scenes"}
)

__all__ = [
    "AUDIO_ROLES",
    "AssetFit",
    "AssetPlacement",
    "AssetRole",
    "Box",
    "CaptionAlign",
    "CaptionAnchor",
    "CaptionOverlay",
    "CaptionPosition",
    "CaptionStyle",
    "ChapterMarker",
    "LongFormTimeline",
    "MAX_CHAPTERS",
    "MAX_CHAPTER_TITLE",
    "MAX_DURATION_MS",
    "MAX_LONGFORM_CAPTIONS",
    "MAX_LONGFORM_SCENES",
    "MAX_MUSIC",
    "MAX_SCENE_ASSETS",
    "MAX_SECTION_INDEX",
    "MAX_TITLE",
    "MIN_CHAPTER_COUNT",
    "MIN_CHAPTER_MS",
    "MIN_SCENE_MS",
    "ROLE_KINDS",
    "Scene",
    "SceneAsset",
    "TIMELINE_SCHEMA_VERSION",
    "TimingSource",
    "VISUAL_ROLES",
    "format_chapter_timestamp",
]


def _whole(name: str, value: object, low: int, high: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise ValueError(f"{name} must be a whole number from {low} to {high}")


def _strict_keys(
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


def format_chapter_timestamp(start_ms: int) -> str:
    """Format ``start_ms`` as YouTube chapter timestamp ``MM:SS`` or ``HH:MM:SS``."""
    _whole("start_ms", start_ms, 0, MAX_DURATION_MS)
    total_seconds = start_ms // 1000
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


@dataclass(frozen=True)
class ChapterMarker:
    id: str
    title: str
    start_scene_id: str
    end_scene_id: str
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        for field_name in ("id", "start_scene_id", "end_scene_id"):
            val = getattr(self, field_name)
            if not isinstance(val, str) or not val.strip() or val != val.strip():
                raise ValueError(f"chapter {field_name} must be non-empty trimmed text")
        if not isinstance(self.title, str):
            raise TypeError("chapter title must be text")
        cleaned_title = " ".join(unicodedata.normalize("NFC", self.title).split())
        if not cleaned_title or len(cleaned_title) > MAX_CHAPTER_TITLE:
            raise ValueError(
                f"chapter title must be 1 to {MAX_CHAPTER_TITLE} characters"
            )
        object.__setattr__(self, "title", cleaned_title)
        _whole("start_ms", self.start_ms, 0, MAX_DURATION_MS)
        _whole("end_ms", self.end_ms, 1, MAX_DURATION_MS)
        if self.end_ms - self.start_ms < MIN_CHAPTER_MS:
            raise ValueError(
                f"chapter {self.id} lasts {self.end_ms - self.start_ms} ms, "
                f"less than the {MIN_CHAPTER_MS} ms minimum"
            )

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def timestamp(self) -> str:
        return format_chapter_timestamp(self.start_ms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "start_scene_id": self.start_scene_id,
            "end_scene_id": self.end_scene_id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
        }

    @classmethod
    def from_dict(cls, data: object) -> "ChapterMarker":
        values = _strict_keys("chapter", data, CHAPTER_KEYS, CHAPTER_KEYS)
        return cls(**values)


def _check_scenes(scenes: tuple[Any, ...], duration_ms: int) -> None:
    if len(scenes) > MAX_LONGFORM_SCENES:
        raise ValueError(
            f"a LongForm timeline has at most {MAX_LONGFORM_SCENES} scenes"
        )
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
            raise ValueError(
                f"scene {scene.id} goes back to an earlier section"
            )
        expected_start = scene.end_ms
        section_index = scene.section_index
    if expected_start != duration_ms:
        raise ValueError(
            f"the scenes end at {expected_start} ms but the audio is "
            f"{duration_ms} ms"
        )


def _check_chapters(
    chapters: tuple[Any, ...],
    scenes: tuple[Scene, ...],
    duration_ms: int,
) -> None:
    if not scenes:
        raise ValueError("chapters need scenes: plan the scenes first")
    if not MIN_CHAPTER_COUNT <= len(chapters) <= MAX_CHAPTERS:
        raise ValueError(
            f"a LongForm timeline needs {MIN_CHAPTER_COUNT} to {MAX_CHAPTERS} "
            f"chapters when chapters are present"
        )
    scene_index_by_id = {scene.id: idx for idx, scene in enumerate(scenes)}
    expected_start = 0
    next_scene_idx = 0
    seen_titles: set[str] = set()
    for position, chapter in enumerate(chapters, start=1):
        if not isinstance(chapter, ChapterMarker):
            raise TypeError("chapters must be ChapterMarker values")
        if chapter.id != f"ch{position}":
            raise ValueError(
                f"chapter {position} must have the id ch{position}"
            )
        title_key = chapter.title.casefold()
        if title_key in seen_titles:
            raise ValueError(f"chapter title {chapter.title!r} is repeated")
        seen_titles.add(title_key)
        start_idx = scene_index_by_id.get(chapter.start_scene_id)
        end_idx = scene_index_by_id.get(chapter.end_scene_id)
        if start_idx is None or end_idx is None:
            raise ValueError(
                f"chapter {chapter.id} references an unknown scene id"
            )
        if start_idx != next_scene_idx or end_idx < start_idx:
            raise ValueError(
                f"chapter {chapter.id} scenes must be contiguous starting at "
                f"{scenes[next_scene_idx].id}"
            )
        start_scene = scenes[start_idx]
        end_scene = scenes[end_idx]
        if chapter.start_ms != expected_start or chapter.start_ms != start_scene.start_ms:
            raise ValueError(
                f"chapter {chapter.id} start_ms ({chapter.start_ms}) must match "
                f"scene {start_scene.id} start_ms ({start_scene.start_ms})"
            )
        if chapter.end_ms != end_scene.end_ms:
            raise ValueError(
                f"chapter {chapter.id} end_ms ({chapter.end_ms}) must match "
                f"scene {end_scene.id} end_ms ({end_scene.end_ms})"
            )
        expected_start = chapter.end_ms
        next_scene_idx = end_idx + 1
    if expected_start != duration_ms or next_scene_idx != len(scenes):
        raise ValueError(
            "chapters must cover all scenes through the end of the timeline"
        )


def _check_captions(
    captions: tuple[Any, ...],
    scenes: tuple[Scene, ...],
    duration_ms: int,
) -> None:
    if len(captions) > MAX_LONGFORM_CAPTIONS:
        raise ValueError(
            f"a LongForm timeline has at most {MAX_LONGFORM_CAPTIONS} captions"
        )
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
                f"caption {caption.id} ends at {caption.end_ms} ms, after the "
                f"audio ({duration_ms} ms)"
            )
        if caption.start_ms < previous_end:
            raise ValueError(
                f"caption {caption.id} starts before the previous caption ends"
            )
        if caption.start_ms < scene.start_ms or caption.end_ms > scene.end_ms:
            raise ValueError(
                f"caption {caption.id} must lie within scene {scene.id}"
            )
        previous_end = caption.end_ms


@dataclass(frozen=True)
class LongFormTimeline:
    schema_version: int
    duration_ms: int
    timing_source: TimingSource
    scenes: tuple[Scene, ...] = ()
    chapters: tuple[ChapterMarker, ...] = ()
    captions: tuple[CaptionOverlay, ...] = ()
    music: tuple[AssetPlacement, ...] = ()

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or self.schema_version != TIMELINE_SCHEMA_VERSION
        ):
            raise ValueError(
                f"schema_version must be {TIMELINE_SCHEMA_VERSION} "
                "(the only version this program writes)"
            )
        _whole("duration_ms", self.duration_ms, 1, MAX_DURATION_MS)
        if not isinstance(self.timing_source, TimingSource):
            raise TypeError("timing_source must be a TimingSource")
        for name in ("scenes", "chapters", "captions", "music"):
            if not isinstance(getattr(self, name), tuple):
                raise TypeError(f"{name} must be a tuple")
        if self.scenes:
            _check_scenes(self.scenes, self.duration_ms)
        if self.chapters:
            _check_chapters(self.chapters, self.scenes, self.duration_ms)
        if self.captions:
            _check_captions(self.captions, self.scenes, self.duration_ms)
        if len(self.music) > MAX_MUSIC:
            raise ValueError(f"a timeline has at most {MAX_MUSIC} music placements")
        for placement in self.music:
            if not isinstance(placement, AssetPlacement):
                raise TypeError("music must be AssetPlacement values")
            placement.check_within(self.duration_ms, "timeline")

    @classmethod
    def empty(cls, duration_ms: int) -> "LongFormTimeline":
        """A timeline of ``duration_ms`` with nothing planned yet."""
        return cls(TIMELINE_SCHEMA_VERSION, duration_ms, TimingSource.ESTIMATED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "duration_ms": self.duration_ms,
            "timing_source": self.timing_source.value,
            "scenes": [scene.to_dict() for scene in self.scenes],
            "chapters": [chapter.to_dict() for chapter in self.chapters],
            "captions": [caption.to_dict() for caption in self.captions],
            "music": [placement.to_dict() for placement in self.music],
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, data: object) -> "LongFormTimeline":
        if not isinstance(data, Mapping):
            raise ValueError("timeline must be an object")
        version = data.get("schema_version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValueError("timeline schema_version must be an integer")
        decoder = _DECODERS.get(version)
        if decoder is None:
            raise ValueError(
                f"timeline schema_version {version} is not supported, this "
                f"program reads version {TIMELINE_SCHEMA_VERSION}"
            )
        return decoder(data)

    @classmethod
    def from_json(cls, text: str) -> "LongFormTimeline":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            raise ValueError(f"timeline is not valid JSON: {error}") from error
        return cls.from_dict(data)


def _decode_v1(data: Mapping[str, Any]) -> LongFormTimeline:
    values = _strict_keys(
        "timeline",
        data,
        LONGFORM_TIMELINE_KEYS,
        LONGFORM_TIMELINE_REQUIRED_KEYS,
    )
    try:
        source = TimingSource(values["timing_source"])
    except (ValueError, TypeError):
        raise ValueError(
            f"timing_source {values['timing_source']!r} is not valid"
        ) from None
    raw_scenes = values["scenes"]
    raw_chapters = values.get("chapters", [])
    raw_captions = values.get("captions", [])
    raw_music = values.get("music", [])
    for label, seq in (
        ("scenes", raw_scenes),
        ("chapters", raw_chapters),
        ("captions", raw_captions),
        ("music", raw_music),
    ):
        if not isinstance(seq, list | tuple):
            raise ValueError(f"timeline {label} must be a list")
    return LongFormTimeline(
        schema_version=values["schema_version"],
        duration_ms=values["duration_ms"],
        timing_source=source,
        scenes=tuple(Scene.from_dict(item) for item in raw_scenes),
        chapters=tuple(ChapterMarker.from_dict(item) for item in raw_chapters),
        captions=tuple(CaptionOverlay.from_dict(item) for item in raw_captions),
        music=tuple(AssetPlacement.from_dict(item) for item in raw_music),
    )


_DECODERS: dict[int, Callable[[Mapping[str, Any]], LongFormTimeline]] = {
    1: _decode_v1,
}

