"""F-073 Script Versioning: the versioner, its storage and its history.

Rules the user approved on 2026-10-04 (the diff itself is tested in
``test_script_revision.py``):

- a revision is made on demand for a version after the first, against its
  parent only: an unknown script is ``ScriptNotFoundError`` (404); version 1, no
  parent, a missing parent or a parent of another content item is
  ``ScriptInputError`` (422); any actor may record one;
- one revision per script version, never recomputed: a stored revision is
  returned before anything is computed, without writing or auditing; the
  revision and its entries are stored in one transaction (migration 0022);
  ``script.revision_recorded`` is audited after commit with ids and counts only;
- no script text is stored or audited;
- ``history`` reads the versions of a content item in order with the revision
  summary of each, and writes nothing;
- no relation to approvals or to the #068 to #072 runs, and no change to the
  script, artifact or approval rows.
"""

import dataclasses
import sqlite3
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path

import pytest

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content import script_versioner
from ai_youtube_agent.content.claim_extractor import (
    ClaimExtractor,
    ScriptNotFoundError,
)
from ai_youtube_agent.content.hook_generator import ContentItemNotFoundError
from ai_youtube_agent.content.script import Script, ScriptSection, SectionKind
from ai_youtube_agent.content.script_generation import ScriptInputError
from ai_youtube_agent.content.script_revision import (
    SCRIPT_DIFF_METHOD,
    ChangeKind,
    ScriptHistoryEntry,
    ScriptRevision,
    SectionChange,
    content_sha256,
    section_sha256,
)
from ai_youtube_agent.content.script_validator import ScriptValidator
from ai_youtube_agent.content.script_versioner import ScriptVersioner
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.content_item import ContentItem
from ai_youtube_agent.core.db.codec import format_datetime
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.migrate import default_migrations, migrate
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ClaimExtractionRepository,
    ContentItemRepository,
    ScriptRepository,
    ScriptRevisionRepository,
)
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from factories import (
    make_approval_request,
    make_artifact,
    make_channel,
    make_content_item,
    make_strategy_profile,
)

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner")
AI = Actor(ActorKind.AI, "mock/mock-1")
SYSTEM = Actor(ActorKind.SYSTEM, "pipeline")
HOOK, BODY, CHAPTER, CTA = (
    SectionKind.HOOK,
    SectionKind.BODY,
    SectionKind.CHAPTER,
    SectionKind.CTA,
)
ADDED, REMOVED, CHANGED, UNCHANGED = (
    ChangeKind.ADDED,
    ChangeKind.REMOVED,
    ChangeKind.CHANGED,
    ChangeKind.UNCHANGED,
)
SECRET = "zebrafish"  # a word of the script that no row or event may hold
HOOK_TEXT = f"A {SECRET} hook."
BODY_TEXT = f"The {SECRET} body."
CTA_TEXT = "Follow please."


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def good(body: str = BODY_TEXT) -> tuple[ScriptSection, ...]:
    return (
        ScriptSection(HOOK, HOOK_TEXT, seconds=5),
        ScriptSection(BODY, body, seconds=30),
        ScriptSection(CTA, CTA_TEXT, seconds=5),
    )


class World:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.clock = Clock()
        self.channel = make_channel()
        self.strategy = make_strategy_profile(self.channel)
        with database.transaction() as connection:
            ChannelRepository(connection).add(self.channel)
            StrategyProfileRepository(connection).add(self.strategy)
        self.item = self.new_item()
        self.sink = InMemoryAuditSink()
        self.versioner = ScriptVersioner(
            database, AuditLog(self.sink), clock=self.clock
        )

    def new_item(self) -> ContentItem:
        item = make_content_item(self.channel, self.strategy)
        with self.database.transaction() as connection:
            ContentItemRepository(connection).add(item)
        return item

    def script(
        self, sections: tuple[ScriptSection, ...] | None = None, item=None
    ) -> Script:
        script = Script.create(
            (item or self.item).id,
            sections=sections or good(),
            created_by=AI,
            strategy_version=self.strategy.version,
            clock=self.clock,
        )
        self.add(script)
        return script

    def revise(self, script: Script, sections: tuple[ScriptSection, ...]) -> Script:
        new = script.next_version(sections=sections, created_by=AI, clock=self.clock)
        self.add(new)
        return new

    def add(self, script: Script, *, foreign_keys: bool = True) -> None:
        if foreign_keys:
            with self.database.transaction() as connection:
                ScriptRepository(connection).add(script)
            return
        connection = self.database.connect()
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            ScriptRepository(connection).add(script)
        finally:
            connection.close()

    def stored(self, script_id: str) -> ScriptRevision | None:
        with self.database.transaction() as connection:
            return ScriptRevisionRepository(connection).get_by_script(script_id)

    def count(self, table: str) -> int:
        with self.database.transaction() as connection:
            return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def rows(self, *tables: str) -> list[tuple]:
        with self.database.transaction() as connection:
            return [
                tuple(row)
                for table in tables
                for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]


