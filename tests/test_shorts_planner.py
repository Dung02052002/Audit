"""I-095 Shorts scene planner and planned status (Prompt Pack v8, prompt #095).

Rules the user accepted on 2026-10-17:

- ``plan_timeline`` makes one scene per script section that covers the audio
  exactly: timed from the chunk durations of the TTS job (``audio_chunks``) when
  there is one per section and they fit the audio, else from the words
  (``estimated``); the shares are whole milliseconds that add up exactly
  (largest remainder) and no scene is shorter than 500 ms;
- ``ShortsProjectService.plan`` saves that timeline, the project becomes
  ``planned``, and ``save_timeline`` checks the scenes against the bound script
  (every section appears, no index is out of range);
- ``ready`` stays unreachable until I-096 and I-097.
"""

import dataclasses
import sqlite3

import pytest
from factories import AI, T0, USER, at
from test_shorts_project import STANDARD, Code, World, entity_args, refusal

from ai_youtube_agent.content.script import Script, ScriptSection, SectionKind
from ai_youtube_agent.content.shorts_config import ShortsConfig
from ai_youtube_agent.content.shorts_planner import allocate, plan_timeline
from ai_youtube_agent.content.shorts_project import ShortsProject, ShortsProjectStatus
from ai_youtube_agent.content.shorts_service import TIMELINE_SAVED, readiness
from ai_youtube_agent.content.shorts_timeline import (
    MAX_SCENES,
    MIN_SCENE_MS,
    Scene,
    Timeline,
    TimingSource,
)
from ai_youtube_agent.content.tts_job import CHUNK_DONE, JOB_KIND, chunk_durations
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import ScriptRepository
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.db.repositories.shorts import ShortsProjectRepository
from ai_youtube_agent.pipeline.idempotency import audio_key
from ai_youtube_agent.pipeline.job import AIJob


@pytest.fixture
def world(database: Database) -> World:
    return World(database)


def make_script(texts, *, titles=None) -> Script:
    sections = [
        ScriptSection.create(
            SectionKind.BODY, text, title=titles[index] if titles else None
        )
        for index, text in enumerate(texts)
    ]
    return Script.create("ci1", sections=sections, clock=at(T0))


def words(count: int) -> str:
    return " ".join(["word"] * count)


def project_of(script: Script, duration_ms: int) -> ShortsProject:
    return ShortsProject.create(
        script.content_item_id,
        **entity_args(
            script_id=script.id,
            script_version=script.version,
            audio_duration_ms=duration_ms,
        ),
    )


# allocate


def test_allocate_shares_in_proportion_and_adds_up_exactly() -> None:
    assert allocate(6050 * 2, [2, 3, 5]) == [2420, 3630, 6050]
    assert allocate(1600, [1, 1, 1]) == [534, 533, 533]  # the first remainder
    assert allocate(30250, [7]) == [30250]


@pytest.mark.parametrize(
    ("total", "weights"),
    [
        (30250, [3, 5, 7, 11, 13]),
        (999_999, [1] * 59),
        (12_345, [100, 1, 1, 1, 50]),
        (3000, [1, 1, 100]),
        (1500, [0, 0, 0]),
    ],
)
def test_allocate_always_sums_to_the_total_with_the_minimum(total, weights) -> None:
    shares = allocate(total, weights)

    assert sum(shares) == total
    assert min(shares) >= MIN_SCENE_MS
    assert all(isinstance(share, int) for share in shares)


def test_allocate_raises_short_scenes_to_the_minimum_and_shares_the_rest() -> None:
    assert allocate(3000, [1, 1, 100]) == [500, 500, 2000]
    assert allocate(1500, [1, 1000, 1]) == [500, 500, 500]


def test_allocate_refuses_audio_too_short_for_the_scenes() -> None:
    with pytest.raises(ValueError, match="too short"):
        allocate(1499, [1, 1, 1])
    with pytest.raises(ValueError, match="nothing to plan"):
        allocate(1000, [])


# plan_timeline


def test_the_estimate_follows_the_words_of_each_section() -> None:
    script = make_script([words(2), words(3), words(5)], titles=["Hook", None, None])

    timeline = plan_timeline(project_of(script, 10_000), script)

    assert timeline.timing_source is TimingSource.ESTIMATED
    assert timeline.duration_ms == 10_000
    assert [(s.id, s.section_index, s.start_ms, s.end_ms) for s in timeline.scenes] == [
        ("s1", 0, 0, 2000),
        ("s2", 1, 2000, 5000),
        ("s3", 2, 5000, 10_000),
    ]
    assert [s.title for s in timeline.scenes] == ["Hook", None, None]


