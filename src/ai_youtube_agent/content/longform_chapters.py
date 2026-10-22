"""LongForm Chapters generator (Prompt Pack v8, J-107), context C8 Video Production.

``generate_chapters`` builds ``ChapterMarker`` entries from the bound LongForm
``Script`` (#064, #067) and the planned scenes of a ``LongFormTimeline`` (#106):

- Requires ``project.config.chapters`` to be ``True`` and a non-empty scene list
  covering every script section in order.
- Groups the script sections into contiguous chapters:
  - A LongForm script with ``SectionKind.CHAPTER`` sections starts chapter 1 at
    ``00:00`` (merging leading ``HOOK`` and ``INTRO`` sections into the first
    chapter so YouTube's ``00:00`` first-chapter rule holds, or making an
    ``Introduction`` chapter when ``HOOK + INTRO`` alone is at least
    ``MIN_CHAPTER_MS = 10_000`` ms), creates one chapter per ``CHAPTER``
    section using its ``title``, and merges trailing ``OUTRO`` and ``CTA``
    sections into the last chapter (or into a closing ``Conclusion`` chapter if
    fewer than ``MIN_CHAPTER_COUNT = 3`` chapters exist).
  - If a script has no ``CHAPTER`` sections (e.g. ``BODY`` sections with titles
    or plain sections), groups sections into at least ``MIN_CHAPTER_COUNT``
    chapters, each lasting at least ``MIN_CHAPTER_MS`` (10,000 ms), with unique
    titles.
- ``format_youtube_chapters`` renders the YouTube description timestamp block
  (``00:00 Title`` per line, ``MM:SS`` or ``HH:MM:SS``).
"""

from collections.abc import Sequence
from dataclasses import dataclass

from ai_youtube_agent.content.longform_project import LongFormProject
from ai_youtube_agent.content.longform_timeline import (
    MAX_CHAPTER_TITLE,
    MAX_CHAPTERS,
    MIN_CHAPTER_COUNT,
    MIN_CHAPTER_MS,
    ChapterMarker,
    LongFormTimeline,
    Scene,
    format_chapter_timestamp,
)
from ai_youtube_agent.content.script import Script, ScriptSection, SectionKind


@dataclass
class _Group:
    title: str
    scenes: list[Scene]

    @property
    def start_ms(self) -> int:
        return self.scenes[0].start_ms

    @property
    def end_ms(self) -> int:
        return self.scenes[-1].end_ms

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


def generate_chapters(
    project: LongFormProject,
    script: Script,
    timeline: LongFormTimeline | None = None,
) -> tuple[ChapterMarker, ...]:
    """Build contiguous ``ChapterMarker`` entries from ``script`` and ``timeline``."""
    if not isinstance(project, LongFormProject):
        raise TypeError("project must be a LongFormProject")
    if not isinstance(script, Script):
        raise TypeError("script must be a Script")
    active_timeline = timeline if timeline is not None else project.timeline
    if not isinstance(active_timeline, LongFormTimeline):
        raise TypeError("timeline must be a LongFormTimeline")
    if script.id != project.script_id or script.version != project.script_version:
        raise ValueError(
            f"script {script.id} version {script.version} is not the script "
            f"version the project is bound to ({project.script_id} version "
            f"{project.script_version})"
        )
    if not project.config.chapters:
        raise ValueError("the format of this LongForm project has chapters off")
    scenes = active_timeline.scenes
    if not scenes:
        raise ValueError("plan the scenes before generating chapters")
    if active_timeline.duration_ms < MIN_CHAPTER_COUNT * MIN_CHAPTER_MS:
        raise ValueError(
            f"timeline duration {active_timeline.duration_ms} ms is too short "
            f"for {MIN_CHAPTER_COUNT} chapters of at least {MIN_CHAPTER_MS} ms"
        )
    sections = script.sections
    for scene in scenes:
        if scene.section_index >= len(sections):
            raise ValueError(
                f"scene {scene.id} references out-of-range section "
                f"{scene.section_index}"
            )

    groups = _initial_groups(scenes, sections)
    groups = _merge_short_groups(groups)
    if len(groups) < MIN_CHAPTER_COUNT:
        raise ValueError(
            f"script sections produced {len(groups)} chapters of at least "
            f"{MIN_CHAPTER_MS} ms; at least {MIN_CHAPTER_COUNT} are required"
        )
    if len(groups) > MAX_CHAPTERS:
        raise ValueError(
            f"script produced {len(groups)} chapters, exceeding {MAX_CHAPTERS}"
        )

    titles = _deduplicate_titles([g.title for g in groups])
    markers: list[ChapterMarker] = []
    for idx, (group, title) in enumerate(zip(groups, titles, strict=True), start=1):
        markers.append(
            ChapterMarker(
                id=f"ch{idx}",
                title=title,
                start_scene_id=group.scenes[0].id,
                end_scene_id=group.scenes[-1].id,
                start_ms=group.start_ms,
                end_ms=group.end_ms,
            )
        )
    return tuple(markers)


