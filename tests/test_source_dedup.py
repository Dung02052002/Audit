"""E-057 Source Deduplication (Prompt Pack v8, prompt #057).

Rules the user approved on 2026-10-02:

- a source keeps a 64-bit simhash of its fetched text (3-word shingles), never
  the text; near-duplicate pages differ in at most 6 bits (raised from 3 after
  measuring), else titles with word Jaccard >= 0.9 are near-duplicates;
- duplicates are grouped and marked, never deleted: the first collected source
  of a group is kept and later ones point to it;
- deduplication is a separate step on a finished request, compares sources of
  that request only, and stores its result once (migration 0009);
- the shared short-text measure (word Jaccard >= 0.8) warns about
  near-duplicate queries in a request; #058 reuses it for topics.
"""

import random
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.research_request import (
    QueryWarning,
    ResearchRequest,
    ResearchRequestStateError,
)
from ai_youtube_agent.content.similarity import (
    MAX_HAMMING_DISTANCE,
    content_fingerprint,
    fingerprint_similarity,
    hamming_distance,
    word_jaccard,
    words,
)
from ai_youtube_agent.content.source import DuplicateReason, Source, SourceDuplicate
from ai_youtube_agent.content.source_collector import (
    ResearchRequestNotFoundError,
    SourceCollector,
)
from ai_youtube_agent.content.source_dedup import (
    SourceDeduplicator,
    find_duplicates,
    match,
)
from ai_youtube_agent.core.audit import Actor, ActorKind, AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository
from ai_youtube_agent.core.db.repositories.research import (
    ResearchRequestRepository,
    SourceRepository,
)
from ai_youtube_agent.providers.mock_research import MockHit, MockResearchProvider
from ai_youtube_agent.providers.research import FetchedDocument
from factories import make_channel

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner")


def article(seed: int, length: int = 400) -> str:
    rng = random.Random(seed)
    return " ".join(rng.choice([f"w{n}" for n in range(3000)]) for _ in range(length))


def edited(text: str, index: int = 200) -> str:
    parts = text.split()
    parts[index] = "changed"
    return " ".join(parts)


BASE = article(1)


# Similarity helpers


def test_words_ignore_case_punctuation_and_script() -> None:
    assert words("Tiết kiệm, ĐẦU TƯ! 2026") == ["tiết", "kiệm", "đầu", "tư", "2026"]


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("Budget tips", "budget TIPS!", 1.0),
        ("budget tips", "budget tricks", 1 / 3),
        ("budget", "", 0.0),
        ("", "", 0.0),
    ],
)
def test_word_jaccard(first, second, expected) -> None:
    assert word_jaccard(first, second) == pytest.approx(expected)


def test_fingerprints_are_stable_hex() -> None:
    fingerprint = content_fingerprint(BASE)

    assert fingerprint == content_fingerprint(BASE)
    assert len(fingerprint) == 16 and int(fingerprint, 16) >= 0
    assert content_fingerprint(BASE.upper() + " !!") == fingerprint


def test_texts_without_words_have_no_fingerprint() -> None:
    assert content_fingerprint("") is None
    assert content_fingerprint(" ... !! ") is None
    assert content_fingerprint("one two") is not None


@pytest.mark.parametrize("seed", range(10))
def test_one_word_edits_stay_within_the_threshold(seed: int) -> None:
    text = article(seed)

    distance = hamming_distance(
        content_fingerprint(text), content_fingerprint(edited(text))
    )

    assert distance <= MAX_HAMMING_DISTANCE


@pytest.mark.parametrize("seed", range(10))
def test_unrelated_texts_are_far_apart(seed: int) -> None:
    distance = hamming_distance(
        content_fingerprint(article(seed)), content_fingerprint(article(seed + 100))
    )

    assert distance > 2 * MAX_HAMMING_DISTANCE


def test_hamming_needs_valid_fingerprints() -> None:
    assert fingerprint_similarity("0" * 16, "0" * 16) == 1.0
    assert fingerprint_similarity("0" * 16, "f" * 16) == 0.0
    with pytest.raises(ValueError):
        hamming_distance("xyz", "0" * 16)


# The source fingerprint


def document(url: str, text: str, title: str = "Page") -> FetchedDocument:
    return FetchedDocument(url, url, title, text, "text/html", T0, "mock")


def test_from_fetch_stores_the_fingerprint_not_the_text(database: Database) -> None:
    source = Source.from_fetch(document("https://a.org/", BASE))
    with database.transaction() as connection:
        SourceRepository(connection).add(source)
        row = connection.execute("SELECT * FROM sources").fetchone()

    assert source.content_fingerprint == content_fingerprint(BASE)
    assert source.content_fingerprint in tuple(row)
    assert BASE not in tuple(row)
    with database.transaction() as connection:
        assert SourceRepository(connection).get(source.id) == source


def test_a_bad_fingerprint_is_refused(database: Database) -> None:
    with pytest.raises(ValueError):
        Source.create("https://a.org/", "T", "mock", T0, content_fingerprint="XYZ")
    with database.transaction() as connection:
        SourceRepository(connection).add(
            Source.create("https://a.org/", "T", "mock", T0)
        )
    with pytest.raises(sqlite3.IntegrityError), database.transaction() as conn:
        conn.execute("UPDATE sources SET content_fingerprint = 'ABCDEF0123456789'")


# Matching


def source(url: str, title: str = "Title", text: str | None = None) -> Source:
    return Source.create(
        url,
        title,
        "mock",
        T0,
        content_fingerprint=content_fingerprint(text) if text else None,
    )


