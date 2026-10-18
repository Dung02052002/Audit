"""I-094 Shorts project (Prompt Pack v8, prompt #094).

Rules the user accepted on 2026-10-17:

- one mutable project per Shorts content item, changed through a version
  counter (``expected_version``);
- it is bound to the latest script version and the latest audio artifact
  version of the item (the audio must voice that script, else
  ``domain.shorts_stale_input``), to the strategy version and to a
  ``ShortsConfig`` copied from the strategy's Shorts format;
- audio longer than the format's maximum is refused, shorter audio is only
  reported; the timeline is always as long as the audio;
- ``rebind`` makes it again from the new latest inputs as an empty draft;
  ``save_timeline`` recomputes the status, which is ``draft`` while the
  timeline is empty (scenes come with I-095 to I-097);
- one event per change is audited after the commit with ids and counts only.
"""

import dataclasses
import json
import sqlite3
from datetime import timedelta

import pytest

from ai_youtube_agent.content.shorts_config import ShortsConfig
from ai_youtube_agent.content.shorts_project import ShortsProject, ShortsProjectStatus
from ai_youtube_agent.content.shorts_service import (
    CREATED,
    HTTP_STATUS,
    REBOUND,
    TIMELINE_SAVED,
    USER_MESSAGES,
    ShortsProjectError,
    ShortsProjectErrorCode,
    ShortsProjectService,
    readiness,
)
from ai_youtube_agent.content.shorts_timeline import (
    CaptionOverlay,
    Scene,
    Timeline,
    TimingSource,
)
from ai_youtube_agent.content.strategy import (
    FormatSettings,
    LongFormFormat,
    ShortsFormat,
)
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus, ContentType
from ai_youtube_agent.core.db.codec import format_datetime
from ai_youtube_agent.core.db.database import (
    ConcurrencyError,
    Database,
    RecordNotFoundError,
)
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
    VoiceProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    AudioMetadataRepository,
    ContentItemRepository,
    ScriptRepository,
)
from ai_youtube_agent.core.db.repositories.shorts import ShortsProjectRepository
from ai_youtube_agent.core.errors import to_public
from factories import (
    AI,
    SYSTEM,
    T0,
    USER,
    at,
    make_artifact,
    make_audio_metadata,
    make_channel,
    make_content_item,
    make_script,
    make_strategy_profile,
    make_voice_profile,
)

T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)
SCRIPT_TEXT = "Most people never check bank fees."
STANDARD = ShortsFormat(15, 60)
Code = ShortsProjectErrorCode


class World:
    """One channel with a strategy, a voice and Shorts items to produce."""

    def __init__(
        self,
        database: Database,
        shorts: ShortsFormat | None = STANDARD,
        *,
        format_settings: bool = True,
    ) -> None:
        self.database = database
        self.channel = make_channel()
        overrides = {}
        if not format_settings:
            overrides["format"] = None
        elif shorts is not None:
            overrides["format"] = FormatSettings(shorts, LongFormFormat(480, 900))
        self.strategy = make_strategy_profile(self.channel, **overrides)
        self.voice = make_voice_profile(self.channel)
        self.item = make_content_item(self.channel, self.strategy)
        with database.transaction() as connection:
            ChannelRepository(connection).add(self.channel)
            StrategyProfileRepository(connection).add(self.strategy)
            VoiceProfileRepository(connection).add(self.voice)
            ContentItemRepository(connection).add(self.item)
        self.sink = InMemoryAuditSink()
        self.audit = AuditLog(self.sink, clock=at(T2))
        self.service = ShortsProjectService(database, self.audit, clock=at(T1))

    def add_item(self, **overrides) -> ContentItem:
        item = make_content_item(self.channel, self.strategy, **overrides)
        with self.database.transaction() as connection:
            ContentItemRepository(connection).add(item)
        return item

    def add_script(self, item: ContentItem | None = None):
        item = item or self.item
        with self.database.transaction() as connection:
            repository = ScriptRepository(connection)
            versions = repository.list_by_content_item(item.id)
            if versions:
                n = versions[-1].version + 1
                script = versions[-1].next_version(f"{SCRIPT_TEXT} Revision {n}.")
            else:
                script = make_script(item)
            repository.add(script)
        return script

    def add_audio(
        self, script, duration_ms: int = 30250, *, metadata: bool = True
    ) -> Artifact:
        item_id = script.content_item_id
        with self.database.transaction() as connection:
            artifacts = ArtifactRepository(connection)
            latest = artifacts.latest(item_id, ArtifactKind.AUDIO)
            version = latest.version + 1 if latest else 1
            item = ContentItemRepository(connection).get(item_id)
            artifact = make_artifact(
                item, ArtifactKind.AUDIO, version=version, media_type="audio/wav"
            )
            artifacts.add(artifact)
            if metadata:
                AudioMetadataRepository(connection).add(
                    make_audio_metadata(
                        artifact, script, self.voice, duration_ms=duration_ms
                    )
                )
        return artifact

    def prepare(self, item: ContentItem | None = None, duration_ms: int = 30250):
        """A new script version and the audio that voices it."""
        script = self.add_script(item)
        return script, self.add_audio(script, duration_ms)

    def create(self, item: ContentItem | None = None, **kwargs) -> ShortsProject:
        item = item or self.item
        if self.latest_script(item) is None:
            self.prepare(item, **kwargs)
        return self.service.create(item.id, actor=USER)

    def latest_script(self, item: ContentItem | None = None):
        with self.database.transaction() as connection:
            versions = ScriptRepository(connection).list_by_content_item(
                (item or self.item).id
            )
        return versions[-1] if versions else None

    def project_of(self, item: ContentItem | None = None) -> ShortsProject | None:
        with self.database.transaction() as connection:
            return ShortsProjectRepository(connection).get_by_content_item(
                (item or self.item).id
            )

    def count(self, table: str = "shorts_projects") -> int:
        with self.database.transaction() as connection:
            (count,) = connection.execute(f"SELECT count(*) FROM {table}").fetchone()
        return count

    def entity(self, item: ContentItem | None = None, **overrides) -> ShortsProject:
        """A project built by hand from the stored script and audio of the item."""
        item = item or self.item
        script = self.latest_script(item)
        with self.database.transaction() as connection:
            artifact = ArtifactRepository(connection).latest(
                item.id, ArtifactKind.AUDIO
            )
        project = ShortsProject.create(
            item.id,
            script_id=script.id,
            script_version=script.version,
            audio_artifact_id=artifact.id,
            audio_version=artifact.version,
            audio_sha256=artifact.sha256,
            audio_duration_ms=30250,
            strategy_version=self.strategy.version,
            config=ShortsConfig.from_format(STANDARD),
            actor=USER,
            clock=at(T1),
        )
        return dataclasses.replace(project, **overrides)

    def save(self, project: ShortsProject) -> None:
        with self.database.transaction() as connection:
            ShortsProjectRepository(connection).add(project)

    def events(self):
        return self.sink.events()


