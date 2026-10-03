"""F-072 Script Validator: the validator, the strategy it reads and the storage.

Rules the user approved on 2026-10-04 (the rules themselves are tested in
``test_script_validation.py``):

- on demand, for any stored script version: an unknown script is
  ``ScriptNotFoundError`` (404), a channel without a strategy
  ``StrategyNotFoundError`` (404), a strategy without a language or a format
  ``ScriptInputError`` (422);
- one run per script, never recomputed (even when the strategy changes later):
  a stored run is returned before the strategy is loaded, without writing or
  auditing; the run and its findings are stored in one transaction (migration
  0021); ``script.validated`` is audited after commit with ids and counts only;
- LongForm scripts are validated even while ``LONGFORM_ENABLED`` is off;
- the scripts the Shorts and LongForm generators write pass;
- a record only: no gate, immutable, no override, and no script text stored.
"""

import dataclasses
import sqlite3
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path

import pytest

import test_longform_script_generator as longform_tests
import test_shorts_script_generator as shorts_tests
from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.claim_extractor import (
    ClaimExtractor,
    ScriptNotFoundError,
)
from ai_youtube_agent.content.hook_generator import ContentItemNotFoundError
from ai_youtube_agent.content.originality_checker import OriginalityChecker
from ai_youtube_agent.content.script import (
    DurationTarget,
    Script,
    ScriptSection,
    SectionKind,
)
from ai_youtube_agent.content.script_generation import ScriptInputError
from ai_youtube_agent.content.script_validation import (
    MAX_FINDINGS,
    MAX_WORDS,
    SCRIPT_VALIDATION_METHOD,
    Finding,
    ScriptValidation,
    ValidationCode,
    ValidationStatus,
)
from ai_youtube_agent.content.script_validator import ScriptValidator
from ai_youtube_agent.content.strategy import (
    Brand,
    FormatSettings,
    LanguageSettings,
    LongFormFormat,
    ShortsFormat,
)
from ai_youtube_agent.content.strategy_settings import StrategyNotFoundError
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus, ContentType
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
    FactCheckRepository,
    OriginalityRepository,
    ScriptRepository,
    ScriptValidationRepository,
)
from ai_youtube_agent.core.flags import FeatureFlags
from factories import make_channel, make_content_item, make_strategy_profile

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner")
AI = Actor(ActorKind.AI, "mock/mock-1")
CODE = ValidationCode
PASS, WARN, FAIL = (
    ValidationStatus.PASS,
    ValidationStatus.WARN,
    ValidationStatus.FAIL,
)
HOOK, INTRO, BODY, CHAPTER, OUTRO, CTA = (
    SectionKind.HOOK,
    SectionKind.INTRO,
    SectionKind.BODY,
    SectionKind.CHAPTER,
    SectionKind.OUTRO,
    SectionKind.CTA,
)
SECRET = "zebrafish"  # a word of the script that no run or event may hold
BRAND = Brand("Money Minute", "calm and clear", banned_phrases=("get rich quick",))
SHORTS_30 = FormatSettings(ShortsFormat(100, 120), LongFormFormat(480, 900))


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def good_shorts() -> tuple[ScriptSection, ...]:
    return (
        ScriptSection(HOOK, f"Short {SECRET} hook.", seconds=5),
        ScriptSection(BODY, f"A {SECRET} body.", seconds=30),
        ScriptSection(CTA, "Follow please.", seconds=5),
    )