def test_content_matches_before_titles() -> None:
    first = source("https://a.org/", "Budget guide", BASE)
    copy = source("https://b.org/", "Completely different", edited(BASE))

    duplicate = match(first, copy)

    assert duplicate.reason is DuplicateReason.FINGERPRINT
    assert duplicate.duplicate_of == first.id
    assert duplicate.similarity >= 1 - MAX_HAMMING_DISTANCE / 64


def test_titles_match_when_content_is_unknown_or_different() -> None:
    first = source("https://a.org/", "The Complete Budget Guide for 2026", BASE)
    retitled = source("https://b.org/", "the complete budget guide for 2026!")
    other_text = source(
        "https://c.org/", "The complete budget guide for 2026", article(50)
    )

    assert match(first, retitled).reason is DuplicateReason.TITLE
    assert match(first, other_text).reason is DuplicateReason.TITLE
    assert match(first, source("https://d.org/", "Index funds explained")) is None


def test_groups_point_to_the_first_kept_source() -> None:
    a = source("https://a.org/", "A", BASE)
    b = source("https://b.org/", "B", article(7))
    a2 = source("https://a2.org/", "A2", edited(BASE))
    a3 = source("https://a3.org/", "A3", edited(edited(BASE), 100))
    b2 = source("https://b2.org/", "B2", edited(article(7)))

    kept, duplicates = find_duplicates([a, b, a2, a3, b2])

    assert kept == (a.id, b.id)
    assert [(d.source_id, d.duplicate_of) for d in duplicates] == [
        (a2.id, a.id),
        (a3.id, a.id),
        (b2.id, b.id),
    ]


def test_a_source_cannot_duplicate_itself() -> None:
    with pytest.raises(ValueError):
        SourceDuplicate("s1", "s1", DuplicateReason.TITLE, 1.0)
    with pytest.raises(ValueError):
        SourceDuplicate("s1", "s2", DuplicateReason.TITLE, 1.5)


# Query warnings (the shared short-text measure)


def test_near_duplicate_queries_are_warned_not_refused() -> None:
    request = ResearchRequest.create(
        "c1",
        ["best budget apps 2026", "Best budget apps, 2026 edition", "index funds"],
        actor=USER,
    )

    assert request.query_warnings == (
        QueryWarning("best budget apps 2026", "Best budget apps, 2026 edition", 0.8),
    )


# The deduplicator


class World:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.provider = MockResearchProvider(clock=lambda: T0)
        self.collector = SourceCollector(
            database,
            self.provider,
            AuditLog(InMemoryAuditSink()),
            sleep=lambda seconds: None,
        )
        self.deduplicator = SourceDeduplicator(database, clock=lambda: T0)
        self.channel = make_channel()
        with database.transaction() as connection:
            ChannelRepository(connection).add(self.channel)

    def page(self, url: str, text: str, title: str) -> MockHit:
        self.provider.add_page(url, text, title=title)
        return MockHit(url, title)

    def finished(self, hits) -> ResearchRequest:
        self.provider.add_results("q", hits)
        request = self.collector.request(self.channel.id, ["q"], actor=USER)
        return self.collector.collect(request.id)


def test_deduplicate_groups_and_stores(database: Database) -> None:
    world = World(database)
    request = world.finished(
        [
            world.page("https://a.org/", BASE, "Original"),
            world.page("https://b.org/", article(9), "Unrelated"),
            world.page("https://mirror.org/", edited(BASE), "Copied post"),
            world.page("https://c.org/", article(11), "Original!"),
        ]
    )
    ids = [c.source_id for c in request.collected]

    result = world.deduplicator.deduplicate(request.id)

    assert result.kept == (ids[0], ids[1])
    assert [(d.source_id, d.duplicate_of, d.reason) for d in result.duplicates] == [
        (ids[2], ids[0], DuplicateReason.FINGERPRINT),
        (ids[3], ids[0], DuplicateReason.TITLE),
    ]
    assert result.groups == {ids[0]: (ids[2], ids[3]), ids[1]: ()}
    assert result.deduplicated_at == T0
    with database.transaction() as connection:
        stored = ResearchRequestRepository(connection).get(request.id)
    assert [c.source_id for c in stored.collected] == ids  # nothing removed


def test_running_again_returns_the_stored_result(database: Database) -> None:
    world = World(database)
    request = world.finished(
        [
            world.page("https://a.org/", BASE, "One"),
            world.page("https://b.org/", edited(BASE), "Two"),
        ]
    )
    first = world.deduplicator.deduplicate(request.id)
    later = SourceDeduplicator(database, clock=lambda: T0 + timedelta(days=1))

    assert later.deduplicate(request.id) == first


def test_a_request_without_duplicates_keeps_everything(database: Database) -> None:
    world = World(database)
    request = world.finished([world.page("https://a.org/", BASE, "One")])

    result = world.deduplicator.deduplicate(request.id)

    assert result.kept == (request.collected[0].source_id,)
    assert result.duplicates == ()


def test_only_finished_requests_are_deduplicated(database: Database) -> None:
    world = World(database)
    pending = world.collector.request(world.channel.id, ["q"], actor=USER)

    with pytest.raises(ResearchRequestStateError):
        world.deduplicator.deduplicate(pending.id)
    with pytest.raises(ResearchRequestNotFoundError):
        world.deduplicator.deduplicate("missing")


def test_bootstrap_registers_the_deduplicator(tmp_path) -> None:
    container = build_container(
        Settings(environment=Environment.TEST, database_path=tmp_path / "a.db")
    )

    assert isinstance(container.resolve(SourceDeduplicator), SourceDeduplicator)