@pytest.fixture
def world(database: Database) -> World:
    return World(database)


def refuse_create(
    world: "World", code: ShortsProjectErrorCode, item_id: str | None = None
) -> ShortsProjectError:
    return refusal(
        lambda: world.service.create(item_id or world.item.id, actor=USER), code
    )


def refusal(call, code: ShortsProjectErrorCode) -> ShortsProjectError:
    with pytest.raises(ShortsProjectError) as info:
        call()
    assert info.value.code == code.value
    return info.value


# The entity


def entity_args(**overrides) -> dict:
    args = {
        "script_id": "sc1",
        "script_version": 2,
        "audio_artifact_id": "ar1",
        "audio_version": 3,
        "audio_sha256": "a" * 64,
        "audio_duration_ms": 30250,
        "strategy_version": 4,
        "config": ShortsConfig.from_format(STANDARD),
        "actor": USER,
        "clock": at(T1),
    }
    return args | overrides


def make_project(**overrides) -> ShortsProject:
    return ShortsProject.create("ci1", **entity_args(**overrides))


def test_create_makes_a_draft_bound_to_its_inputs() -> None:
    project = make_project()

    assert len(project.id) == 32
    assert project.content_item_id == "ci1"
    assert (project.script_id, project.script_version) == ("sc1", 2)
    assert (project.audio_artifact_id, project.audio_version) == ("ar1", 3)
    assert project.audio_sha256 == "a" * 64
    assert project.audio_duration_ms == 30250
    assert project.strategy_version == 4
    assert project.status is ShortsProjectStatus.DRAFT
    assert project.version == 1
    assert project.timeline == Timeline.empty(30250)
    assert project.created_by == project.updated_by == USER
    assert project.created_at == project.updated_at == T1


def test_create_gives_every_project_a_new_id() -> None:
    assert make_project().id != make_project().id


def test_create_uses_utc_now_without_a_clock() -> None:
    project = make_project(clock=None)

    assert project.created_at.utcoffset() == timedelta(0)


def test_any_kind_of_actor_may_make_a_project() -> None:
    for actor in (USER, SYSTEM, AI):
        assert make_project(actor=actor).created_by == actor


@pytest.mark.parametrize("field", ["script_id", "audio_artifact_id"])
def test_ids_must_not_be_empty(field) -> None:
    with pytest.raises(ValueError, match=field):
        make_project(**{field: " "})


@pytest.mark.parametrize(
    "field",
    ["script_version", "audio_version", "audio_duration_ms", "strategy_version"],
)
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "2"])
def test_numbers_must_be_whole_and_positive(field, value) -> None:
    with pytest.raises(ValueError):
        make_project(**{field: value})


@pytest.mark.parametrize("sha256", ["", "a" * 63, "A" * 64, "g" * 64, None])
def test_the_audio_checksum_must_be_lowercase_sha256(sha256) -> None:
    with pytest.raises(ValueError, match="audio_sha256"):
        make_project(audio_sha256=sha256)


