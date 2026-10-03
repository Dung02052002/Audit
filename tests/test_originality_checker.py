"""F-071 Originality Check: the checker, the prior content and the storage.

Rules the user approved on 2026-10-03 (the rules themselves are tested in
``test_originality.py``):

- the prior content of a script is the latest version of every other content
  item of the same channel, among the scripts created strictly before it, the
  newest 50, whatever the status of the item; earlier versions of the same item
  and other channels are never compared;
- a record only: no gate, immutable, no override, and no script text stored;
- on demand, for any stored script (an unknown one is
  ``ScriptNotFoundError``, 404), one run per script, never recomputed, stored
  with its findings in one transaction (migration 0020); ``originality.checked``
  is audited after commit with ids and counts only.
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path

import pytest

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.claim_extractor import (
    ClaimExtractor,
    ScriptNotFoundError,
)
from ai_youtube_agent.content.originality import (
    MAX_PRIORS,
    ORIGINALITY_METHOD,
    Finding,
    OriginalityCheck,
    OriginalityCode,
    OriginalityStatus,
)
from ai_youtube_agent.content.originality_checker import OriginalityChecker
from ai_youtube_agent.content.script import Script, ScriptSection, SectionKind
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus
from ai_youtube_agent.core.db.codec import format_datetime
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.migrate import default_migrations, migrate
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ClaimExtractionRepository,
    ContentItemRepository,
    OriginalityRepository,
    ScriptRepository,
)
from factories import make_channel, make_content_item, make_strategy_profile

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner")
AI = Actor(ActorKind.AI, "mock/mock-1")
CODE = OriginalityCode
PASS, WARN, FAIL = (
    OriginalityStatus.PASS,
    OriginalityStatus.WARN,
    OriginalityStatus.FAIL,
)


def words(prefix: str, count: int = 40) -> str:
    """A text of ``count`` words found in no other text."""
    return " ".join(f"{prefix}{i}" for i in range(count))


BASE = words("base")  # 40 words, so 36 shingles
SENTENCES = " ".join(words(f"sent{i}x", 8) + "." for i in range(4))


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


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
        self.checker = OriginalityChecker(
            database, AuditLog(self.sink), clock=self.clock
        )

    def new_item(self, **change) -> ContentItem:
        item = make_content_item(self.channel, self.strategy, **change)
        with self.database.transaction() as connection:
            ContentItemRepository(connection).add(item)
        return item

    def other_channel_item(self) -> ContentItem:
        channel = make_channel()
        strategy = make_strategy_profile(channel)
        item = make_content_item(channel, strategy)
        with self.database.transaction() as connection:
            ChannelRepository(connection).add(channel)
            StrategyProfileRepository(connection).add(strategy)
            ContentItemRepository(connection).add(item)
        return item

    def script(
        self,
        text: str | tuple[ScriptSection, ...] = BASE,
        item: ContentItem | None = None,
    ) -> Script:
        """The next version-1 script of ``item``, created after every earlier one."""
        sections = (
            (ScriptSection(SectionKind.BODY, text),) if isinstance(text, str) else text
        )
        script = Script.create(
            (item or self.item).id, sections=sections, created_by=AI, clock=self.clock
        )
        return self.save(script)

    def revise(self, script: Script, text: str) -> Script:
        return self.save(script.next_version(text, created_by=AI, clock=self.clock))

    def save(self, script: Script) -> Script:
        with self.database.transaction() as connection:
            ScriptRepository(connection).add(script)
        return script

    def stored(self, script_id: str) -> OriginalityCheck | None:
        with self.database.transaction() as connection:
            return OriginalityRepository(connection).get_by_script(script_id)

    def count(self, table: str) -> int:
        with self.database.transaction() as connection:
            return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


@pytest.fixture
def world(database: Database) -> World:
    return World(database)


# The check


def test_a_script_is_checked_stored_and_audited(world: World) -> None:
    other = world.new_item()
    older = world.script(BASE, other)
    script = world.script(BASE)

    check = world.checker.check(script.id, actor=USER)

    assert (check.script_id, check.content_item_id) == (script.id, world.item.id)
    assert check.method == ORIGINALITY_METHOD == "originality-rules-v1"
    assert (check.words, check.priors_count) == (40, 1)
    assert check.requested_by == USER
    assert check.findings == (
        Finding(CODE.NEAR_DUPLICATE, FAIL, older.id, other.id, score=1.0, matched=36),
    )
    assert (check.findings_count, check.warn_count, check.fail_count) == (1, 0, 1)
    assert check.status is FAIL

    assert world.stored(script.id) == check
    with world.database.transaction() as connection:
        assert OriginalityRepository(connection).get(check.id) == check

    [event] = world.sink.events()
    assert (event.action, event.result, event.actor) == (
        "originality.checked",
        AuditResult.SUCCESS,
        USER,
    )
    assert (event.entity.type, event.entity.id) == ("script", script.id)
    assert dict(event.metadata) == {
        "originality_check_id": check.id,
        "content_item_id": world.item.id,
        "version": 1,
        "method": "originality-rules-v1",
        "words": 40,
        "priors": 1,
        "findings": 1,
        "warn": 0,
        "fail": 1,
        "status": "fail",
    }


def test_the_audit_and_the_rows_hold_no_script_text(world: World) -> None:
    world.script(BASE, world.new_item())
    script = world.script(BASE)

    world.checker.check(script.id, actor=USER)

    [event] = world.sink.events()
    assert all(isinstance(value, str | int) for value in event.metadata.values())
    assert "base1" not in repr(event.metadata)
    with world.database.transaction() as connection:
        rows = connection.execute(
            "SELECT * FROM originality_checks, originality_findings"
        ).fetchall()
    assert "base1" not in repr([tuple(row) for row in rows])


def test_a_script_without_priors_passes(world: World) -> None:
    script = world.script(BASE)

    check = world.checker.check(script.id, actor=USER)

    assert (check.priors_count, check.findings, check.status) == (0, (), PASS)
    assert world.stored(script.id) == check
    [event] = world.sink.events()
    assert (event.metadata["priors"], event.metadata["status"]) == (0, "pass")


def test_unrelated_priors_pass(world: World) -> None:
    world.script(words("other"), world.new_item())
    script = world.script(BASE)

    check = world.checker.check(script.id, actor=USER)

    assert (check.priors_count, check.findings, check.status) == (1, (), PASS)


def test_the_findings_are_stored_with_the_run_in_order(world: World) -> None:
    first, second = world.new_item(), world.new_item()
    older = world.script(SENTENCES, first)
    newer = world.script(SENTENCES, second)
    script = world.script(SENTENCES)

    check = world.checker.check(script.id, actor=USER)

    # Newest prior first; near_duplicate and copied_sentences for each.
    assert [(f.code, f.prior_script_id) for f in check.findings] == [
        (CODE.NEAR_DUPLICATE, newer.id),
        (CODE.COPIED_SENTENCES, newer.id),
        (CODE.NEAR_DUPLICATE, older.id),
        (CODE.COPIED_SENTENCES, older.id),
    ]
    assert world.stored(script.id) == check
    assert world.count("originality_findings") == 4


def test_the_structure_findings_are_stored(world: World) -> None:
    line = "Fees always add up quickly."
    script = world.script(f"{line} {line} {line} {line}")

    check = world.checker.check(script.id, actor=USER)

    assert [f.code for f in check.findings] == [
        CODE.REPEATED_SENTENCES,
        CODE.REPEATED_OPENERS,
    ]
    assert check.status is WARN
    assert world.stored(script.id) == check
    assert world.stored(script.id).findings[1].score == 1.0


# The prior content


def test_a_script_is_compared_only_with_older_content(world: World) -> None:
    older = world.script(BASE, world.new_item())
    newer = world.script(BASE)

    flagged = world.checker.check(newer.id, actor=USER)
    unflagged = world.checker.check(older.id, actor=USER)

    assert [f.prior_script_id for f in flagged.findings] == [older.id]
    assert (unflagged.priors_count, unflagged.findings) == (0, ())


def test_only_the_latest_version_of_another_item_counts(world: World) -> None:
    changed, kept = world.new_item(), world.new_item()
    first = world.script(BASE, changed)
    world.revise(first, words("changed"))  # the latest version is different
    second = world.script(words("early"), kept)
    latest = world.revise(second, BASE)  # the latest version is a copy
    script = world.script(BASE)

    check = world.checker.check(script.id, actor=USER)

    assert check.priors_count == 2
    assert [f.prior_script_id for f in check.findings] == [latest.id]


def test_the_versions_made_after_the_script_do_not_count(world: World) -> None:
    other = world.new_item()
    first = world.script(BASE, other)
    script = world.script(BASE)
    world.revise(first, words("later"))  # created after the script

    check = world.checker.check(script.id, actor=USER)

    assert (check.priors_count, [f.prior_script_id for f in check.findings]) == (
        1,
        [first.id],
    )


def test_earlier_versions_of_the_same_item_are_never_compared(world: World) -> None:
    first = world.script(BASE)
    second = world.revise(first, BASE + " extra")

    check = world.checker.check(second.id, actor=USER)

    assert (check.priors_count, check.findings, check.status) == (0, (), PASS)


def test_other_channels_are_never_compared(world: World) -> None:
    world.script(BASE, world.other_channel_item())
    script = world.script(BASE)

    check = world.checker.check(script.id, actor=USER)

    assert (check.priors_count, check.findings) == (0, ())


@pytest.mark.parametrize("status", list(ContentStatus))
def test_an_item_of_any_status_is_compared(world: World, status) -> None:
    other = world.new_item(status=status)
    prior = world.script(BASE, other)
    script = world.script(BASE)

    check = world.checker.check(script.id, actor=USER)

    assert [f.prior_script_id for f in check.findings] == [prior.id]


def test_only_the_newest_fifty_priors_are_compared(world: World) -> None:
    oldest = world.script(BASE, world.new_item())  # the 51st, left out
    for number in range(MAX_PRIORS - 1):
        world.script(words(f"fill{number}x"), world.new_item())
    newest = world.script(BASE, world.new_item())  # the 1st
    script = world.script(BASE)

    check = world.checker.check(script.id, actor=USER)

    assert check.priors_count == MAX_PRIORS == 50
    assert [f.prior_script_id for f in check.findings] == [newest.id]
    assert oldest.id not in {f.prior_script_id for f in check.findings}


def test_fifty_one_priors_are_cut_to_fifty(world: World) -> None:
    for number in range(MAX_PRIORS + 1):
        world.script(words(f"p{number}x"), world.new_item())
    script = world.script(BASE)

    assert world.checker.check(script.id, actor=USER).priors_count == MAX_PRIORS


# The prior scripts of the repository


def prior_ids(world: World, script: Script, limit: int = 50) -> list[str]:
    """The prior scripts of ``script``, as the checker reads them."""
    with world.database.transaction() as connection:
        item = ContentItemRepository(connection).get(script.content_item_id)
        return [
            s.id
            for s in ScriptRepository(connection).list_prior_latest(
                item.channel_id, item.id, script.created_at, limit
            )
        ]


def test_the_prior_scripts_are_cut_off_by_the_time(world: World) -> None:
    other = world.new_item()
    early = world.script(BASE, other)
    target = world.script(BASE)
    late = world.script(BASE, world.new_item())

    assert prior_ids(world, target) == [early.id]
    assert prior_ids(world, late) == [target.id, early.id]  # newest first
    assert prior_ids(world, early) == []


def test_a_script_made_at_the_same_time_is_not_a_prior(world: World) -> None:
    target = world.script(BASE)
    same = Script.create(
        world.new_item().id,
        sections=(ScriptSection(SectionKind.BODY, BASE),),
        clock=lambda: target.created_at,
    )
    world.save(same)

    assert prior_ids(world, target) == []
    after = Script.create(
        world.new_item().id,
        sections=(ScriptSection(SectionKind.BODY, BASE),),
        clock=lambda: target.created_at - timedelta(microseconds=1),
    )
    world.save(after)
    assert prior_ids(world, target) == [after.id]


def test_the_time_is_compared_as_stored(world: World) -> None:
    # Times are fixed-width text, so a time of another second or year is
    # ordered by the text: this pins the format the cut-off relies on.
    stamps = [
        datetime(2026, 10, 3, 12, 0, 0, 1, tzinfo=UTC),
        datetime(2026, 10, 3, 12, 0, 0, 999_999, tzinfo=UTC),
        datetime(2026, 10, 3, 12, 0, 1, tzinfo=UTC),
        datetime(2027, 1, 1, tzinfo=UTC),
    ]
    scripts = [
        world.save(
            Script.create(
                world.new_item().id,
                sections=(ScriptSection(SectionKind.BODY, BASE),),
                clock=lambda stamp=stamp: stamp,
            )
        )
        for stamp in stamps
    ]
    cut = Script.create(
        world.item.id,
        sections=(ScriptSection(SectionKind.BODY, BASE),),
        clock=lambda: stamps[2],
    )

    assert prior_ids(world, cut) == [scripts[1].id, scripts[0].id]
    assert format_datetime(stamps[0]) < format_datetime(stamps[1])


def test_the_highest_version_before_the_time_is_the_prior(world: World) -> None:
    other = world.new_item()
    v1 = world.script(BASE, other)
    v2 = world.revise(v1, words("two"))
    target = world.script(BASE)
    v3 = world.revise(v2, words("three"))  # after the time: not a prior

    assert prior_ids(world, target) == [v2.id]
    after = world.script(BASE, world.new_item())
    assert prior_ids(world, after) == [v3.id, target.id]


def test_the_prior_scripts_exclude_the_item_and_other_channels(world: World) -> None:
    own = world.script(BASE)  # the item itself, every version
    world.script(BASE, world.other_channel_item())
    sibling = world.script(BASE, world.new_item())
    target = world.revise(own, words("v2"))

    assert prior_ids(world, target) == [sibling.id]


def test_the_prior_scripts_are_limited_newest_first(world: World) -> None:
    made = [world.script(words(f"n{i}x"), world.new_item()) for i in range(5)]
    target = world.script(BASE)

    assert prior_ids(world, target, limit=3) == [s.id for s in reversed(made)][:3]


def test_equal_times_are_ordered_by_rowid_newest_first(world: World) -> None:
    stamp = T0 + timedelta(days=1)
    made = [
        world.save(
            Script.create(
                world.new_item().id,
                sections=(ScriptSection(SectionKind.BODY, BASE),),
                clock=lambda: stamp,
            )
        )
        for _ in range(3)
    ]
    target = Script.create(
        world.item.id,
        sections=(ScriptSection(SectionKind.BODY, BASE),),
        clock=lambda: stamp + timedelta(seconds=1),
    )

    assert prior_ids(world, target, limit=2) == [
        made[2].id,
        made[1].id,
    ]


# The run


def test_the_audit_event_is_recorded_after_the_commit(world: World) -> None:
    world.script(BASE, world.new_item())
    script = world.script(BASE)
    seen: list[OriginalityCheck | None] = []

    class CheckingSink(InMemoryAuditSink):
        def append(self, event) -> None:
            # A fresh connection only sees committed data.
            seen.append(world.stored(script.id))
            super().append(event)

    check = OriginalityChecker(world.database, AuditLog(CheckingSink())).check(
        script.id, actor=USER
    )

    assert seen == [check]


def test_a_second_call_returns_the_stored_run(world: World, monkeypatch) -> None:
    world.script(BASE, world.new_item())
    script = world.script(BASE)
    first = world.checker.check(script.id, actor=USER)
    rows = world.count("originality_findings")

    def not_read(self, *args):
        raise AssertionError("the prior scripts are not read again")

    monkeypatch.setattr(ScriptRepository, "list_prior_latest", not_read)
    world.script(BASE, world.new_item())  # newer content does not change the run

    again = world.checker.check(script.id, actor=AI)

    assert again == first
    assert world.count("originality_findings") == rows
    assert world.count("originality_checks") == 1
    assert [e.action for e in world.sink.events()] == ["originality.checked"]


def test_a_stored_run_is_not_recomputed_when_the_priors_change(world: World) -> None:
    script = world.script(BASE)
    first = world.checker.check(script.id, actor=USER)
    world.script(BASE, world.new_item())

    assert world.checker.check(script.id, actor=USER) == first
    assert first.priors_count == 0


def test_an_unknown_script_is_not_found(world: World) -> None:
    with pytest.raises(ScriptNotFoundError) as caught:
        world.checker.check("missing", actor=USER)

    assert caught.value.code == "domain.script_not_found"
    assert caught.value.to_public().http_status == HTTPStatus.NOT_FOUND
    assert world.sink.events() == ()
    assert world.count("originality_checks") == 0


def test_a_script_needs_no_other_run(world: World) -> None:
    script = world.script(BASE)

    check = world.checker.check(script.id, actor=USER)

    assert check.status is PASS
    assert (world.count("claim_extractions"), world.count("fact_checks")) == (0, 0)


def test_a_concurrent_run_stored_first_is_returned(world, monkeypatch) -> None:
    world.script(BASE, world.new_item())
    script = world.script(BASE)
    winner = world.checker.check(script.id, actor=USER)
    rows = world.count("originality_findings")
    real = OriginalityRepository.get_by_script
    calls: list[str] = []

    def racing(self, script_id):
        # The first read happens before the other run is committed.
        calls.append(script_id)
        return None if len(calls) == 1 else real(self, script_id)

    monkeypatch.setattr(OriginalityRepository, "get_by_script", racing)

    loser = world.checker.check(script.id, actor=AI)

    assert loser == winner
    assert calls == [script.id, script.id]
    assert world.count("originality_findings") == rows  # the losing insert rolled back
    assert [e.action for e in world.sink.events()] == ["originality.checked"]


def test_a_failed_finding_insert_rolls_back_the_run(world, monkeypatch) -> None:
    world.script(SENTENCES, world.new_item())
    script = world.script(SENTENCES)  # a near_duplicate and copied_sentences
    real = OriginalityRepository._insert
    inserted: list[str] = []

    def failing(self, table, values):
        if table == "originality_findings":
            if inserted:  # the run row and one finding are already written
                raise RuntimeError("disk full")
            inserted.append(values["id"])
        real(self, table, values)

    monkeypatch.setattr(OriginalityRepository, "_insert", failing)

    with pytest.raises(RuntimeError):
        world.checker.check(script.id, actor=USER)
    assert inserted
    assert world.stored(script.id) is None
    assert (world.count("originality_checks"), world.count("originality_findings")) == (
        0,
        0,
    )
    assert world.sink.events() == ()


def test_other_integrity_errors_are_raised(world, monkeypatch) -> None:
    script = world.script(BASE)

    def broken(self, check):
        raise sqlite3.IntegrityError("broken")

    monkeypatch.setattr(OriginalityRepository, "add", broken)

    with pytest.raises(sqlite3.IntegrityError):
        world.checker.check(script.id, actor=USER)
    assert world.stored(script.id) is None
    assert world.sink.events() == ()


def test_a_check_changes_no_script_row(world: World) -> None:
    world.script(BASE, world.new_item())
    script = world.script(BASE)
    before = (world.count("scripts"), world.count("claims"), world.count("evidence"))

    world.checker.check(script.id, actor=USER)

    assert (
        world.count("scripts"),
        world.count("claims"),
        world.count("evidence"),
    ) == before


# Storage


def stored_run(world: World):
    world.script(BASE, world.new_item())
    script = world.script(BASE)
    return script, world.checker.check(script.id, actor=USER)


def finding_row(world: World, **change) -> dict:
    script, check = stored_run(world)
    prior = check.findings[0]
    row = {
        "id": "new",
        "check_id": check.id,
        "status": "warn",
        "code": "high_overlap",
        "prior_script_id": prior.prior_script_id,
        "prior_content_item_id": prior.prior_content_item_id,
        "section_index": 1,
        "score": 0.5,
        "matched": 9,
    }
    row.update(change)
    return row


def insert(world: World, table: str, row: dict) -> None:
    with world.database.transaction() as connection:
        connection.execute(
            f"INSERT INTO {table} ({', '.join(row)}) "
            f"VALUES ({', '.join('?' for _ in row)})",
            tuple(row.values()),
        )


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (dict(), None),
        (dict(prior_script_id=None, prior_content_item_id=None), None),
        (dict(section_index=None, score=None, matched=None), None),
        (dict(score=0.0), None),
        (dict(score=1.0), None),
        (dict(status="fail"), None),
        (dict(status="pass"), "CHECK"),  # a finding is never a pass
        (dict(status="skip"), "CHECK"),
        (dict(code=" "), "CHECK"),
        (dict(code="x" * 101), "CHECK"),
        (dict(code="a_later_rule"), None),  # the code is open text in SQL
        (dict(section_index=-1), "CHECK"),
        (dict(score=1.5), "CHECK"),
        (dict(score=-0.5), "CHECK"),
        (dict(matched=-1), "CHECK"),
        (dict(prior_content_item_id=None), "CHECK"),  # both or neither
        (dict(prior_script_id=None), "CHECK"),
        (dict(prior_script_id="missing"), "FOREIGN KEY"),
        (dict(prior_content_item_id="missing"), "FOREIGN KEY"),
        (dict(check_id="missing"), "FOREIGN KEY"),
    ],
)
def test_the_database_checks_findings(world: World, change, error) -> None:
    row = finding_row(world, **change)

    if error is None:
        insert(world, "originality_findings", row)
        return
    with pytest.raises(sqlite3.IntegrityError, match=error):
        insert(world, "originality_findings", row)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (dict(), None),
        (dict(findings_count=2, warn_count=1, fail_count=1), None),
        (dict(script_id="same"), "UNIQUE"),  # a second run for the script
        (dict(method=" "), "CHECK"),
        (dict(method="x" * 101), "CHECK"),
        (dict(words=-1), "CHECK"),
        (dict(words=20_001), "CHECK"),
        (dict(words=20_000), None),
        (dict(priors_count=51), "CHECK"),
        (dict(priors_count=-1), "CHECK"),
        (dict(findings_count=104, warn_count=104), "CHECK"),
        (dict(findings_count=103, warn_count=103), None),
        (dict(findings_count=1), "CHECK"),  # the counts must add up
        (dict(warn_count=1), "CHECK"),
        (dict(fail_count=-1, warn_count=1), "CHECK"),
        (dict(requested_by_kind="robot"), "CHECK"),
        (dict(created_at="2026-10-03"), "CHECK"),
        (dict(script_id="missing"), "FOREIGN KEY"),
        (dict(content_item_id="missing"), "FOREIGN KEY"),
    ],
)
def test_the_database_checks_runs(world: World, change, error) -> None:
    script, check = stored_run(world)
    spare = world.script(words("spare"), world.new_item())
    row = {
        "id": "new",
        "script_id": spare.id,
        "content_item_id": spare.content_item_id,
        "method": "originality-rules-v1",
        "words": 40,
        "priors_count": 1,
        "findings_count": 0,
        "warn_count": 0,
        "fail_count": 0,
        "requested_by_kind": "user",
        "requested_by_id": "owner",
        "created_at": format_datetime(T0),
    }
    row.update(change)
    if row["script_id"] == "same":
        row["script_id"] = script.id

    if error is None:
        insert(world, "originality_checks", row)
        return
    with pytest.raises(sqlite3.IntegrityError, match=error):
        insert(world, "originality_checks", row)


def test_a_missing_run_is_none(world: World) -> None:
    with world.database.transaction() as connection:
        repository = OriginalityRepository(connection)
        assert repository.get("missing") is None
        assert repository.get_by_script("missing") is None


def test_migration_0020_keeps_earlier_rows(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    migrate(path, migrations=default_migrations()[:19])
    database = Database(path)
    world = World(database)
    first = world.script(BASE, world.new_item())
    script = world.script(BASE)
    extraction = ClaimExtractor(
        database, AuditLog(InMemoryAuditSink()), clock=world.clock
    ).extract(script.id, actor=USER)
    with database.transaction() as connection:
        before = [
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("scripts", "content_items", "claim_extractions")
        ]
        names = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
    assert "originality_checks" not in names

    migrate(path)

    with database.transaction() as connection:
        assert ClaimExtractionRepository(connection).get_by_script(script.id) == (
            extraction
        )
        assert ScriptRepository(connection).get(first.id) == first
        assert [
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("scripts", "content_items", "claim_extractions")
        ] == before
    check = OriginalityChecker(database, AuditLog(InMemoryAuditSink())).check(
        script.id, actor=USER
    )
    assert (check.priors_count, check.status) == (1, FAIL)


def test_bootstrap_registers_the_checker(tmp_path: Path) -> None:
    container = build_container(
        Settings(environment=Environment.TEST, database_path=tmp_path / "a.db")
    )
    assert isinstance(container.resolve(OriginalityChecker), OriginalityChecker)
