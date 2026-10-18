"""LongForm scene planner (Prompt Pack v8, prompt J-106), context C8 Video Production.

``plan_longform_timeline`` turns the script a LongForm project is bound to into
a ``LongFormTimeline`` of scenes, one scene per script section, that covers the
audio duration exactly:

- ``timing_source`` is ``audio_chunks`` when ``chunk_durations`` has one value
  per section and their sum is within ``CHUNK_TOLERANCE`` (5%) of
  ``project.audio_duration_ms``.
- Otherwise ``timing_source`` is ``estimated``, allocating
  ``project.audio_duration_ms`` across sections by word count using largest
  remainder, with every scene lasting at least ``MIN_SCENE_MS`` (500 ms).
"""

from collections.abc import Sequence

from ai_youtube_agent.content.longform_project import LongFormProject
from ai_youtube_agent.content.longform_timeline import (
    MAX_LONGFORM_SCENES,
    MIN_SCENE_MS,
    TIMELINE_SCHEMA_VERSION,
    LongFormTimeline,
    Scene,
    TimingSource,
)
from ai_youtube_agent.content.script import Script
from ai_youtube_agent.content.shorts_planner import CHUNK_TOLERANCE, allocate


def plan_longform_timeline(
    project: LongFormProject,
    script: Script,
    chunk_durations: Sequence[int] | None = None,
) -> LongFormTimeline:
    """One scene per section of ``script``, spanning ``project.audio_duration_ms``."""
    if script.id != project.script_id or script.version != project.script_version:
        raise ValueError(
            f"script {script.id} version {script.version} is not the script "
            f"version the project is bound to ({project.script_id} version "
            f"{project.script_version})"
        )
    sections = script.sections
    if len(sections) > MAX_LONGFORM_SCENES:
        raise ValueError(
            f"the script has {len(sections)} sections, a LongForm timeline has "
            f"at most {MAX_LONGFORM_SCENES} scenes"
        )
    total = project.audio_duration_ms
    weights, source = _weights(project, script, chunk_durations)
    lengths = allocate(total, weights)
    scenes: list[Scene] = []
    start = 0
    for index, (section, length) in enumerate(zip(sections, lengths, strict=True)):
        scenes.append(
            Scene(f"s{index + 1}", index, start, start + length, section.title)
        )
        start += length
    return LongFormTimeline(
        TIMELINE_SCHEMA_VERSION,
        total,
        source,
        tuple(scenes),
    )


def _weights(
    project: LongFormProject,
    script: Script,
    chunk_durations: Sequence[int] | None,
) -> tuple[list[int], TimingSource]:
    sections = script.sections
    if chunk_durations is not None and len(chunk_durations) == len(sections):
        durations = list(chunk_durations)
        if all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in durations
        ):
            measured = sum(durations)
            gap = abs(measured - project.audio_duration_ms)
            if measured > 0 and gap <= project.audio_duration_ms * CHUNK_TOLERANCE:
                return durations, TimingSource.AUDIO_CHUNKS
    return [max(section.words, 1) for section in sections], TimingSource.ESTIMATED


__all__ = [
    "CHUNK_TOLERANCE",
    "MIN_SCENE_MS",
    "allocate",
    "plan_longform_timeline",
]