def test_audio_longer_than_the_format_maximum_is_refused() -> None:
    assert make_project(audio_duration_ms=60_000).audio_duration_ms == 60_000
    with pytest.raises(ValueError, match="longer than"):
        make_project(audio_duration_ms=60_001)


@pytest.mark.parametrize(
    ("duration_ms", "within"),
    [(14_999, False), (15_000, True), (30_250, True), (60_000, True)],
)
def test_audio_shorter_than_the_format_minimum_is_only_reported(
    duration_ms, within
) -> None:
    project = make_project(audio_duration_ms=duration_ms)

    assert project.audio_within_target is within
    assert project.as_dict()["audio_within_target"] is within


def test_a_draft_must_have_no_scenes_and_a_planned_project_must_have_some() -> None:
    project = make_project()

    with pytest.raises(ValueError, match="draft"):
        dataclasses.replace(project, status=ShortsProjectStatus.PLANNED)
    scenes = dataclasses.replace(
        project.timeline, scenes=(Scene("s1", 0, 0, 30250, None),)
    )
    with pytest.raises(ValueError, match="draft"):
        dataclasses.replace(project, timeline=scenes)
    planned = dataclasses.replace(
        project, timeline=scenes, status=ShortsProjectStatus.PLANNED
    )
    assert planned.status is ShortsProjectStatus.PLANNED


def test_the_timeline_must_be_as_long_as_the_audio() -> None:
    with pytest.raises(ValueError, match="as long as the audio"):
        dataclasses.replace(make_project(), timeline=Timeline.empty(30251))


def test_the_parts_must_have_their_types() -> None:
    project = make_project()

    with pytest.raises(TypeError):
        dataclasses.replace(project, config={})
    with pytest.raises(TypeError):
        dataclasses.replace(project, timeline={})
    with pytest.raises(TypeError):
        dataclasses.replace(project, status="draft")
    with pytest.raises(TypeError):
        dataclasses.replace(project, created_by="owner-1")
    with pytest.raises(ValueError):
        dataclasses.replace(project, updated_by=Actor(ActorKind.USER, " "))


def test_times_must_be_utc_and_ordered() -> None:
    project = make_project()

    with pytest.raises(ValueError, match="UTC"):
        dataclasses.replace(project, created_at=T1.replace(tzinfo=None))
    with pytest.raises(ValueError, match="earlier"):
        dataclasses.replace(project, updated_at=T0)


def test_rebind_is_an_empty_draft_of_the_next_version() -> None:
    project = make_project()

    again = project.rebind(
        script_id="sc2",
        script_version=3,
        audio_artifact_id="ar2",
        audio_version=4,
        audio_sha256="b" * 64,
        audio_duration_ms=40_000,
        strategy_version=5,
        config=ShortsConfig.from_format(ShortsFormat(30, 90)),
        actor=AI,
        clock=at(T2),
    )

    assert again.id == project.id and again.content_item_id == "ci1"
    assert again.version == 2
    assert (again.script_id, again.script_version) == ("sc2", 3)
    assert (again.audio_artifact_id, again.audio_version) == ("ar2", 4)
    assert again.audio_sha256 == "b" * 64
    assert again.audio_duration_ms == 40_000
    assert again.strategy_version == 5
    assert again.config.max_seconds == 90
    assert again.timeline == Timeline.empty(40_000)
    assert again.status is ShortsProjectStatus.DRAFT
    assert again.created_by == USER and again.created_at == T1
    assert again.updated_by == AI and again.updated_at == T2


def test_rebind_refuses_audio_longer_than_the_new_maximum() -> None:
    with pytest.raises(ValueError, match="longer than"):
        make_project().rebind(**entity_args(audio_duration_ms=61_000))


def test_with_timeline_keeps_the_binding_and_counts_a_version() -> None:
    project = make_project()
    timeline = dataclasses.replace(
        project.timeline, timing_source=TimingSource.AUDIO_CHUNKS
    )

    changed = project.with_timeline(
        timeline, ShortsProjectStatus.DRAFT, actor=SYSTEM, clock=at(T2)
    )

    assert changed.timeline == timeline
    assert changed.version == 2
    assert changed.updated_by == SYSTEM and changed.updated_at == T2
    assert changed.script_id == project.script_id
    assert changed.audio_artifact_id == project.audio_artifact_id


def test_with_timeline_needs_a_timeline_of_the_same_audio() -> None:
    project = make_project()

    with pytest.raises(ValueError, match="30251 ms"):
        project.with_timeline(
            Timeline.empty(30251), ShortsProjectStatus.DRAFT, actor=USER
        )
    with pytest.raises(TypeError):
        project.with_timeline({}, ShortsProjectStatus.DRAFT, actor=USER)


def test_as_dict_is_plain_data() -> None:
    project = make_project()
    data = project.as_dict()

    assert json.loads(json.dumps(data)) == data
    assert data["id"] == project.id
    assert data["status"] == "draft"
    assert data["version"] == 1
    assert data["config"] == project.config.to_dict()
    assert data["timeline"] == project.timeline.to_dict()
    assert data["timeline_sha256"] == project.timeline.sha256
    assert data["created_by"] == {"kind": "user", "id": "owner-1"}
    assert data["created_at"] == T1.isoformat()
    assert data["script_version"] == 2 and data["audio_version"] == 3