def test_the_estimate_rounds_to_whole_milliseconds_that_add_up() -> None:
    script = make_script([words(1), words(1), words(1)])

    timeline = plan_timeline(project_of(script, 1600), script)

    assert [s.duration_ms for s in timeline.scenes] == [534, 533, 533]
    assert timeline.scenes[-1].end_ms == 1600


def test_a_short_section_gets_the_minimum_and_the_others_give_it_up() -> None:
    script = make_script([words(1), words(1), words(100)])

    timeline = plan_timeline(project_of(script, 3000), script)

    assert [s.duration_ms for s in timeline.scenes] == [500, 500, 2000]


def test_a_script_of_one_section_is_one_scene() -> None:
    script = make_script([words(4)])

    timeline = plan_timeline(project_of(script, 30250), script)

    assert [(s.start_ms, s.end_ms) for s in timeline.scenes] == [(0, 30250)]


def test_chunk_durations_time_the_scenes_when_there_is_one_per_section() -> None:
    script = make_script([words(1), words(1), words(1)])

    timeline = plan_timeline(project_of(script, 6000), script, [1000, 2000, 3000])

    assert timeline.timing_source is TimingSource.AUDIO_CHUNKS
    assert [s.duration_ms for s in timeline.scenes] == [1000, 2000, 3000]


def test_chunk_durations_are_scaled_to_the_audio_length() -> None:
    script = make_script([words(1), words(1), words(1)])

    timeline = plan_timeline(project_of(script, 6100), script, (1000, 2000, 3000))

    assert timeline.timing_source is TimingSource.AUDIO_CHUNKS
    assert [s.duration_ms for s in timeline.scenes] == [1017, 2033, 3050]
    assert timeline.scenes[-1].end_ms == 6100


@pytest.mark.parametrize(
    "durations",
    [
        [1000, 2000],  # not one per section
        [1000, 2000, 3000, 4000],
        [1000, 2000, 3000],  # far from the audio length of 7000
        [0, 0, 0],
        [1000, -1, 3000],
        [1000, True, 3000],
        [1000, 2000.5, 3000],
    ],
)
def test_other_chunk_durations_fall_back_to_the_estimate(durations) -> None:
    script = make_script([words(1), words(1), words(1)])

    timeline = plan_timeline(project_of(script, 7000), script, durations)

    assert timeline.timing_source is TimingSource.ESTIMATED
    assert timeline.scenes[-1].end_ms == 7000


def test_the_script_must_be_the_one_the_project_is_bound_to() -> None:
    script = make_script([words(2)])
    other = dataclasses.replace(script, id="another")
    version = dataclasses.replace(script, version=2, parent_id=script.id)

    for wrong in (other, version):
        with pytest.raises(ValueError, match="bound to"):
            plan_timeline(project_of(script, 5000), wrong)


def test_a_script_with_too_many_sections_is_refused() -> None:
    script = make_script([words(1)] * (MAX_SCENES + 1))

    with pytest.raises(ValueError, match="at most 60"):
        plan_timeline(project_of(script, 60_000), script)


def test_audio_too_short_for_the_sections_is_refused() -> None:
    script = make_script([words(1)] * 3)

    with pytest.raises(ValueError, match="too short"):
        plan_timeline(project_of(script, 1499), script)


def test_the_planned_timeline_round_trips_and_has_a_stable_checksum() -> None:
    script = make_script([words(2), words(3)], titles=["A", "B"])
    project = project_of(script, 12_345)

    timeline = plan_timeline(project, script)

    assert Timeline.from_json(timeline.to_json()) == timeline
    assert plan_timeline(project, script).sha256 == timeline.sha256
    assert readiness(ShortsConfig.from_format(STANDARD), timeline) is (
        ShortsProjectStatus.PLANNED
    )


# chunk_durations (tts_job)


def job_with_chunks(world: World, script: Script, durations, *, succeed=True) -> AIJob:
    job = AIJob.create(
        JOB_KIND,
        audio_key(script.id, world.voice.id, world.voice.version),
        content_item_id=script.content_item_id,
        clock=at(T0),
    ).start(clock=at(T0))
    for index, duration in enumerate(durations):
        job = job.checkpoint(
            CHUNK_DONE,
            {"index": index, "section_index": index, "duration_ms": duration},
            clock=at(T0),
        )
    return job.succeed(clock=at(T0)) if succeed else job


def test_chunk_durations_reads_a_succeeded_job_in_chunk_order(world: World) -> None:
    script = make_script(["a", "b", "c"])

    assert chunk_durations(job_with_chunks(world, script, [10, 20, 30])) == (
        10,
        20,
        30,
    )


