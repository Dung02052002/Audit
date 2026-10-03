"""F-064 Script Model (Prompt Pack v8, prompt #064).

Rules the user approved on 2026-10-03 (B-017 ``Script`` extended):

- a script version has 1-100 ordered sections of a fixed ``SectionKind``
  (hook, intro, body, chapter, outro, cta) with text, an optional title
  (required for a chapter) and optional seconds; ``text`` joins them;
- the duration target is copied from the strategy format for the content type
  and only reported (``estimated_seconds``, ``within_target``; 150 words per
  minute when a section has no seconds); #072 validates;
- each version records who made it, why, its parent version and the strategy
  version and research report it used; a claim may name its section;
- migration 0015 turns stored scripts into one body section and links each
  later version to the previous one.
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_youtube_agent.content.script import (
    WORDS_PER_MINUTE,
    Claim,
    DurationTarget,
    Script,
    ScriptSection,
    SectionKind,
)
from ai_youtube_agent.content.strategy import (
    FormatSettings,
    LongFormFormat,
    ShortsFormat,
)
from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.core.db.codec import format_datetime
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.migrate import default_migrations, migrate
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ContentItemRepository,
    ScriptRepository,
)
from factories import make_channel, make_content_item, make_strategy_profile

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=5)
AI = Actor(ActorKind.AI, "script-writer")
USER = Actor(ActorKind.USER, "owner")
FORMAT = FormatSettings(ShortsFormat(15, 60), LongFormFormat(480, 900))


def at(moment: datetime):
    return lambda: moment


def words(n: int) -> str:
    return " ".join(["word"] * n)


def shorts_sections() -> list[ScriptSection]:
    return [
        ScriptSection.create(SectionKind.HOOK, " Most people never check bank fees. "),
        ScriptSection.create(SectionKind.BODY, "Here is why that matters.", seconds=20),
        ScriptSection.create(SectionKind.CTA, "Follow for more."),
    ]


def new_script(**kwargs) -> Script:
    return Script.create(
        "item-1",
        sections=shorts_sections(),
        duration_target=DurationTarget.from_format(FORMAT, ContentType.SHORTS),
        created_by=AI,
        strategy_version=3,
        clock=at(T0),
        **kwargs,
    )


# Sections


def test_a_script_is_ordered_sections_joined_into_its_text() -> None:
    script = new_script()

    assert [s.kind for s in script.sections] == [
        SectionKind.HOOK,
        SectionKind.BODY,
        SectionKind.CTA,
    ]
    assert script.sections[0].text == "Most people never check bank fees."
    assert script.text == (
        "Most people never check bank fees.\n\nHere is why that matters.\n\n"
        "Follow for more."
    )


def test_text_alone_makes_one_body_section() -> None:
    script = Script.create("item-1", "  Just text.  ")

    assert script.sections == (ScriptSection(SectionKind.BODY, "Just text."),)


def test_a_script_needs_text_or_sections_and_1_to_100_sections() -> None:
    section = ScriptSection(SectionKind.BODY, "x")
    for bad in (
        dict(),
        dict(text="a", sections=[section]),
        dict(sections=[]),
        dict(sections=[section] * 101),
    ):
        with pytest.raises(ValueError):
            Script.create("item-1", **bad)
    assert len(Script.create("item-1", sections=[section] * 100).sections) == 100


def test_section_kinds_are_closed() -> None:
    assert [k.value for k in SectionKind] == [
        "hook",
        "intro",
        "body",
        "chapter",
        "outro",
        "cta",
    ]
    with pytest.raises(TypeError):
        ScriptSection("hook", "text")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad",
    [
        dict(kind=SectionKind.CHAPTER, text="t"),
        dict(kind=SectionKind.BODY, text=" "),
        dict(kind=SectionKind.BODY, text=" t"),
        dict(kind=SectionKind.BODY, text="t", title=" "),
        dict(kind=SectionKind.BODY, text="t", title="x" * 101),
        dict(kind=SectionKind.BODY, text="t", seconds=0),
        dict(kind=SectionKind.BODY, text="t", seconds=14_401),
        dict(kind=SectionKind.BODY, text="t", seconds=True),
    ],
)
def test_sections_reject_invalid_values(bad) -> None:
    with pytest.raises(ValueError):
        ScriptSection(**bad)


def test_a_chapter_has_a_title() -> None:
    chapter = ScriptSection.create(SectionKind.CHAPTER, "Text", title=" Fees ")
    assert chapter.title == "Fees"


def test_sections_round_trip_as_dicts() -> None:
    chapter = ScriptSection(SectionKind.CHAPTER, "Text", "Fees", 90)
    assert ScriptSection.from_dict(chapter.as_dict()) == chapter


# Duration


def test_duration_target_comes_from_the_format_of_the_content_type() -> None:
    assert DurationTarget.from_format(FORMAT, ContentType.SHORTS) == DurationTarget(
        15, 60
    )
    assert DurationTarget.from_format(FORMAT, ContentType.LONGFORM) == DurationTarget(
        480, 900
    )
    for bad in ((0, 10), (10, 14_401), (60, 15)):
        with pytest.raises(ValueError):
            DurationTarget(*bad)


def test_estimated_seconds_use_given_seconds_else_words() -> None:
    assert WORDS_PER_MINUTE == 150
    assert ScriptSection(SectionKind.BODY, words(150)).estimated_seconds == 60
    assert ScriptSection(SectionKind.BODY, words(151)).estimated_seconds == 61
    assert ScriptSection(SectionKind.BODY, "Chi phí ngân hàng").words == 4
    assert ScriptSection(SectionKind.BODY, words(500), seconds=7).estimated_seconds == 7
    # hook 6 words -> 3 s, body 20 s given, cta 3 words -> 2 s
    assert new_script().estimated_seconds == 25


def test_within_target_only_reports() -> None:
    assert new_script().within_target is True
    short = Script.create(
        "item-1", "Too short.", duration_target=DurationTarget(15, 60)
    )
    assert short.within_target is False and short.estimated_seconds == 1
    assert Script.create("item-1", "No target.").within_target is None


# Version history


def test_next_version_records_its_parent_author_and_reason() -> None:
    first = new_script(research_report_id="report-1")
    second = first.next_version(
        "A rewrite.", created_by=USER, reason="  shorter hook ", clock=at(T1)
    )

    assert (second.version, second.parent_id) == (2, first.id)
    assert (second.created_by, second.reason, second.created_at) == (
        USER,
        "shorter hook",
        T1,
    )
    # Not given: kept from the parent.
    assert second.duration_target == first.duration_target
    assert (second.strategy_version, second.research_report_id) == (3, "report-1")
    third = second.next_version(
        sections=shorts_sections(), strategy_version=4, research_report_id="report-2"
    )
    assert (third.parent_id, third.strategy_version, third.research_report_id) == (
        second.id,
        4,
        "report-2",
    )
    assert first.parent_id is None and first.created_by == AI


def test_next_version_refuses_unchanged_sections() -> None:
    script = new_script()
    with pytest.raises(ValueError):
        script.next_version(sections=shorts_sections())


@pytest.mark.parametrize(
    "changes",
    [
        dict(parent_id="p"),
        dict(version=2),
        dict(version=2, parent_id="SELF"),
        dict(reason="x" * 501),
        dict(reason=" padded"),
        dict(strategy_version=0),
        dict(research_report_id=" "),
        dict(created_by="ai"),
    ],
)
def test_version_history_rejects_invalid_values(changes) -> None:
    script = new_script()
    values = {**vars(script), **changes}
    if values.get("parent_id") == "SELF":
        values["parent_id"] = script.id
    with pytest.raises((ValueError, TypeError)):
        Script(**values)


# Claims


def test_a_claim_may_name_a_section_of_its_version() -> None:
    script = new_script()

    claim = script.claim(" Most people never check fees. ", section_index=0)

    assert (claim.script_id, claim.section_index, claim.text) == (
        script.id,
        0,
        "Most people never check fees.",
    )
    assert script.claim("Whole script claim").section_index is None
    for bad in (-1, 3, True):
        with pytest.raises(ValueError):
            script.claim("x", section_index=bad)
    with pytest.raises(ValueError):
        Claim.create("s", "x", section_index=100)


# Persistence


@pytest.fixture
def item(database: Database):
    channel = make_channel()
    strategy = make_strategy_profile(channel)
    item = make_content_item(channel, strategy)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(strategy)
        ContentItemRepository(connection).add(item)
    return item


def test_scripts_and_claims_round_trip_with_their_history(database, item) -> None:
    first = Script.create(
        item.id,
        sections=[
            *shorts_sections(),
            ScriptSection(SectionKind.CHAPTER, "Text", "Fees", 90),
        ],
        duration_target=DurationTarget(15, 60),
        created_by=AI,
        reason="first draft",
        strategy_version=3,
        clock=at(T0),
    )
    second = first.next_version("Rewrite.", created_by=USER, clock=at(T1))
    claim = second.claim("A claim.", section_index=0, clock=at(T1))

    with database.transaction() as connection:
        scripts = ScriptRepository(connection)
        scripts.add(second)  # the parent check waits for the commit
        scripts.add(first)
        scripts.add_claim(claim)

    with database.transaction() as connection:
        scripts = ScriptRepository(connection)
        assert scripts.list_by_content_item(item.id) == [first, second]
        assert scripts.list_claims(second.id) == [claim]


def test_a_missing_parent_is_refused_at_commit(database, item) -> None:
    orphan = Script.create(item.id, "One.").next_version("Two.")

    with pytest.raises(sqlite3.IntegrityError), database.transaction() as connection:
        ScriptRepository(connection).add(orphan)


def test_migration_0015_turns_old_scripts_into_body_sections(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    migrate(path, migrations=default_migrations()[:14])
    database = Database(path)
    channel = make_channel()
    strategy = make_strategy_profile(channel)
    item = make_content_item(channel, strategy)
    stamp = format_datetime(T0)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(strategy)
        ContentItemRepository(connection).add(item)
        for script_id, version, text in (("s1", 1, "First."), ("s2", 2, "Second.")):
            connection.execute(
                "INSERT INTO scripts VALUES (?, ?, ?, ?, ?)",
                (script_id, item.id, version, text, stamp),
            )
        connection.execute(
            "INSERT INTO claims VALUES ('c1', 's2', 'A claim.', ?)", (stamp,)
        )

    migrate(path)

    with database.transaction() as connection:
        scripts = ScriptRepository(connection)
        first, second = scripts.list_by_content_item(item.id)
        claims = scripts.list_claims("s2")
    assert first.sections == (ScriptSection(SectionKind.BODY, "First."),)
    assert (first.parent_id, second.parent_id) == (None, "s1")
    assert second.text == "Second." and second.created_by is None
    assert second.duration_target is None
    assert claims[0].section_index is None