# The repository


def test_a_project_round_trips_through_the_database(world: World) -> None:
    world.prepare()
    project = world.entity()

    world.save(project)

    assert world.project_of() == project


def test_unknown_projects_are_none(world: World) -> None:
    with world.database.transaction() as connection:
        repository = ShortsProjectRepository(connection)

        assert repository.get("missing") is None
        assert repository.get_by_content_item(world.item.id) is None
        assert repository.list_by_channel(world.channel.id) == []


def test_get_finds_a_project_by_id_and_by_item(world: World) -> None:
    world.prepare()
    project = world.entity()
    world.save(project)

    with world.database.transaction() as connection:
        repository = ShortsProjectRepository(connection)

        assert repository.get(project.id) == project
        assert repository.get_by_content_item(world.item.id) == project


def test_list_by_channel_keeps_creation_order_and_the_channel(
    world: World, database: Database
) -> None:
    second = world.add_item()
    elsewhere = World(database)
    world.prepare()
    world.prepare(second)
    elsewhere.prepare()
    first_project = world.entity(world.item)
    later = T1 + timedelta(minutes=1)
    second_project = world.entity(second, created_at=later, updated_at=later)
    world.save(second_project)
    world.save(first_project)
    elsewhere.save(elsewhere.entity())

    with database.transaction() as connection:
        listed = ShortsProjectRepository(connection).list_by_channel(world.channel.id)

    assert [project.id for project in listed] == [first_project.id, second_project.id]


def test_update_changes_the_row_and_keeps_its_origin(world: World) -> None:
    world.prepare()
    project = world.entity()
    world.save(project)
    changed = project.with_timeline(
        dataclasses.replace(project.timeline, timing_source=TimingSource.AUDIO_CHUNKS),
        ShortsProjectStatus.DRAFT,
        actor=AI,
        clock=at(T2),
    )

    with world.database.transaction() as connection:
        ShortsProjectRepository(connection).update(changed, expected_version=1)

    stored = world.project_of()
    assert stored == changed
    assert stored.version == 2
    assert stored.created_by == USER and stored.created_at == T1
    assert stored.updated_by == AI and stored.updated_at == T2


def test_update_with_an_old_version_is_refused_and_changes_nothing(
    world: World,
) -> None:
    world.prepare()
    project = world.entity()
    world.save(project)
    changed = project.with_timeline(
        project.timeline, ShortsProjectStatus.DRAFT, actor=AI, clock=at(T2)
    )

    with world.database.transaction() as connection:
        with pytest.raises(ConcurrencyError):
            ShortsProjectRepository(connection).update(changed, expected_version=7)

    assert world.project_of() == project


def test_update_of_an_unknown_project_is_refused(world: World) -> None:
    world.prepare()
    project = world.entity()

    with world.database.transaction() as connection:
        with pytest.raises(RecordNotFoundError):
            ShortsProjectRepository(connection).update(project, expected_version=1)


def test_an_item_has_one_project(world: World) -> None:
    world.prepare()
    world.save(world.entity())

    with pytest.raises(sqlite3.IntegrityError):
        world.save(world.entity())

    assert world.count() == 1


def test_a_project_needs_its_script_and_audio_rows(world: World) -> None:
    world.prepare()

    with pytest.raises(sqlite3.IntegrityError):
        world.save(world.entity(script_id="no-such-script"))
    with pytest.raises(sqlite3.IntegrityError):
        world.save(world.entity(audio_artifact_id="no-such-artifact"))
    assert world.count() == 0


def test_the_configuration_is_stored_as_json_with_its_version(world: World) -> None:
    world.prepare()
    world.save(world.entity())

    with world.database.transaction() as connection:
        (format_json, timeline_json) = connection.execute(
            "SELECT format_json, timeline_json FROM shorts_projects"
        ).fetchone()

    assert json.loads(format_json)["config_version"] == 1
    assert json.loads(format_json)["max_seconds"] == 60
    assert timeline_json == Timeline.empty(30250).to_json()


# The migration (0027)


def row_for(world: World, **overrides) -> dict:
    script = world.latest_script()
    with world.database.transaction() as connection:
        artifact = ArtifactRepository(connection).latest(
            world.item.id, ArtifactKind.AUDIO
        )
    stamp = format_datetime(T1)
    row = {
        "id": "sp1",
        "content_item_id": world.item.id,
        "script_id": script.id,
        "script_version": script.version,
        "audio_artifact_id": artifact.id,
        "audio_version": artifact.version,
        "audio_sha256": artifact.sha256,
        "audio_duration_ms": 30250,
        "strategy_version": 1,
        "format_json": json.dumps({"max_seconds": 60}),
        "timeline_json": Timeline.empty(30250).to_json(),
        "status": "draft",
        "version": 1,
        "created_by_kind": "user",
        "created_by_id": "owner-1",
        "created_at": stamp,
        "updated_by_kind": "user",
        "updated_by_id": "owner-1",
        "updated_at": stamp,
    }
    return row | overrides