def format_youtube_chapters(chapters: Sequence[ChapterMarker]) -> str:
    """Format chapters as a newline-separated YouTube description block."""
    if not isinstance(chapters, tuple | list):
        raise TypeError("chapters must be a sequence of ChapterMarker values")
    if not chapters:
        return ""
    for chapter in chapters:
        if not isinstance(chapter, ChapterMarker):
            raise TypeError("chapters must hold ChapterMarker values only")
    if len(chapters) < MIN_CHAPTER_COUNT:
        raise ValueError(
            f"YouTube needs at least {MIN_CHAPTER_COUNT} chapters to show them"
        )
    if chapters[0].start_ms != 0:
        raise ValueError("the first YouTube chapter must start at 00:00 (0 ms)")
    return "\n".join(
        f"{format_chapter_timestamp(chapter.start_ms)} {chapter.title}"
        for chapter in chapters
    )


def _initial_groups(
    scenes: tuple[Scene, ...], sections: tuple[ScriptSection, ...]
) -> list[_Group]:
    chapter_scene_indices = [
        idx
        for idx, scene in enumerate(scenes)
        if sections[scene.section_index].kind is SectionKind.CHAPTER
    ]
    if not chapter_scene_indices:
        # Group by scene section_index when no explicit CHAPTER kind is used.
        groups: list[_Group] = []
        for scene in scenes:
            sec = sections[scene.section_index]
            title = (
                scene.title or sec.title or _default_title(sec.kind, len(groups) + 1)
            )
            if groups and groups[-1].scenes[-1].section_index == scene.section_index:
                groups[-1].scenes.append(scene)
            else:
                groups.append(_Group(title=title, scenes=[scene]))
        return groups

    first_ch_idx = chapter_scene_indices[0]
    last_ch_idx = chapter_scene_indices[-1]
    groups = []

    leading = list(scenes[:first_ch_idx])
    leading_ms = sum(s.duration_ms for s in leading)
    if leading and leading_ms >= MIN_CHAPTER_MS:
        lead_title = (
            leading[0].title
            or sections[leading[0].section_index].title
            or "Introduction"
        )
        groups.append(_Group(title=lead_title, scenes=leading))
        pending_prefix: list[Scene] = []
    else:
        pending_prefix = leading

    for pos, scene_idx in enumerate(chapter_scene_indices):
        next_idx = (
            chapter_scene_indices[pos + 1]
            if pos + 1 < len(chapter_scene_indices)
            else last_ch_idx + 1
        )
        chunk = list(scenes[scene_idx:next_idx])
        if pos == 0 and pending_prefix:
            chunk = pending_prefix + chunk
        sec = sections[scenes[scene_idx].section_index]
        title = scenes[scene_idx].title or sec.title or f"Chapter {pos + 1}"
        groups.append(_Group(title=title, scenes=chunk))

    trailing = list(scenes[last_ch_idx + 1 :])
    if trailing:
        trailing_ms = sum(s.duration_ms for s in trailing)
        if trailing_ms >= MIN_CHAPTER_MS and len(groups) < MIN_CHAPTER_COUNT:
            trail_title = (
                trailing[0].title
                or sections[trailing[0].section_index].title
                or "Conclusion"
            )
            groups.append(_Group(title=trail_title, scenes=trailing))
        else:
            groups[-1].scenes.extend(trailing)

    return groups


def _merge_short_groups(groups: list[_Group]) -> list[_Group]:
    items = list(groups)
    while len(items) > 1:
        short_idx = next(
            (i for i, g in enumerate(items) if g.duration_ms < MIN_CHAPTER_MS),
            None,
        )
        if short_idx is None:
            break
        if short_idx == 0:
            items[1].scenes = items[0].scenes + items[1].scenes
            del items[0]
        else:
            items[short_idx - 1].scenes.extend(items[short_idx].scenes)
            del items[short_idx]
    return items


def _default_title(kind: SectionKind, number: int) -> str:
    if kind in (SectionKind.HOOK, SectionKind.INTRO):
        return "Introduction"
    if kind in (SectionKind.OUTRO, SectionKind.CTA):
        return "Conclusion"
    return f"Chapter {number}"


def _deduplicate_titles(raw_titles: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_titles, start=1):
        base = " ".join(raw.split())[:MAX_CHAPTER_TITLE] or f"Chapter {index}"
        candidate = base
        suffix = 2
        while candidate.casefold() in seen:
            tag = f" ({suffix})"
            trimmed = base[: MAX_CHAPTER_TITLE - len(tag)].rstrip()
            candidate = f"{trimmed}{tag}"
            suffix += 1
        seen.add(candidate.casefold())
        result.append(candidate)
    return result