@pytest.fixture
def world(database: Database) -> World:
    return World(database)


def pair(
    world: World, body: str = f"The {SECRET} body, changed."
) -> tuple[Script, Script]:
    first = world.script()
    return first, world.revise(first, good(body))


# The revision


def test_a_revision_is_stored_and_audited(world: World) -> None:
    first, second = pair(world)

    revision = world.versioner.record(second.id, actor=USER)

    assert (revision.script_id, revision.parent_script_id) == (second.id, first.id)
    assert (revision.content_item_id, revision.version) == (world.item.id, 2)
    assert revision.method == SCRIPT_DIFF_METHOD == "script-diff-v1"
    assert revision.requested_by == USER
    assert (revision.added_count, revision.removed_count) == (0, 0)
    assert (revision.changed_count, revision.unchanged_count) == (1, 2)
    assert revision.entries_count == 3
    assert (revision.words_before, revision.words_after) == (8, 9)
    assert (revision.seconds_before, revision.seconds_after) == (40, 40)
    assert revision.content_sha256 == content_sha256(second.sections)
    assert [e.change for e in revision.entries] == [UNCHANGED, CHANGED, UNCHANGED]
    assert revision.entries[1].sha256 == section_sha256(second.sections[1])

    assert world.stored(second.id) == revision
    with world.database.transaction() as connection:
        assert ScriptRevisionRepository(connection).get(revision.id) == revision
    assert world.stored(first.id) is None  # version 1 has none

    [event] = world.sink.events()
    assert (event.action, event.result, event.actor) == (
        "script.revision_recorded",
        AuditResult.SUCCESS,
        USER,
    )
    assert (event.entity.type, event.entity.id) == ("script", second.id)
    assert dict(event.metadata) == {
        "script_revision_id": revision.id,
        "parent_script_id": first.id,
        "content_item_id": world.item.id,
        "version": 2,
        "method": "script-diff-v1",
        "added": 0,
        "removed": 0,
        "changed": 1,
        "unchanged": 2,
        "words_before": 8,
        "words_after": 9,
        "seconds_before": 40,
        "seconds_after": 40,
    }


def test_the_revision_is_made_against_the_parent_only(world: World) -> None:
    first = world.script()
    second = world.revise(
        first, good() + (ScriptSection(CTA, "Subscribe too.", seconds=5),)
    )
    third = world.revise(
        second,
        good()
        + (ScriptSection(CTA, "Subscribe too.", seconds=5),)
        + (ScriptSection(CTA, "And share.", seconds=5),),
    )

    revision = world.versioner.record(third.id, actor=USER)

    assert revision.parent_script_id == second.id
    assert (revision.added_count, revision.unchanged_count) == (1, 4)
    assert (revision.words_before, revision.words_after) == (10, 12)
    # Nothing was recorded for the version in between.
    assert world.stored(second.id) is None


@pytest.mark.parametrize("actor", [USER, AI, SYSTEM])
def test_any_actor_may_record_a_revision(world: World, actor: Actor) -> None:
    _, second = pair(world)

    revision = world.versioner.record(second.id, actor=actor)

    assert revision.requested_by == actor
    [event] = world.sink.events()
    assert event.actor == actor


def test_the_revision_time_comes_from_the_clock(world: World) -> None:
    _, second = pair(world)
    expected = world.clock.now + timedelta(seconds=1)

    assert world.versioner.record(second.id, actor=USER).created_at == expected


def test_a_revision_with_no_change_is_allowed(world: World) -> None:
    first = world.script()
    twin = dataclasses.replace(
        first,
        id="twin",
        version=2,
        parent_id=first.id,
        created_at=T0 + timedelta(hours=1),
    )
    world.add(twin)

    revision = world.versioner.record(twin.id, actor=USER)

    assert (revision.changed_count, revision.unchanged_count) == (0, 3)
    assert revision.words_before == revision.words_after
    assert world.stored(twin.id) == revision