def insert_row(world: World, **overrides) -> None:
    row = row_for(world, **overrides)
    with world.database.transaction() as connection:
        connection.execute(
            f"INSERT INTO shorts_projects ({', '.join(row)}) "
            f"VALUES ({', '.join('?' for _ in row)})",
            tuple(row.values()),
        )


def timeline_text(duration_ms: int = 30250, scenes: int = 0, **extra) -> str:
    data = Timeline.empty(duration_ms).to_dict() | extra
    data["scenes"] = [{"id": f"s{n}"} for n in range(scenes)]
    return json.dumps(data)


def test_a_valid_row_is_accepted(world: World) -> None:
    world.prepare()

    insert_row(world)

    assert world.count() == 1


def test_a_planned_or_ready_row_with_scenes_is_accepted(world: World) -> None:
    world.prepare()

    insert_row(world, status="planned", timeline_json=timeline_text(scenes=2))

    assert world.count() == 1
    with world.database.transaction() as connection:
        connection.execute("UPDATE shorts_projects SET status = 'ready'")


def test_the_length_limits_are_accepted(world: World) -> None:
    world.prepare()

    insert_row(
        world,
        audio_duration_ms=60_000,
        timeline_json=timeline_text(60_000),
    )

    assert world.count() == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"script_version": 0},
        {"audio_version": 0},
        {"audio_sha256": "a" * 63},
        {"audio_sha256": "A" * 64},
        {"audio_sha256": "g" * 64},
        {"audio_duration_ms": 0, "timeline_json": timeline_text(1)},
        {"strategy_version": 0},
        {"format_json": "not json"},
        {"format_json": "[]"},
        {"format_json": "{}"},
        {"format_json": '{"max_seconds": "60"}'},
        {"format_json": '{"max_seconds": 60.5}'},
        {"timeline_json": "not json"},
        {"timeline_json": "[]"},
        {"timeline_json": '{"duration_ms": 30250, "scenes": []}'},
        {"timeline_json": json.dumps({"schema_version": 1, "duration_ms": 30250})},
        {
            "timeline_json": json.dumps(
                Timeline.empty(30250).to_dict() | {"scenes": {}}
            )
        },
        {"timeline_json": '{"schema_version": 1, "scenes": []}'},
        {"timeline_json": timeline_text(30251)},
        {"audio_duration_ms": 60_001, "timeline_json": timeline_text(60_001)},
        {"status": "bogus"},
        {"status": "draft", "timeline_json": timeline_text(scenes=1)},
        {"status": "planned"},
        {"status": "ready"},
        {"version": 0},
        {"created_by_kind": "robot"},
        {"updated_by_kind": "robot"},
        {"created_by_id": "  "},
        {"updated_by_id": ""},
        {"created_at": "2026-09-30T08:00:00Z"},
        {"updated_at": "2026-09-30"},
        {"updated_at": "2026-09-30T07:00:00.000000Z"},
        {"content_item_id": "no-such-item"},
        {"script_id": "no-such-script"},
        {"audio_artifact_id": "no-such-artifact"},
    ],
)
def test_the_rules_are_enforced_in_sql(world: World, overrides) -> None:
    world.prepare()

    with pytest.raises(sqlite3.IntegrityError):
        insert_row(world, **overrides)

    assert world.count() == 0


def test_a_row_cannot_be_changed_into_a_contradiction(world: World) -> None:
    world.prepare()
    insert_row(world)

    with world.database.transaction() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE shorts_projects SET status = 'planned'")

    with world.database.transaction() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE shorts_projects SET timeline_json = ?",
                (timeline_text(scenes=1),),
            )


def test_the_content_item_is_unique(world: World) -> None:
    world.prepare()
    insert_row(world)

    with pytest.raises(sqlite3.IntegrityError):
        insert_row(world, id="sp2")


# The service: create


def test_create_binds_the_latest_script_and_audio(world: World) -> None:
    script, audio = world.prepare(duration_ms=31_000)

    project = world.service.create(world.item.id, actor=USER)

    assert project.content_item_id == world.item.id
    assert (project.script_id, project.script_version) == (script.id, 1)
    assert (project.audio_artifact_id, project.audio_version) == (audio.id, 1)
    assert project.audio_sha256 == audio.sha256
    assert project.audio_duration_ms == 31_000
    assert project.strategy_version == world.strategy.version
    assert project.config == ShortsConfig.from_format(STANDARD)
    assert project.status is ShortsProjectStatus.DRAFT
    assert project.version == 1
    assert project.timeline == Timeline.empty(31_000)
    assert project.created_by == USER
    assert project.created_at == T1
    assert world.project_of() == project