class World:
    def __init__(self, database: Database, **strategy) -> None:
        self.database = database
        self.clock = Clock()
        self.channel = make_channel()
        self.strategy = make_strategy_profile(
            self.channel, **{"brand": BRAND, **strategy}
        )
        with database.transaction() as connection:
            ChannelRepository(connection).add(self.channel)
            StrategyProfileRepository(connection).add(self.strategy)
        self.item = self.new_item()
        self.used: set[str] = set()
        self.sink = InMemoryAuditSink()
        self.validator = ScriptValidator(
            database, AuditLog(self.sink), clock=self.clock
        )

    def new_item(self, **change) -> ContentItem:
        item = make_content_item(self.channel, self.strategy, **change)
        with self.database.transaction() as connection:
            ContentItemRepository(connection).add(item)
        return item

    def script(
        self,
        sections: tuple[ScriptSection, ...] | None = None,
        item: ContentItem | None = None,
        **change,
    ) -> Script:
        if item is None:  # the first script of the item, else a new item's
            item = self.item if self.item.id not in self.used else self.new_item()
        self.used.add(item.id)
        script = Script.create(
            item.id,
            sections=sections or good_shorts(),
            created_by=AI,
            strategy_version=self.strategy.version,
            clock=self.clock,
            **change,
        )
        with self.database.transaction() as connection:
            ScriptRepository(connection).add(script)
        return script

    def revise(self, script: Script, sections: tuple[ScriptSection, ...]) -> Script:
        new = script.next_version(sections=sections, created_by=AI, clock=self.clock)
        with self.database.transaction() as connection:
            ScriptRepository(connection).add(new)
        return new

    def change_strategy(self, **change) -> None:
        new = dataclasses.replace(
            self.strategy, version=self.strategy.version + 1, **change
        )
        with self.database.transaction() as connection:
            StrategyProfileRepository(connection).update(
                new, expected_version=self.strategy.version
            )
        self.strategy = new

    def forget_strategy(self) -> None:
        connection = self.database.connect()
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("DELETE FROM strategy_profiles")
        finally:
            connection.close()

    def stored(self, script_id: str) -> ScriptValidation | None:
        with self.database.transaction() as connection:
            return ScriptValidationRepository(connection).get_by_script(script_id)

    def count(self, table: str) -> int:
        with self.database.transaction() as connection:
            return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


@pytest.fixture
def world(database: Database) -> World:
    return World(database)


def codes(validation: ScriptValidation) -> list[str]:
    return [finding.code.value for finding in validation.findings]


# The validation


def test_a_script_is_validated_stored_and_audited(world: World) -> None:
    script = world.script(good_shorts()[:2])  # no call to action

    validation = world.validator.validate(script.id, actor=USER)

    assert (validation.script_id, validation.content_item_id) == (
        script.id,
        world.item.id,
    )
    assert validation.method == SCRIPT_VALIDATION_METHOD == "script-rules-v1"
    assert validation.requested_by == USER
    assert (validation.words, validation.seconds) == (6, 35)
    assert validation.findings == (Finding(CODE.MISSING_SECTION, FAIL),)
    counts = (validation.findings_count, validation.warn_count, validation.fail_count)
    assert counts == (1, 0, 1)
    assert validation.status is FAIL
    assert (validation.strategy_version, validation.script_strategy_version) == (1, 1)

    assert world.stored(script.id) == validation
    with world.database.transaction() as connection:
        assert ScriptValidationRepository(connection).get(validation.id) == validation

    [event] = world.sink.events()
    assert (event.action, event.result, event.actor) == (
        "script.validated",
        AuditResult.SUCCESS,
        USER,
    )
    assert (event.entity.type, event.entity.id) == ("script", script.id)
    assert dict(event.metadata) == {
        "script_validation_id": validation.id,
        "content_item_id": world.item.id,
        "version": 1,
        "method": "script-rules-v1",
        "strategy_version": 1,
        "words": 6,
        "seconds": 35,
        "language_checked": False,
        "findings": 1,
        "warn": 0,
        "fail": 1,
        "status": "fail",
    }


def test_a_good_script_passes(world: World) -> None:
    script = world.script()

    validation = world.validator.validate(script.id, actor=USER)

    assert (validation.findings, validation.status) == ((), PASS)
    assert world.stored(script.id) == validation
    [event] = world.sink.events()
    assert (event.metadata["findings"], event.metadata["status"]) == (0, "pass")


