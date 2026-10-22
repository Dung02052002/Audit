"""Shorts scene planner (Prompt Pack v8, prompt #095), context C8 Shorts Production.

``plan_timeline`` turns the script a Shorts project is bound to into a timeline
of scenes, one scene per script section, that covers the audio exactly:

- ``timing_source`` is ``audio_chunks`` when ``chunk_durations`` has one value per
  section (the ``duration_ms`` of the ``audio.chunk_done`` checkpoints of the TTS
  job, see ``tts_job.chunk_durations``; a section is one chunk exactly when the
  chunk count equals the section count) and their sum is within
  ``CHUNK_TOLERANCE`` of the project's audio length. The durations are scaled to
  the audio length, because the merged audio is the master clock.
- Otherwise the scenes are ``estimated``: the audio length is shared by the
  words of the sections.
- Both shares are whole milliseconds that add up to the audio length (largest
  remainder), and no scene is shorter than ``MIN_SCENE_MS``: a short scene is
  raised to the minimum and the others give up the time in proportion. When the
  audio is too short for that, or the script has more than ``MAX_SCENES``
  sections, a ``ValueError`` says so.

Nothing here reads a store: the service passes the script and the durations.
"""

from collections.abc import Sequence

from ai_youtube_agent.content.script import Script
from ai_youtube_agent.content.shorts_project import ShortsProject
from ai_youtube_agent.content.shorts_timeline import (
    MAX_SCENES,
    MIN_SCENE_MS,
    TIMELINE_SCHEMA_VERSION,
    Scene,
    Timeline,
    TimingSource,
)

CHUNK_TOLERANCE = 0.05


def plan_timeline(
    project: ShortsProject,
    script: Script,
    chunk_durations: Sequence[int] | None = None,
) -> Timeline:
    """One scene per section of ``script``, as long as the audio of ``project``."""
    if script.id != project.script_id or script.version != project.script_version:
        raise ValueError(
            f"script {script.id} version {script.version} is not the script "
            f"version the project is bound to ({project.script_id} version "
            f"{project.script_version})"
        )
    sections = script.sections
    if len(sections) > MAX_SCENES:
        raise ValueError(
            f"the script has {len(sections)} sections, a timeline has at most "
            f"{MAX_SCENES} scenes"
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
    return Timeline(TIMELINE_SCHEMA_VERSION, total, source, tuple(scenes))


def _weights(
    project: ShortsProject, script: Script, chunk_durations: Sequence[int] | None
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


def allocate(total: int, weights: Sequence[int]) -> list[int]:
    """Share ``total`` ms by ``weights`` in whole ms, each at least MIN_SCENE_MS.

    The shares add up to ``total`` exactly. A ValueError when ``total`` cannot
    give every weight the minimum.
    """
    count = len(weights)
    if count == 0:
        raise ValueError("there is nothing to plan")
    if total < MIN_SCENE_MS * count:
        raise ValueError(
            f"{total} ms of audio is too short for {count} scenes of at least "
            f"{MIN_SCENE_MS} ms"
        )
    values = [max(int(weight), 1) for weight in weights]
    fixed: set[int] = set()
    while True:
        free = [index for index in range(count) if index not in fixed]
        remaining = total - MIN_SCENE_MS * len(fixed)
        shares = _largest_remainder(remaining, [values[index] for index in free])
        low = [
            index
            for index, share in zip(free, shares, strict=True)
            if share < MIN_SCENE_MS
        ]
        if not low:
            break
        fixed.update(low)
    result = [MIN_SCENE_MS] * count
    for index, share in zip(free, shares, strict=True):
        result[index] = share
    return result


def _largest_remainder(total: int, weights: Sequence[int]) -> list[int]:
    weight_sum = sum(weights)
    shares = [total * weight // weight_sum for weight in weights]
    left = total - sum(shares)
    order = sorted(
        range(len(weights)),
        key=lambda index: (-(total * weights[index] % weight_sum), index),
    )
    for index in order[:left]:
        shares[index] += 1
    return shares