def test_create_binds_the_newest_versions_when_there_are_several(world: World) -> None:
    world.prepare()
    script, audio = world.prepare()

    project = world.service.create(world.item.id, actor=USER)

    assert (project.script_id, project.script_version) == (script.id, 2)
    assert (project.audio_artifact_id, project.audio_version) == (audio.id, 2)


def test_create_copies_the_strategy_format(database: Database) -> None:
    world = World(database, ShortsFormat(30, 45))
    world.prepare(duration_ms=40_000)

    project = world.service.create(world.item.id, actor=USER)

    assert (project.config.min_seconds, project.config.max_seconds) == (30, 45)


def test_create_audits_ids_and_counts_only(world: World) -> None:
    world.prepare()

    project = world.service.create(world.item.id, actor=USER)

    (event,) = world.events()
    assert event.action == CREATED == "shorts.project_created"
    assert event.actor == USER
    assert (event.entity.type, event.entity.id) == ("shorts_project", project.id)
    assert event.metadata == {
        "content_item_id": world.item.id,
        "script_id": project.script_id,
        "script_version": 1,
        "audio_artifact_id": project.audio_artifact_id,
        "audio_version": 1,
        "audio_duration_ms": 30250,
        "strategy_version": world.strategy.version,
        "status": "draft",
        "version": 1,
    }
    assert "bank fees" not in repr(event)


def test_create_does_not_touch_the_content_item(world: World) -> None:
    world.prepare()
    world.service.create(world.item.id, actor=USER)

    with world.database.transaction() as connection:
        stored = ContentItemRepository(connection).get(world.item.id)

    assert stored == world.item
    assert stored.status is ContentStatus.DRAFT


def test_create_accepts_audio_at_the_limits(world: World) -> None:
    world.prepare(duration_ms=60_000)

    assert world.service.create(world.item.id, actor=USER).audio_within_target


def test_create_accepts_short_audio_and_reports_it(world: World) -> None:
    world.prepare(duration_ms=5_000)

    project = world.service.create(world.item.id, actor=USER)

    assert project.audio_duration_ms == 5_000
    assert project.audio_within_target is False


def test_create_refuses_audio_longer_than_the_maximum(world: World) -> None:
    world.prepare(duration_ms=60_001)

    refuse_create(world, Code.AUDIO_TOO_LONG)

    assert world.count() == 0
    assert world.events() == ()


def test_create_refuses_an_unknown_item(world: World) -> None:
    refuse_create(world, Code.ITEM_NOT_FOUND, "missing")


def test_create_refuses_a_long_form_item(world: World) -> None:
    item = world.add_item(content_type=ContentType.LONGFORM)
    world.prepare(item)

    refuse_create(world, Code.NOT_SHORTS, item.id)

    assert world.count() == 0


def test_create_needs_a_strategy_format(database: Database) -> None:
    world = World(database, format_settings=False)
    world.prepare()

    refuse_create(world, Code.STRATEGY_MISSING)


def test_create_needs_a_script(world: World) -> None:
    refuse_create(world, Code.SCRIPT_MISSING)


def test_create_needs_audio(world: World) -> None:
    world.add_script()

    refuse_create(world, Code.AUDIO_MISSING)


def test_create_needs_the_details_of_the_audio(world: World) -> None:
    script = world.add_script()
    world.add_audio(script, metadata=False)

    refuse_create(world, Code.AUDIO_MISSING)


def test_create_refuses_audio_of_an_older_script(world: World) -> None:
    world.prepare()
    world.add_script()

    error = refuse_create(world, Code.STALE_INPUT)

    assert "latest script" in error.detail
    assert world.count() == 0
    assert world.events() == ()


def test_create_works_again_once_the_new_script_is_voiced(world: World) -> None:
    world.prepare()
    script = world.add_script()
    refuse_create(world, Code.STALE_INPUT)

    world.add_audio(script)

    project = world.service.create(world.item.id, actor=USER)
    assert (project.script_version, project.audio_version) == (2, 2)


def test_an_item_gets_only_one_project(world: World) -> None:
    world.prepare()
    first = world.service.create(world.item.id, actor=USER)

    refuse_create(world, Code.EXISTS)

    assert world.count() == 1
    assert world.project_of() == first
    assert len(world.events()) == 1


def test_a_concurrent_create_is_reported_as_existing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.prepare()
    stored = world.entity()
    world.save(stored)
    original = ShortsProjectRepository.get_by_content_item
    calls = []

    def first_call_sees_nothing(self, content_item_id):
        calls.append(content_item_id)
        return None if len(calls) == 1 else original(self, content_item_id)

    monkeypatch.setattr(
        ShortsProjectRepository, "get_by_content_item", first_call_sees_nothing
    )

    refuse_create(world, Code.EXISTS)

    assert world.count() == 1
    assert world.events() == ()


# The service: rebind