def test_every_kind_of_entry_is_stored_and_read_back(world: World) -> None:
    first = world.script()
    sections = (
        ScriptSection(HOOK, HOOK_TEXT, seconds=5),
        ScriptSection(CHAPTER, "A new chapter text.", "Chapter", seconds=20),
        ScriptSection(BODY, "A changed body text now.", seconds=31),
    )
    second = world.revise(first, sections)

    revision = world.versioner.record(second.id, actor=USER)

    assert [(e.change, e.old_index, e.new_index) for e in revision.entries] == [
        (UNCHANGED, 0, 0),
        (ADDED, None, 1),
        (CHANGED, 1, 2),
        (REMOVED, 2, None),
    ]
    assert world.stored(second.id) == revision
    [entry] = [e for e in revision.entries if e.change is CHANGED]
    assert (entry.text_changed, entry.seconds_changed) == (True, True)
    assert isinstance(entry, SectionChange)


def test_the_audit_and_the_rows_hold_no_script_text(world: World) -> None:
    first = world.script(
        (
            ScriptSection(HOOK, HOOK_TEXT, seconds=5),
            ScriptSection(BODY, BODY_TEXT, "Zebra title", seconds=30),
        )
    )
    second = world.revise(
        first,
        (
            ScriptSection(HOOK, f"A {SECRET} hook!", seconds=5),
            ScriptSection(BODY, f"Another {SECRET} body.", "Zebra title", seconds=30),
        ),
    )

    revision = world.versioner.record(second.id, actor=USER)

    [event] = world.sink.events()
    assert all(isinstance(value, str | int) for value in event.metadata.values())
    assert SECRET not in repr(event.metadata)
    rows = world.rows("script_revisions", "script_revision_sections")
    assert rows
    assert SECRET not in repr(rows) and "Zebra" not in repr(rows)
    assert SECRET not in repr(revision) and "Zebra" not in repr(revision)
    assert SECRET not in repr(world.versioner.history(world.item.id))


# Refusals


def test_version_one_has_no_revision(world: World) -> None:
    first = world.script()

    with pytest.raises(ScriptInputError) as caught:
        world.versioner.record(first.id, actor=USER)

    assert caught.value.code == "domain.script_input"
    assert caught.value.to_public().http_status == HTTPStatus.UNPROCESSABLE_ENTITY
    assert world.sink.events() == ()
    assert world.count("script_revisions") == 0


def test_an_unknown_script_is_not_found(world: World) -> None:
    with pytest.raises(ScriptNotFoundError) as caught:
        world.versioner.record("missing", actor=USER)

    assert caught.value.code == "domain.script_not_found"
    assert caught.value.to_public().http_status == HTTPStatus.NOT_FOUND
    assert world.sink.events() == ()
    assert world.count("script_revisions") == 0


def test_a_missing_parent_is_refused(world: World) -> None:
    first = world.script()
    child = dataclasses.replace(
        first, id="child", version=2, parent_id="ghost", sections=good("Other body.")
    )
    world.add(child, foreign_keys=False)

    with pytest.raises(ScriptInputError) as caught:
        world.versioner.record(child.id, actor=USER)

    assert caught.value.to_public().http_status == HTTPStatus.UNPROCESSABLE_ENTITY
    assert world.sink.events() == ()
    assert world.count("script_revisions") == 0


def test_a_parent_of_another_content_item_is_refused(world: World) -> None:
    mine = world.script()
    other = world.script(item=world.new_item())
    child = dataclasses.replace(
        mine,
        id="child",
        version=2,
        parent_id=other.id,
        sections=good("Other body."),
        created_at=T0 + timedelta(hours=1),
    )
    world.add(child)

    with pytest.raises(ScriptInputError):
        world.versioner.record(child.id, actor=USER)

    assert world.sink.events() == ()
    assert world.count("script_revisions") == 0


# The run


def test_the_audit_event_is_recorded_after_the_commit(world: World) -> None:
    _, second = pair(world)
    seen: list[ScriptRevision | None] = []

    class CheckingSink(InMemoryAuditSink):
        def append(self, event) -> None:
            # A fresh connection only sees committed data.
            seen.append(world.stored(second.id))
            super().append(event)

    revision = ScriptVersioner(world.database, AuditLog(CheckingSink())).record(
        second.id, actor=USER
    )

    assert seen == [revision]