def test_the_audit_and_the_rows_hold_no_script_text(world: World) -> None:
    sections = (
        ScriptSection(HOOK, f"Get rich quick with {SECRET}.", seconds=5),
        ScriptSection(BODY, f"A {SECRET} body.", seconds=30),
        ScriptSection(CTA, "Follow please.", seconds=5),
    )
    script = world.script(sections)

    validation = world.validator.validate(script.id, actor=USER)

    assert codes(validation) == ["banned_phrase"]
    [event] = world.sink.events()
    assert all(isinstance(value, str | int | bool) for value in event.metadata.values())
    assert SECRET not in repr(event.metadata) and "rich" not in repr(event.metadata)
    with world.database.transaction() as connection:
        rows = []
        for table in ("script_validations", "script_validation_findings"):
            rows += [tuple(r) for r in connection.execute(f"SELECT * FROM {table}")]
    assert SECRET not in repr(rows) and "rich" not in repr(rows)
    assert SECRET not in repr(validation) and "rich" not in repr(validation)


def test_the_language_flag_is_recorded(world: World) -> None:
    unchecked = world.validator.validate(world.script().id, actor=USER)
    english = " ".join(["the and of to in is it you that was"] * 3)
    sections = (
        ScriptSection(HOOK, "Short hook.", seconds=5),
        ScriptSection(BODY, english, seconds=30),
        ScriptSection(CTA, "Follow please.", seconds=5),
    )
    checked = world.validator.validate(world.script(sections).id, actor=USER)

    assert (unchecked.language_checked, unchecked.status) == (False, PASS)
    assert (checked.language_checked, checked.language_hits) == (True, 30)
    assert world.stored(unchecked.script_id).language_checked is False
    assert world.stored(checked.script_id).language_checked is True
    with world.database.transaction() as connection:
        flags = [
            tuple(r)
            for r in connection.execute(
                "SELECT language_checked, language_hits FROM script_validations "
                "ORDER BY created_at"
            )
        ]
    assert flags == [(0, 1), (1, 30)]  # one signal word: "a"


def test_the_findings_are_stored_with_the_run_in_order(world: World) -> None:
    sections = (
        ScriptSection(CTA, "Follow please.", seconds=2),
        ScriptSection(HOOK, " ".join(["word"] * 16), seconds=2),
        ScriptSection(BODY, "Get rich quick.", seconds=2),
    )
    script = world.script(sections)

    validation = world.validator.validate(script.id, actor=USER)

    assert codes(validation) == [
        "wrong_order",
        "too_short",
        "banned_phrase",
        "hook_too_long",
    ]
    assert [f.status for f in validation.findings] == [FAIL, FAIL, FAIL, WARN]
    assert world.stored(script.id) == validation
    assert world.count("script_validation_findings") == 4


def test_the_rules_read_the_current_strategy(world: World) -> None:
    world.change_strategy(format=SHORTS_30, languages=LanguageSettings("vi"))
    script = world.script()

    validation = world.validator.validate(script.id, actor=USER)

    assert (validation.strategy_version, validation.script_strategy_version) == (2, 2)
    assert validation.findings == (
        Finding(CODE.TOO_SHORT, FAIL, actual=40, minimum=100, maximum=120),
    )


def test_the_run_records_both_strategy_versions(world: World) -> None:
    script = world.script()  # written from version 1
    world.change_strategy(brand=Brand("Money Minute"))

    validation = world.validator.validate(script.id, actor=USER)

    assert (validation.strategy_version, validation.script_strategy_version) == (2, 1)


def test_a_script_without_a_strategy_version_is_validated(world: World) -> None:
    script = Script.create(
        world.item.id, sections=good_shorts(), created_by=AI, clock=world.clock
    )
    with world.database.transaction() as connection:
        ScriptRepository(connection).add(script)

    validation = world.validator.validate(script.id, actor=USER)

    assert validation.script_strategy_version is None
    assert world.stored(script.id) == validation