def test_rebind_takes_the_new_latest_inputs_as_an_empty_draft(world: World) -> None:
    world.prepare()
    project = world.create()
    script, audio = world.prepare(duration_ms=20_000)

    again = world.service.rebind(project.id, expected_version=1, actor=AI)

    assert again.id == project.id
    assert again.version == 2
    assert (again.script_id, again.script_version) == (script.id, 2)
    assert (again.audio_artifact_id, again.audio_version) == (audio.id, 2)
    assert again.audio_sha256 == audio.sha256
    assert again.audio_duration_ms == 20_000
    assert again.timeline == Timeline.empty(20_000)
    assert again.status is ShortsProjectStatus.DRAFT
    assert again.created_by == USER
    assert again.updated_by == AI
    assert world.project_of() == again


def test_rebind_audits_the_change(world: World) -> None:
    project = world.create()
    world.prepare()

    again = world.service.rebind(project.id, expected_version=1, actor=AI)

    event = world.events()[-1]
    assert event.action == REBOUND == "shorts.project_rebound"
    assert event.actor == AI
    assert event.entity.id == project.id
    assert event.metadata["version"] == 2
    assert event.metadata["previous_version"] == 1
    assert event.metadata["script_version"] == again.script_version == 2
    assert event.metadata["audio_duration_ms"] == again.audio_duration_ms


def test_rebind_forgets_a_saved_timeline(world: World) -> None:
    project = world.create()
    saved = world.service.save_timeline(
        project.id,
        dataclasses.replace(project.timeline, timing_source=TimingSource.AUDIO_CHUNKS),
        expected_version=1,
        actor=USER,
    )
    world.prepare()

    again = world.service.rebind(saved.id, expected_version=2, actor=USER)

    assert again.version == 3
    assert again.timeline.timing_source is TimingSource.ESTIMATED


def test_rebind_with_the_same_inputs_still_counts_a_version(world: World) -> None:
    project = world.create()

    again = world.service.rebind(project.id, expected_version=1, actor=USER)

    assert again.version == 2
    assert again.script_id == project.script_id


def test_rebind_needs_the_version_that_was_read(world: World) -> None:
    project = world.create()
    world.prepare()

    refusal(
        lambda: world.service.rebind(project.id, expected_version=2, actor=USER),
        Code.CONFLICT,
    )

    assert world.project_of() == project
    assert len(world.events()) == 1


def test_rebind_refuses_an_unknown_project(world: World) -> None:
    refusal(
        lambda: world.service.rebind("missing", expected_version=1, actor=USER),
        Code.NOT_FOUND,
    )


def test_rebind_refuses_a_script_that_is_not_voiced_yet(world: World) -> None:
    project = world.create()
    world.add_script()

    refusal(
        lambda: world.service.rebind(project.id, expected_version=1, actor=USER),
        Code.STALE_INPUT,
    )

    assert world.project_of() == project


def test_rebind_refuses_audio_that_became_too_long(world: World) -> None:
    project = world.create()
    world.prepare(duration_ms=75_000)

    refusal(
        lambda: world.service.rebind(project.id, expected_version=1, actor=USER),
        Code.AUDIO_TOO_LONG,
    )

    assert world.project_of() == project


# The service: save_timeline


def test_save_timeline_keeps_an_empty_timeline_a_draft(world: World) -> None:
    project = world.create()
    timeline = dataclasses.replace(
        project.timeline, timing_source=TimingSource.AUDIO_CHUNKS
    )

    saved = world.service.save_timeline(
        project.id, timeline, expected_version=1, actor=AI
    )

    assert saved.version == 2
    assert saved.status is ShortsProjectStatus.DRAFT
    assert saved.timeline == timeline
    assert saved.updated_by == AI and saved.updated_at == T1
    assert saved.script_id == project.script_id
    assert world.project_of() == saved


def test_save_timeline_audits_counts_and_the_checksum(world: World) -> None:
    project = world.create()

    saved = world.service.save_timeline(
        project.id, project.timeline, expected_version=1, actor=USER
    )

    event = world.events()[-1]
    assert event.action == TIMELINE_SAVED == "shorts.timeline_saved"
    assert event.entity.id == project.id
    assert event.metadata["timeline_sha256"] == saved.timeline.sha256
    assert event.metadata["scene_count"] == 0
    assert event.metadata["caption_count"] == 0
    assert event.metadata["music_count"] == 0
    assert event.metadata["status"] == "draft"
    assert event.metadata["version"] == 2


def test_save_timeline_needs_the_version_that_was_read(world: World) -> None:
    project = world.create()

    refusal(
        lambda: world.service.save_timeline(
            project.id, project.timeline, expected_version=5, actor=USER
        ),
        Code.CONFLICT,
    )

    assert world.project_of() == project


def test_two_saves_from_the_same_version_cannot_both_win(world: World) -> None:
    project = world.create()
    world.service.save_timeline(
        project.id, project.timeline, expected_version=1, actor=USER
    )

    refusal(
        lambda: world.service.save_timeline(
            project.id, project.timeline, expected_version=1, actor=AI
        ),
        Code.CONFLICT,
    )

    assert world.project_of().version == 2


def test_save_timeline_refuses_an_unknown_project(world: World) -> None:
    refusal(
        lambda: world.service.save_timeline(
            "missing", Timeline.empty(1000), expected_version=1, actor=USER
        ),
        Code.NOT_FOUND,
    )