def test_a_second_call_returns_the_stored_revision(world: World, monkeypatch) -> None:
    _, second = pair(world)
    first_call = world.versioner.record(second.id, actor=USER)
    rows = world.count("script_revision_sections")

    def not_computed(*args):
        raise AssertionError("the diff is not computed again")

    monkeypatch.setattr(script_versioner, "diff_scripts", not_computed)

    again = world.versioner.record(second.id, actor=AI)

    assert again == first_call
    assert again.requested_by == USER
    assert world.count("script_revision_sections") == rows
    assert world.count("script_revisions") == 1
    assert [e.action for e in world.sink.events()] == ["script.revision_recorded"]


def test_a_stored_revision_is_returned_before_the_parent_is_read(
    world: World, monkeypatch
) -> None:
    _, second = pair(world)
    stored = world.versioner.record(second.id, actor=USER)
    original = ScriptRepository.get

    def only_the_child(self, script_id):
        return original(self, script_id) if script_id == second.id else None

    monkeypatch.setattr(ScriptRepository, "get", only_the_child)

    assert world.versioner.record(second.id, actor=USER) == stored


def test_a_concurrent_revision_stored_first_is_returned(world, monkeypatch) -> None:
    _, second = pair(world)
    winner = world.versioner.record(second.id, actor=USER)
    rows = world.count("script_revision_sections")
    real = ScriptRevisionRepository.get_by_script
    calls: list[str] = []

    def racing(self, script_id):
        # The first read happens before the other revision is committed.
        calls.append(script_id)
        return None if len(calls) == 1 else real(self, script_id)

    monkeypatch.setattr(ScriptRevisionRepository, "get_by_script", racing)

    loser = world.versioner.record(second.id, actor=AI)

    assert loser == winner
    assert calls == [second.id, second.id]
    assert world.count("script_revision_sections") == rows
    assert [e.action for e in world.sink.events()] == ["script.revision_recorded"]


def test_a_failed_entry_insert_rolls_back_the_revision(world, monkeypatch) -> None:
    _, second = pair(world)
    real = ScriptRevisionRepository._insert
    inserted: list[str] = []

    def failing(self, table, values):
        if table == "script_revision_sections":
            inserted.append(values["id"])
            raise RuntimeError("disk full")
        real(self, table, values)

    monkeypatch.setattr(ScriptRevisionRepository, "_insert", failing)

    with pytest.raises(RuntimeError):
        world.versioner.record(second.id, actor=USER)

    assert inserted
    assert world.stored(second.id) is None
    assert (
        world.count("script_revisions"),
        world.count("script_revision_sections"),
    ) == (0, 0)
    assert world.sink.events() == ()


def test_other_integrity_errors_are_raised(world, monkeypatch) -> None:
    _, second = pair(world)

    def broken(self, revision):
        raise sqlite3.IntegrityError("broken")

    monkeypatch.setattr(ScriptRevisionRepository, "add", broken)

    with pytest.raises(sqlite3.IntegrityError):
        world.versioner.record(second.id, actor=USER)

    assert world.stored(second.id) is None
    assert world.sink.events() == ()


def test_a_revision_changes_no_other_row(world: World) -> None:
    first, second = pair(world)
    video = make_artifact(world.item, ArtifactKind.VIDEO)
    with world.database.transaction() as connection:
        ArtifactRepository(connection).add(video)
        ApprovalRequestRepository(connection).add(
            make_approval_request(world.item, [video])
        )
    tables = (
        "scripts",
        "content_items",
        "artifacts",
        "approval_requests",
        "approval_artifacts",
        "claims",
        "evidence",
        "strategy_profiles",
    )
    before = world.rows(*tables)
    assert world.count("approval_requests") == 1

    world.versioner.record(second.id, actor=USER)

    assert world.rows(*tables) == before
    with world.database.transaction() as connection:
        assert ScriptRepository(connection).get(second.id) == second