def test_chunk_durations_uses_the_newest_checkpoint_of_a_chunk(world: World) -> None:
    script = make_script(["a", "b"])
    job = job_with_chunks(world, script, [10, 20], succeed=False)
    job = job.checkpoint(
        CHUNK_DONE,
        {"index": 1, "section_index": 1, "duration_ms": 25},
        clock=at(T0),
    ).succeed(clock=at(T0))

    assert chunk_durations(job) == (10, 25)


def test_chunk_durations_is_none_without_a_complete_succeeded_job(
    world: World,
) -> None:
    script = make_script(["a", "b"])

    assert chunk_durations(job_with_chunks(world, script, [], succeed=True)) is None
    running = job_with_chunks(world, script, [10, 20], succeed=False)
    assert chunk_durations(running) is None
    gap = AIJob.create(JOB_KIND, "key", content_item_id="ci1", clock=at(T0))
    gap = gap.start(clock=at(T0)).checkpoint(
        CHUNK_DONE, {"index": 1, "duration_ms": 5}, clock=at(T0)
    )
    assert chunk_durations(gap.succeed(clock=at(T0))) is None


# The service


def add_sectioned_script(world: World, texts, duration_ms: int = 30250) -> Script:
    script = Script.create(
        world.item.id,
        sections=[ScriptSection.create(SectionKind.BODY, text) for text in texts],
        clock=at(T0),
    )
    with world.database.transaction() as connection:
        ScriptRepository(connection).add(script)
    world.add_audio(script, duration_ms)
    return script


def test_plan_makes_a_planned_project_with_one_scene_per_section(
    world: World,
) -> None:
    add_sectioned_script(world, [words(2), words(3), words(5)])
    project = world.service.create(world.item.id, actor=USER)

    planned = world.service.plan(project.id, expected_version=1, actor=AI)

    assert planned.status is ShortsProjectStatus.PLANNED
    assert planned.version == 2 and planned.updated_by == AI
    assert [s.section_index for s in planned.timeline.scenes] == [0, 1, 2]
    assert planned.timeline.timing_source is TimingSource.ESTIMATED
    assert planned.timeline.scenes[-1].end_ms == planned.audio_duration_ms
    assert world.project_of() == planned
    assert world.service.get(project.id).timeline.sha256 == planned.timeline.sha256


def test_plan_uses_the_chunk_durations_of_the_tts_job(world: World) -> None:
    script = add_sectioned_script(world, [words(1), words(1), words(1)], 6000)
    with world.database.transaction() as connection:
        AIJobRepository(connection).add(
            job_with_chunks(world, script, [1000, 2000, 3000])
        )
    project = world.service.create(world.item.id, actor=USER)

    planned = world.service.plan(project.id, expected_version=1, actor=USER)

    assert planned.timeline.timing_source is TimingSource.AUDIO_CHUNKS
    assert [s.duration_ms for s in planned.timeline.scenes] == [1000, 2000, 3000]


def test_plan_audits_counts_and_the_checksum(world: World) -> None:
    add_sectioned_script(world, [words(2), words(3)])
    project = world.service.create(world.item.id, actor=USER)

    planned = world.service.plan(project.id, expected_version=1, actor=USER)

    event = world.events()[-1]
    assert event.action == TIMELINE_SAVED
    assert event.metadata["scene_count"] == 2
    assert event.metadata["status"] == "planned"
    assert event.metadata["timeline_sha256"] == planned.timeline.sha256
    assert event.metadata["timing_source"] == "estimated"
    assert event.metadata["version"] == 2


def test_plan_needs_the_version_that_was_read(world: World) -> None:
    add_sectioned_script(world, [words(2)])
    project = world.service.create(world.item.id, actor=USER)

    refusal(
        lambda: world.service.plan(project.id, expected_version=9, actor=USER),
        Code.CONFLICT,
    )

    assert world.project_of() == project


def test_plan_refuses_an_unknown_project(world: World) -> None:
    refusal(
        lambda: world.service.plan("missing", expected_version=1, actor=USER),
        Code.NOT_FOUND,
    )


def test_plan_refuses_audio_too_short_for_the_sections(world: World) -> None:
    add_sectioned_script(world, [words(1)] * 3, 1499)
    project = world.service.create(world.item.id, actor=USER)

    error = refusal(
        lambda: world.service.plan(project.id, expected_version=1, actor=USER),
        Code.TIMELINE_INVALID,
    )

    assert "too short" in str(error)
    assert world.project_of() == project