def test_the_scripts_own_duration_target_is_used(world: World) -> None:
    script = world.script(duration_target=DurationTarget(100, 200))

    validation = world.validator.validate(script.id, actor=USER)

    assert validation.findings == (
        Finding(CODE.TOO_SHORT, FAIL, actual=40, minimum=100, maximum=200),
    )


@pytest.mark.parametrize("status", list(ContentStatus))
def test_an_item_of_any_status_is_validated(world: World, status) -> None:
    item = world.new_item(status=status)
    script = world.script(item=item)

    assert world.validator.validate(script.id, actor=USER).status is PASS


def test_each_version_is_validated_on_its_own(world: World) -> None:
    first = world.script()
    second = world.revise(first, good_shorts()[:2])

    one = world.validator.validate(first.id, actor=USER)
    two = world.validator.validate(second.id, actor=USER)

    assert (one.status, two.status) == (PASS, FAIL)
    assert world.count("script_validations") == 2


def test_a_script_needs_no_other_run_and_reads_none(world: World, monkeypatch) -> None:
    script = world.script()

    def not_read(self, *args):
        raise AssertionError("an earlier result is not read")

    for repository in (
        ClaimExtractionRepository,
        FactCheckRepository,
        OriginalityRepository,
    ):
        monkeypatch.setattr(repository, "get_by_script", not_read, raising=False)

    validation = world.validator.validate(script.id, actor=USER)

    assert validation.status is PASS
    for table in ("claim_extractions", "fact_checks", "originality_checks"):
        assert world.count(table) == 0


def test_a_check_changes_no_script_row(world: World) -> None:
    script = world.script()
    tables = ("scripts", "claims", "evidence", "content_items")
    before = [world.count(table) for table in tables]

    world.validator.validate(script.id, actor=USER)

    assert before == [world.count(table) for table in tables]


# LongForm, with the flag off


def longform_sections(count: int = 3, kind: SectionKind = CHAPTER):
    title = "A title" if kind is CHAPTER else None
    return (
        ScriptSection(HOOK, "Short hook.", seconds=10),
        ScriptSection(INTRO, "A plain intro.", seconds=20),
        *(ScriptSection(kind, "Plain words here.", title, 150) for _ in range(count)),
        ScriptSection(OUTRO, "A plain outro.", seconds=20),
        ScriptSection(CTA, "Follow please.", seconds=10),
    )


def test_a_longform_script_is_validated_while_the_flag_is_off(world: World) -> None:
    assert FeatureFlags().longform_enabled is False
    item = world.new_item(content_type=ContentType.LONGFORM)
    script = world.script(longform_sections(), item=item)

    validation = world.validator.validate(script.id, actor=USER)

    assert (validation.findings, validation.status) == ((), PASS)
    assert validation.seconds == 510


def test_the_longform_rules_apply(world: World) -> None:
    item = world.new_item(content_type=ContentType.LONGFORM)
    script = world.script(longform_sections(2), item=item)

    validation = world.validator.validate(script.id, actor=USER)

    assert codes(validation) == ["chapter_count", "too_short"]


def test_body_chapters_follow_the_strategy_format(database: Database) -> None:
    longform = LongFormFormat(480, 900, chapters=False)
    world = World(database, format=FormatSettings(ShortsFormat(15, 60), longform))
    body = world.script(
        longform_sections(3, BODY),
        item=world.new_item(content_type=ContentType.LONGFORM),
    )
    titled = world.script(
        longform_sections(3, CHAPTER),
        item=world.new_item(content_type=ContentType.LONGFORM),
    )

    assert world.validator.validate(body.id, actor=USER).status is PASS
    stored = world.validator.validate(titled.id, actor=USER)
    assert stored.findings == (Finding(CODE.CHAPTER_KIND, FAIL, 2, actual=3),)
    assert world.stored(titled.id) == stored


# The scripts the generators write