def test_a_revision_needs_no_other_run_and_reads_none(world, monkeypatch) -> None:
    _, second = pair(world)

    def not_read(self, *args):
        raise AssertionError("a run is not read")

    monkeypatch.setattr(ClaimExtractionRepository, "get_by_script", not_read)

    world.versioner.record(second.id, actor=USER)

    for table in (
        "claim_extractions",
        "evidence_matches",
        "fact_checks",
        "originality_checks",
        "script_validations",
    ):
        assert world.count(table) == 0


# The history


def test_the_history_lists_the_versions_in_order(world: World) -> None:
    first = world.script()
    second = world.revise(first, good("Second body text here."))
    third = world.revise(second, good("Third body text here now."))
    revision = world.versioner.record(second.id, actor=USER)

    history = world.versioner.history(world.item.id)

    assert [h.version for h in history] == [1, 2, 3]
    assert [h.script_id for h in history] == [first.id, second.id, third.id]
    assert [h.parent_id for h in history] == [None, first.id, second.id]
    assert all(isinstance(h, ScriptHistoryEntry) for h in history)
    assert [h.created_by for h in history] == [AI, AI, AI]
    assert [h.strategy_version for h in history] == [1, 1, 1]
    assert [h.reason for h in history] == [None, None, None]
    assert [(h.words, h.seconds) for h in history] == [(8, 40), (9, 40), (10, 40)]
    # Versions without a revision show none; the stored one shows its summary.
    assert [h.revision is None for h in history] == [True, False, True]
    summary = history[1].revision
    assert summary == revision.summary()
    assert (summary.changed, summary.unchanged) == (1, 2)
    assert [e.action for e in world.sink.events()] == ["script.revision_recorded"]


def test_the_history_keeps_the_reason_and_the_actor(world: World) -> None:
    first = world.script()
    second = first.next_version(
        sections=good("Reasoned body."),
        created_by=USER,
        reason="Tighter",
        clock=world.clock,
    )
    world.add(second)

    [one, two] = world.versioner.history(world.item.id)

    assert (one.created_by, one.reason) == (AI, None)
    assert (two.created_by, two.reason) == (USER, "Tighter")


def test_the_history_follows_the_versions_after_revisions_are_recorded(
    world: World,
) -> None:
    first = world.script()
    second = world.revise(first, good("Second body text here."))
    third = world.revise(second, good("Third body text here now."))
    world.versioner.record(third.id, actor=USER)
    world.versioner.record(second.id, actor=USER)  # recorded out of order

    history = world.versioner.history(world.item.id)

    assert [h.version for h in history] == [1, 2, 3]
    assert [h.revision is not None for h in history] == [False, True, True]


def test_the_history_of_an_item_without_a_script_is_empty(world: World) -> None:
    assert world.versioner.history(world.item.id) == []


def test_the_history_is_of_one_item(world: World) -> None:
    pair(world)
    other = world.new_item()
    world.script(item=other)

    assert len(world.versioner.history(other.id)) == 1
    assert len(world.versioner.history(world.item.id)) == 2


def test_the_history_of_an_unknown_item_is_not_found(world: World) -> None:
    with pytest.raises(ContentItemNotFoundError) as caught:
        world.versioner.history("missing")

    assert caught.value.to_public().http_status == HTTPStatus.NOT_FOUND


def test_the_history_writes_nothing(world: World) -> None:
    pair(world)
    tables = ("scripts", "script_revisions", "script_revision_sections")
    before = world.rows(*tables)

    world.versioner.history(world.item.id)

    assert world.rows(*tables) == before
    assert world.sink.events() == ()


def test_the_revisions_of_an_item_are_listed_by_version(world: World) -> None:
    first = world.script()
    second = world.revise(first, good("Second body text here."))
    third = world.revise(second, good("Third body text here now."))
    one = world.versioner.record(third.id, actor=USER)
    two = world.versioner.record(second.id, actor=USER)

    with world.database.transaction() as connection:
        listed = ScriptRevisionRepository(connection).list_by_content_item(
            world.item.id
        )
        assert ScriptRevisionRepository(connection).list_by_content_item("x") == []

    assert listed == [two, one]


def test_a_missing_revision_is_none(world: World) -> None:
    with world.database.transaction() as connection:
        repository = ScriptRevisionRepository(connection)
        assert repository.get("missing") is None
        assert repository.get_by_script("missing") is None


# Storage


def stored_revision(world: World) -> ScriptRevision:
    _, second = pair(world)
    return world.versioner.record(second.id, actor=USER)