def test_planning_again_replaces_the_timeline(world: World) -> None:
    add_sectioned_script(world, [words(2), words(3)])
    project = world.service.create(world.item.id, actor=USER)
    first = world.service.plan(project.id, expected_version=1, actor=USER)

    second = world.service.plan(project.id, expected_version=2, actor=USER)

    assert second.version == 3
    assert second.timeline == first.timeline


def test_a_saved_timeline_with_scenes_is_planned_and_never_ready(
    world: World,
) -> None:
    script = add_sectioned_script(world, [words(2), words(3)], 10_000)
    project = world.service.create(world.item.id, actor=USER)
    timeline = Timeline(
        1,
        10_000,
        TimingSource.ESTIMATED,
        (Scene("s1", 0, 0, 4000, None), Scene("s2", 1, 4000, 10_000, None)),
    )

    saved = world.service.save_timeline(
        project.id, timeline, expected_version=1, actor=USER
    )

    assert saved.status is ShortsProjectStatus.PLANNED
    assert saved.script_id == script.id
    assert world.project_of() == saved


def test_save_timeline_refuses_scenes_that_miss_a_section(world: World) -> None:
    add_sectioned_script(world, [words(2), words(3), words(4)], 10_000)
    project = world.service.create(world.item.id, actor=USER)
    timeline = Timeline(
        1,
        10_000,
        TimingSource.ESTIMATED,
        (Scene("s1", 0, 0, 4000, None), Scene("s2", 2, 4000, 10_000, None)),
    )

    error = refusal(
        lambda: world.service.save_timeline(
            project.id, timeline, expected_version=1, actor=USER
        ),
        Code.TIMELINE_INVALID,
    )

    assert "section 1" in str(error)
    assert world.project_of() == project
    assert len(world.events()) == 1


def test_save_timeline_refuses_a_section_index_out_of_range(world: World) -> None:
    add_sectioned_script(world, [words(2), words(3)], 10_000)
    project = world.service.create(world.item.id, actor=USER)
    timeline = Timeline(
        1,
        10_000,
        TimingSource.ESTIMATED,
        (
            Scene("s1", 0, 0, 3000, None),
            Scene("s2", 1, 3000, 6000, None),
            Scene("s3", 2, 6000, 10_000, None),
        ),
    )

    error = refusal(
        lambda: world.service.save_timeline(
            project.id, timeline, expected_version=1, actor=USER
        ),
        Code.TIMELINE_INVALID,
    )

    assert "section 2" in str(error)
    assert world.project_of() == project


def test_save_timeline_accepts_several_scenes_of_one_section(world: World) -> None:
    add_sectioned_script(world, [words(2), words(3)], 10_000)
    project = world.service.create(world.item.id, actor=USER)
    timeline = Timeline(
        1,
        10_000,
        TimingSource.ESTIMATED,
        (
            Scene("s1", 0, 0, 3000, None),
            Scene("s2", 0, 3000, 6000, None),
            Scene("s3", 1, 6000, 10_000, None),
        ),
    )

    saved = world.service.save_timeline(
        project.id, timeline, expected_version=1, actor=USER
    )

    assert saved.status is ShortsProjectStatus.PLANNED


def test_rebind_after_planning_starts_again_as_a_draft(world: World) -> None:
    add_sectioned_script(world, [words(2), words(3)])
    project = world.service.create(world.item.id, actor=USER)
    world.service.plan(project.id, expected_version=1, actor=USER)

    rebound = world.service.rebind(project.id, expected_version=2, actor=USER)

    assert rebound.status is ShortsProjectStatus.DRAFT
    assert rebound.timeline.scenes == ()


# The store


def test_a_planned_project_with_scenes_round_trips_through_the_database(
    world: World,
) -> None:
    add_sectioned_script(world, [words(2), words(3)])
    project = world.service.create(world.item.id, actor=USER)
    planned = world.service.plan(project.id, expected_version=1, actor=USER)

    with world.database.transaction() as connection:
        stored = ShortsProjectRepository(connection).get(project.id)
        (status,) = connection.execute(
            "SELECT status FROM shorts_projects WHERE id = ?", (project.id,)
        ).fetchone()

    assert stored == planned and status == "planned"
    assert stored.timeline.sha256 == planned.timeline.sha256


def test_the_sql_check_accepts_planned_with_scenes_and_refuses_draft_with_them(
    world: World,
) -> None:
    add_sectioned_script(world, [words(2), words(3)])
    project = world.service.create(world.item.id, actor=USER)
    planned = world.service.plan(project.id, expected_version=1, actor=USER)

    with world.database.transaction() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE shorts_projects SET status = 'draft' WHERE id = ?",
                (project.id,),
            )

    assert world.project_of() == planned