@pytest.mark.parametrize("bodies", [1, 2, 3])
def test_the_shorts_generator_scripts_pass(database: Database, bodies: int) -> None:
    generator = shorts_tests.ShortsWorld(database)
    validator = ScriptValidator(database, AuditLog(InMemoryAuditSink()))
    text = shorts_tests.words(30 if bodies == 1 else 20)
    script = generator.write(shorts_tests.answer(*[text] * bodies))

    validation = validator.validate(script.id, actor=USER)

    assert sum(s.kind is BODY for s in script.sections) == bodies
    assert (validation.findings, validation.status) == ((), PASS)


def test_a_second_shorts_version_passes(database: Database) -> None:
    generator = shorts_tests.ShortsWorld(database)
    validator = ScriptValidator(database, AuditLog(InMemoryAuditSink()))
    first = generator.write(shorts_tests.answer(shorts_tests.words(30)))
    second = generator.write(shorts_tests.answer(shorts_tests.words(25, "save")))

    assert second.version == 2
    assert validator.validate(first.id, actor=USER).status is PASS
    assert validator.validate(second.id, actor=USER).status is PASS


@pytest.mark.parametrize("chapters", [3, 4, 12])
def test_the_longform_generator_scripts_pass(database: Database, chapters) -> None:
    generator = longform_tests.LongFormWorld(database)
    validator = ScriptValidator(database, AuditLog(InMemoryAuditSink()))
    titles = [f"Chapter number {n}" for n in range(chapters)]
    text = longform_tests.words({3: 500, 4: 400, 12: 150}[chapters])
    script = generator.write(
        longform_tests.outline(titles=titles),
        *[longform_tests.chapter(text)] * chapters,
    )

    validation = validator.validate(script.id, actor=USER)

    assert sum(s.kind is CHAPTER for s in script.sections) == chapters
    assert (validation.findings, validation.status) == ((), PASS)


def test_the_longform_generator_scripts_without_chapters_pass(
    database: Database,
) -> None:
    longform = LongFormFormat(480, 900, chapters=False)
    generator = longform_tests.LongFormWorld(
        database, format=FormatSettings(ShortsFormat(15, 60), longform)
    )
    validator = ScriptValidator(database, AuditLog(InMemoryAuditSink()))
    script = generator.write(longform_tests.outline(), *[longform_tests.GOOD] * 3)

    validation = validator.validate(script.id, actor=USER)

    assert [s.kind for s in script.sections].count(BODY) == 3
    assert (validation.findings, validation.status) == ((), PASS)


def test_the_generators_pass_while_the_longform_flag_is_off(database: Database) -> None:
    # The script is written with the flag on, then read by the validator, which
    # has no flag: nothing in a run depends on LONGFORM_ENABLED.
    generator = longform_tests.LongFormWorld(database, enabled=True)
    script = generator.write(longform_tests.outline(), *[longform_tests.GOOD] * 3)
    container = build_container(
        Settings(environment=Environment.TEST, database_path=database.path)
    )

    validator = container.resolve(ScriptValidator)

    assert container.resolve(FeatureFlags).longform_enabled is False
    assert validator.validate(script.id, actor=USER).status is PASS


# The strategy


def test_an_unknown_script_is_not_found(world: World) -> None:
    with pytest.raises(ScriptNotFoundError) as caught:
        world.validator.validate("missing", actor=USER)

    assert caught.value.code == "domain.script_not_found"
    assert caught.value.to_public().http_status == HTTPStatus.NOT_FOUND
    assert world.sink.events() == ()
    assert world.count("script_validations") == 0


def test_a_channel_without_a_strategy_is_not_found(world: World) -> None:
    script = world.script()
    world.forget_strategy()

    with pytest.raises(StrategyNotFoundError) as caught:
        world.validator.validate(script.id, actor=USER)

    assert caught.value.code == "domain.strategy_not_found"
    assert caught.value.to_public().http_status == HTTPStatus.NOT_FOUND
    assert world.sink.events() == ()
    assert world.count("script_validations") == 0