def insert(world: World, table: str, row: dict) -> None:
    with world.database.transaction() as connection:
        connection.execute(
            f"INSERT INTO {table} ({', '.join(row)}) "
            f"VALUES ({', '.join('?' for _ in row)})",
            tuple(row.values()),
        )


def section_row(world: World, **change) -> dict:
    revision = stored_revision(world)
    row = {
        "id": "new",
        "revision_id": revision.id,
        "change": "added",
        "kind": "body",
        "old_index": None,
        "new_index": 7,
        "text_changed": 0,
        "title_changed": 0,
        "seconds_changed": 0,
        "words_delta": 3,
        "sha256": "a" * 64,
    }
    row.update(change)
    return row


PAIRED = dict(old_index=7, new_index=7)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (dict(), None),
        (dict(sha256=None), None),
        (dict(words_delta=0), None),
        (dict(words_delta=-1), "CHECK"),
        (dict(old_index=7), "CHECK"),
        (dict(new_index=None), "CHECK"),
        (dict(text_changed=1), "CHECK"),
        (dict(change="removed", old_index=7, new_index=None, words_delta=-3), None),
        (dict(change="removed", old_index=7, new_index=None, words_delta=1), "CHECK"),
        (dict(change="removed", old_index=7), "CHECK"),
        (dict(change="removed", old_index=None, new_index=None), "CHECK"),
        (dict(change="removed", old_index=7, new_index=None, title_changed=1), "CHECK"),
        (dict(change="changed", text_changed=1, words_delta=-2, **PAIRED), None),
        (dict(change="changed", title_changed=1, words_delta=0, **PAIRED), None),
        (dict(change="changed", seconds_changed=1, words_delta=0, **PAIRED), None),
        (dict(change="changed", words_delta=0, **PAIRED), "CHECK"),
        (dict(change="changed", title_changed=1, words_delta=2, **PAIRED), "CHECK"),
        (dict(change="changed", text_changed=1, old_index=None, new_index=7), "CHECK"),
        (dict(change="unchanged", words_delta=0, **PAIRED), None),
        (dict(change="unchanged", words_delta=1, **PAIRED), "CHECK"),
        (dict(change="unchanged", words_delta=0, seconds_changed=1, **PAIRED), "CHECK"),
        (dict(change="unchanged", words_delta=0, old_index=None, new_index=7), "CHECK"),
        # The change is open text in SQL: a later method may add a value.
        (dict(change="moved", old_index=None, new_index=None, words_delta=5), None),
        (dict(change=" "), "CHECK"),
        (dict(change="x" * 101), "CHECK"),
        (dict(kind="song"), "CHECK"),
        (dict(kind=None), "NOT NULL"),
        (dict(old_index=-1, change="removed", new_index=None), "CHECK"),
        (dict(old_index=100, change="removed", new_index=None), "CHECK"),
        (dict(new_index=99), None),
        (dict(new_index=100), "CHECK"),
        (dict(new_index=-1), "CHECK"),
        (dict(text_changed=2, change="changed", **PAIRED), "CHECK"),
        (dict(seconds_changed=-1, change="changed", **PAIRED), "CHECK"),
        (dict(words_delta=10_000_000), None),
        (dict(sha256="A" * 64), "CHECK"),
        (dict(sha256="a" * 63), "CHECK"),
        (dict(sha256="g" * 64), "CHECK"),
        (dict(revision_id="missing"), "FOREIGN KEY"),
        (dict(new_index=0), "UNIQUE"),  # taken by the first entry
        (dict(change="removed", old_index=0, new_index=None, words_delta=-1), "UNIQUE"),
    ],
)
def test_the_database_checks_entries(world: World, change, error) -> None:
    row = section_row(world, **change)

    if error is None:
        insert(world, "script_revision_sections", row)
        return
    with pytest.raises(sqlite3.IntegrityError, match=error):
        insert(world, "script_revision_sections", row)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (dict(), None),
        (dict(script_id="same"), "UNIQUE"),  # a second revision for the script
        (dict(parent_script_id="self"), "CHECK"),
        (dict(method=" "), "CHECK"),
        (dict(method="x" * 101), "CHECK"),
        (dict(method="a-later-method"), None),  # the method is open text
        (dict(version=1), "CHECK"),
        (dict(version=2), None),
        (dict(added_count=-1, entries_count=-1), "CHECK"),
        (dict(added_count=1, entries_count=1), None),
        (dict(added_count=200, entries_count=200), None),
        (dict(added_count=201, entries_count=201), "CHECK"),
        (dict(added_count=1), "CHECK"),  # the counts must add up
        (dict(entries_count=1), "CHECK"),
        (
            dict(unchanged_count=2, removed_count=1, changed_count=1, entries_count=3),
            "CHECK",
        ),
        (dict(removed_count=-1, added_count=1, entries_count=0), "CHECK"),
        (dict(words_before=-1), "CHECK"),
        (dict(words_after=-1), "CHECK"),
        (dict(words_after=10_000_000), None),
        (dict(seconds_before=-1), "CHECK"),
        (dict(seconds_after=-1), "CHECK"),
        (dict(content_sha256="A" * 64), "CHECK"),
        (dict(content_sha256="a" * 63), "CHECK"),
        (dict(content_sha256="g" * 64), "CHECK"),
        (dict(content_sha256=None), "NOT NULL"),
        (dict(requested_by_kind="robot"), "CHECK"),
        (dict(created_at="2026-10-04"), "CHECK"),
        (dict(script_id="missing"), "FOREIGN KEY"),
        (dict(parent_script_id="missing"), "FOREIGN KEY"),
        (dict(content_item_id="missing"), "FOREIGN KEY"),
    ],
)
def test_the_database_checks_revisions(world: World, change, error) -> None:
    revision = stored_revision(world)
    spare = world.revise(world.script(item=world.new_item()), good("Spare body."))
    row = {
        "id": "new",
        "script_id": spare.id,
        "parent_script_id": spare.parent_id,
        "content_item_id": spare.content_item_id,
        "method": "script-diff-v1",
        "version": 2,
        "added_count": 0,
        "removed_count": 0,
        "changed_count": 0,
        "unchanged_count": 0,
        "entries_count": 0,
        "words_before": 8,
        "words_after": 8,
        "seconds_before": 40,
        "seconds_after": 40,
        "content_sha256": "a" * 64,
        "requested_by_kind": "user",
        "requested_by_id": "owner",
        "created_at": format_datetime(T0),
    }
    row.update(change)
    if row["script_id"] == "same":
        row["script_id"] = revision.script_id
    if row["parent_script_id"] == "self":
        row["parent_script_id"] = row["script_id"]

    if error is None:
        insert(world, "script_revisions", row)
        return
    with pytest.raises(sqlite3.IntegrityError, match=error):
        insert(world, "script_revisions", row)