def test_save_timeline_needs_the_length_of_the_audio(world: World) -> None:
    project = world.create()

    refusal(
        lambda: world.service.save_timeline(
            project.id, Timeline.empty(30251), expected_version=1, actor=USER
        ),
        Code.TIMELINE_INVALID,
    )

    assert world.project_of() == project


def test_save_timeline_refuses_what_is_not_a_timeline(world: World) -> None:
    project = world.create()

    refusal(
        lambda: world.service.save_timeline(
            project.id, project.timeline.to_dict(), expected_version=1, actor=USER
        ),
        Code.TIMELINE_INVALID,
    )


def test_save_timeline_refuses_captions_in_a_draft(world: World) -> None:
    # A draft has no scenes, and captions need scenes (I-097).
    project = world.create()
    caption = CaptionOverlay("c1", "s1", "Hi", 0, 900)

    with pytest.raises(ValueError, match="need scenes"):
        dataclasses.replace(project.timeline, captions=(caption,))

    assert world.project_of() == project
    assert len(world.events()) == 1


# The service: reads


def test_get_returns_the_stored_project(world: World) -> None:
    project = world.create()

    assert world.service.get(project.id) == project


def test_get_refuses_an_unknown_project(world: World) -> None:
    refusal(lambda: world.service.get("missing"), Code.NOT_FOUND)


def test_get_by_content_item_returns_the_project_or_none(world: World) -> None:
    other = world.add_item()
    project = world.create()

    assert world.service.get_by_content_item(world.item.id) == project
    assert world.service.get_by_content_item(other.id) is None


def test_get_by_content_item_refuses_an_unknown_item(world: World) -> None:
    refusal(lambda: world.service.get_by_content_item("missing"), Code.ITEM_NOT_FOUND)


def test_list_by_channel_lists_the_projects_of_the_channel(
    world: World, database: Database
) -> None:
    second = world.add_item()
    first_project = world.create()
    elsewhere = World(database)
    elsewhere.create()

    assert world.service.list_by_channel(world.channel.id) == [first_project]
    second_project = world.create(second)
    assert world.service.list_by_channel(world.channel.id) == [
        first_project,
        second_project,
    ]
    assert world.service.list_by_channel("missing") == []


def test_is_current_follows_the_latest_script_and_audio(world: World) -> None:
    project = world.create()
    assert world.service.is_current(project.id) is True

    script = world.add_script()
    assert world.service.is_current(project.id) is False

    world.add_audio(script)
    assert world.service.is_current(project.id) is False

    again = world.service.rebind(project.id, expected_version=1, actor=USER)
    assert world.service.is_current(again.id) is True


def test_is_current_notices_new_audio_of_the_same_script(world: World) -> None:
    project = world.create()

    world.add_audio(world.latest_script())

    assert world.service.is_current(project.id) is False


def test_is_current_refuses_an_unknown_project(world: World) -> None:
    refusal(lambda: world.service.is_current("missing"), Code.NOT_FOUND)


# Readiness


def test_an_empty_timeline_is_a_draft() -> None:
    config = ShortsConfig.from_format(STANDARD)

    assert readiness(config, Timeline.empty(1000)) is ShortsProjectStatus.DRAFT


def test_scenes_that_cover_the_audio_are_planned() -> None:
    config = ShortsConfig.from_format(STANDARD)
    timeline = Timeline(
        1, 1000, TimingSource.ESTIMATED, (Scene("s1", 0, 0, 1000, None),)
    )

    assert readiness(config, timeline) is ShortsProjectStatus.PLANNED


def test_readiness_of_a_timeline_without_scenes_is_a_draft_with_captions_on() -> None:
    config = ShortsConfig.from_format(STANDARD)

    assert readiness(config, Timeline.empty(1000)) is ShortsProjectStatus.DRAFT


# Errors


def test_every_error_code_has_a_message_and_a_domain_code() -> None:
    assert set(USER_MESSAGES) == set(Code)
    for code in Code:
        assert code.value.startswith("domain.shorts_")
    assert len({code.value for code in Code}) == len(Code)


def test_errors_carry_their_code_and_status() -> None:
    not_found = ShortsProjectError("x", code=Code.NOT_FOUND)
    conflict = ShortsProjectError("x", code=Code.CONFLICT)
    plain = ShortsProjectError("x", code=Code.AUDIO_TOO_LONG)

    assert not_found.to_public().http_status == 404
    assert conflict.to_public().http_status == 409
    assert plain.to_public().http_status == 422
    assert set(HTTP_STATUS) <= set(Code)
    assert ShortsProjectError().code == Code.PROJECT.value


def test_the_public_error_hides_the_detail() -> None:
    error = ShortsProjectError("internal detail 123", code=Code.CONFLICT)

    public = to_public(error)

    assert public.code == "domain.shorts_project_conflict"
    assert "internal detail" not in public.message
    assert public.message == USER_MESSAGES[Code.CONFLICT]