@pytest.mark.parametrize("missing", ["languages", "format"])
def test_a_strategy_without_a_language_or_a_format_is_an_input_error(
    database: Database, missing: str
) -> None:
    world = World(database, **{missing: None})
    script = world.script()

    with pytest.raises(ScriptInputError) as caught:
        world.validator.validate(script.id, actor=USER)

    assert caught.value.code == "domain.script_input"
    assert caught.value.to_public().http_status == HTTPStatus.UNPROCESSABLE_ENTITY
    assert world.sink.events() == ()
    assert world.count("script_validations") == 0


def test_a_missing_content_item_is_not_found(world: World, monkeypatch) -> None:
    script = world.script()
    monkeypatch.setattr(ContentItemRepository, "get", lambda self, item_id: None)

    with pytest.raises(ContentItemNotFoundError) as caught:
        world.validator.validate(script.id, actor=USER)

    assert caught.value.to_public().http_status == HTTPStatus.NOT_FOUND
    assert world.count("script_validations") == 0


# The run


def test_the_audit_event_is_recorded_after_the_commit(world: World) -> None:
    script = world.script()
    seen: list[ScriptValidation | None] = []

    class CheckingSink(InMemoryAuditSink):
        def append(self, event) -> None:
            # A fresh connection only sees committed data.
            seen.append(world.stored(script.id))
            super().append(event)

    validation = ScriptValidator(world.database, AuditLog(CheckingSink())).validate(
        script.id, actor=USER
    )

    assert seen == [validation]


def test_a_second_call_returns_the_stored_run(world: World, monkeypatch) -> None:
    script = world.script()
    first = world.validator.validate(script.id, actor=USER)
    rows = world.count("script_validation_findings")

    def not_read(self, *args):
        raise AssertionError("the strategy is not loaded again")

    monkeypatch.setattr(StrategyProfileRepository, "get_by_channel", not_read)

    again = world.validator.validate(script.id, actor=AI)

    assert again == first
    assert world.count("script_validation_findings") == rows
    assert world.count("script_validations") == 1
    assert [e.action for e in world.sink.events()] == ["script.validated"]


def test_a_stored_run_is_not_recomputed_when_the_strategy_changes(
    world: World,
) -> None:
    script = world.script()
    first = world.validator.validate(script.id, actor=USER)
    world.change_strategy(format=SHORTS_30)

    again = world.validator.validate(script.id, actor=USER)

    assert again == first
    assert (first.strategy_version, first.status) == (1, PASS)
    # A script made after the change is read by the new strategy.
    later = world.script()
    assert world.validator.validate(later.id, actor=USER).strategy_version == 2


def test_a_stored_run_is_returned_even_without_the_strategy(world: World) -> None:
    script = world.script()
    first = world.validator.validate(script.id, actor=USER)
    world.forget_strategy()

    assert world.validator.validate(script.id, actor=USER) == first


def test_a_concurrent_run_stored_first_is_returned(world, monkeypatch) -> None:
    script = world.script()
    winner = world.validator.validate(script.id, actor=USER)
    rows = world.count("script_validation_findings")
    real = ScriptValidationRepository.get_by_script
    calls: list[str] = []

    def racing(self, script_id):
        # The first read happens before the other run is committed.
        calls.append(script_id)
        return None if len(calls) == 1 else real(self, script_id)

    monkeypatch.setattr(ScriptValidationRepository, "get_by_script", racing)

    loser = world.validator.validate(script.id, actor=AI)

    assert loser == winner
    assert calls == [script.id, script.id]
    assert world.count("script_validation_findings") == rows
    assert [e.action for e in world.sink.events()] == ["script.validated"]


def test_a_failed_finding_insert_rolls_back_the_run(world, monkeypatch) -> None:
    script = world.script(good_shorts()[:2])
    real = ScriptValidationRepository._insert
    inserted: list[str] = []

    def failing(self, table, values):
        if table == "script_validation_findings":
            inserted.append(values["id"])
            raise RuntimeError("disk full")
        real(self, table, values)

    monkeypatch.setattr(ScriptValidationRepository, "_insert", failing)

    with pytest.raises(RuntimeError):
        world.validator.validate(script.id, actor=USER)
    assert inserted
    assert world.stored(script.id) is None
    assert (
        world.count("script_validations"),
        world.count("script_validation_findings"),
    ) == (0, 0)
    assert world.sink.events() == ()