def test_the_python_bounds_are_the_sql_bounds() -> None:
    from ai_youtube_agent.content.script import MAX_SECTIONS
    from ai_youtube_agent.content.script_revision import MAX_ENTRIES, MAX_METHOD

    # 200 entries, 100 sections and a method of 100 characters: migration 0022.
    assert (MAX_ENTRIES, MAX_SECTIONS, MAX_METHOD) == (200, 100, 100)


# Migration and bootstrap


def test_migration_0022_keeps_earlier_rows(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    migrate(path, migrations=default_migrations()[:21])
    database = Database(path)
    world = World(database)
    first = world.script()
    second = world.revise(first, good("Second body text here."))
    audit = AuditLog(InMemoryAuditSink())
    extraction = ClaimExtractor(database, audit, clock=world.clock).extract(
        second.id, actor=USER
    )
    validation = ScriptValidator(database, audit, clock=world.clock).validate(
        second.id, actor=USER
    )
    tables = ("scripts", "content_items", "claim_extractions", "script_validations")
    before = [world.rows(table) for table in tables]
    with database.transaction() as connection:
        names = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
    assert "script_revisions" not in names

    migrate(path)

    assert [world.rows(table) for table in tables] == before
    with database.transaction() as connection:
        assert ClaimExtractionRepository(connection).get_by_script(second.id) == (
            extraction
        )
        assert ScriptRepository(connection).get(first.id) == first
    assert validation.script_id == second.id
    revision = ScriptVersioner(database, audit).record(second.id, actor=USER)
    assert (revision.changed_count, revision.unchanged_count) == (1, 2)


def test_bootstrap_registers_the_versioner(tmp_path: Path) -> None:
    container = build_container(
        Settings(environment=Environment.TEST, database_path=tmp_path / "a.db")
    )

    assert isinstance(container.resolve(ScriptVersioner), ScriptVersioner)