def test_other_integrity_errors_are_raised(world, monkeypatch) -> None:
    script = world.script()

    def broken(self, validation):
        raise sqlite3.IntegrityError("broken")

    monkeypatch.setattr(ScriptValidationRepository, "add", broken)

    with pytest.raises(sqlite3.IntegrityError):
        world.validator.validate(script.id, actor=USER)
    assert world.stored(script.id) is None
    assert world.sink.events() == ()


# Storage


def stored_run(world: World):
    script = world.script(good_shorts()[:2])
    return script, world.validator.validate(script.id, actor=USER)


def finding_row(world: World, **change) -> dict:
    script, validation = stored_run(world)
    row = {
        "id": "new",
        "validation_id": validation.id,
        "status": "warn",
        "code": "hook_too_long",
        "section_index": 1,
        "actual": 16,
        "minimum": 1,
        "maximum": 15,
        "score": 0.5,
        "matched": 2,
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
        (dict(section_index=None, actual=None, minimum=None, maximum=None), None),
        (dict(score=None, matched=None), None),
        (dict(score=0.0), None),
        (dict(score=1.0), None),
        (dict(minimum=15, maximum=15), None),
        (dict(minimum=16, maximum=15), "CHECK"),
        (dict(minimum=16, maximum=None), None),
        (dict(status="fail"), None),
        (dict(status="pass"), "CHECK"),  # a finding is never a pass
        (dict(status="skip"), "CHECK"),
        (dict(code=" "), "CHECK"),
        (dict(code="x" * 101), "CHECK"),
        (dict(code="a_later_rule"), None),  # the code is open text in SQL
        (dict(section_index=-1), "CHECK"),
        (dict(actual=-1), "CHECK"),
        (dict(minimum=-1), "CHECK"),
        (dict(maximum=-1), "CHECK"),
        (dict(score=1.5), "CHECK"),
        (dict(score=-0.5), "CHECK"),
        (dict(matched=-1), "CHECK"),
        (dict(validation_id="missing"), "FOREIGN KEY"),
    ],
)
def test_the_database_checks_findings(world: World, change, error) -> None:
    row = finding_row(world, **change)

    if error is None:
        insert(world, "script_validation_findings", row)
        return
    with pytest.raises(sqlite3.IntegrityError, match=error):
        insert(world, "script_validation_findings", row)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (dict(), None),
        (dict(findings_count=2, warn_count=1, fail_count=1), None),
        (dict(script_id="same"), "UNIQUE"),  # a second run for the script
        (dict(method=" "), "CHECK"),
        (dict(method="x" * 101), "CHECK"),
        (dict(words=-1), "CHECK"),
        (dict(words=10_000_000), None),
        (dict(seconds=-1), "CHECK"),
        (dict(seconds=0), None),
        (dict(language_checked=2), "CHECK"),
        (dict(language_checked=-1), "CHECK"),
        (dict(language_checked=1, language_hits=20_000), None),
        (dict(language_hits=20_001), "CHECK"),
        (dict(language_hits=-1), "CHECK"),
        (dict(strategy_version=0), "CHECK"),
        (dict(script_strategy_version=0), "CHECK"),
        (dict(script_strategy_version=None), None),
        (dict(findings_count=211, warn_count=211), "CHECK"),
        (dict(findings_count=210, warn_count=210), None),
        (dict(findings_count=1), "CHECK"),  # the counts must add up
        (dict(warn_count=1), "CHECK"),
        (dict(fail_count=-1, warn_count=1), "CHECK"),
        (dict(requested_by_kind="robot"), "CHECK"),
        (dict(created_at="2026-10-04"), "CHECK"),
        (dict(script_id="missing"), "FOREIGN KEY"),
        (dict(content_item_id="missing"), "FOREIGN KEY"),
    ],
)
def test_the_database_checks_runs(world: World, change, error) -> None:
    script, validation = stored_run(world)
    spare = world.script()
    row = {
        "id": "new",
        "script_id": spare.id,
        "content_item_id": spare.content_item_id,
        "method": "script-rules-v1",
        "words": 40,
        "seconds": 40,
        "language_checked": 0,
        "language_hits": 0,
        "strategy_version": 1,
        "script_strategy_version": 1,
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
        insert(world, "script_validations", row)
        return
    with pytest.raises(sqlite3.IntegrityError, match=error):
        insert(world, "script_validations", row)


def test_every_kind_of_finding_is_stored_and_read_back(world: World) -> None:
    script = world.script()
    findings = (
        Finding(CODE.MISSING_SECTION, FAIL),
        Finding(CODE.UNEXPECTED_SECTION, FAIL, 2),
        Finding(CODE.CHAPTER_COUNT, FAIL, actual=2, minimum=3, maximum=12),
        Finding(CODE.WRONG_ORDER, FAIL, 1),
        Finding(CODE.TOO_LONG, FAIL, actual=40, minimum=10, maximum=30),
        Finding(CODE.LANGUAGE_MISMATCH, WARN, actual=40, score=0.7, matched=28),
        Finding(CODE.BANNED_PHRASE, FAIL, 1, matched=2),
        Finding(CODE.HOOK_TOO_LONG, WARN, 0, actual=16, maximum=15),
    )
    validation = ScriptValidation(
        id="run-1",
        script_id=script.id,
        content_item_id=script.content_item_id,
        method=SCRIPT_VALIDATION_METHOD,
        words=50,
        seconds=40,
        language_checked=True,
        language_hits=40,
        strategy_version=1,
        script_strategy_version=1,
        findings=findings,
        findings_count=8,
        warn_count=2,
        fail_count=6,
        requested_by=USER,
        created_at=T0,
    )
    with world.database.transaction() as connection:
        ScriptValidationRepository(connection).add(validation)

    assert world.stored(script.id) == validation
    assert world.stored(script.id).findings == findings


def test_a_missing_run_is_none(world: World) -> None:
    with world.database.transaction() as connection:
        repository = ScriptValidationRepository(connection)
        assert repository.get("missing") is None
        assert repository.get_by_script("missing") is None


def test_the_python_bounds_are_the_sql_bounds() -> None:
    # 20,000 language words and 210 findings: the checks of migration 0021.
    assert (MAX_WORDS, MAX_FINDINGS) == (20_000, 210)


def test_migration_0021_keeps_earlier_rows(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    migrate(path, migrations=default_migrations()[:20])
    database = Database(path)
    world = World(database)
    first = world.script(item=world.new_item())
    script = world.script()
    audit = AuditLog(InMemoryAuditSink())
    extraction = ClaimExtractor(database, audit, clock=world.clock).extract(
        script.id, actor=USER
    )
    originality = OriginalityChecker(database, audit, clock=world.clock).check(
        script.id, actor=USER
    )
    tables = ("scripts", "content_items", "claim_extractions", "originality_checks")
    before = [world.count(table) for table in tables]
    with database.transaction() as connection:
        names = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
    assert "script_validations" not in names

    migrate(path)

    assert [world.count(table) for table in tables] == before
    with database.transaction() as connection:
        assert ClaimExtractionRepository(connection).get_by_script(script.id) == (
            extraction
        )
        assert OriginalityRepository(connection).get_by_script(script.id) == (
            originality
        )
        assert ScriptRepository(connection).get(first.id) == first
    validation = ScriptValidator(database, audit).validate(script.id, actor=USER)
    assert validation.status is PASS


def test_bootstrap_registers_the_validator(tmp_path: Path) -> None:
    container = build_container(
        Settings(environment=Environment.TEST, database_path=tmp_path / "a.db")
    )
    assert isinstance(container.resolve(ScriptValidator), ScriptValidator)
