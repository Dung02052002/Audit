"""E-055 Source Model (Prompt Pack v8, prompt #055).

Rules the user approved on 2026-10-02:

- URLs are normalised: lower-case scheme and host, no credentials, no default
  port, no fragment, no tracking parameters (``utm_*``, ``fbclid``, ``gclid``,
  ``mc_cid``, ``mc_eid``), sorted query, no trailing slash except the root;
  http and https are kept; the normalised form comes from the final URL, and
  the requested URL is kept too;
- a source has an optional ``published_at`` and a required ``retrieved_at``
  (UTC), a whitespace-collapsed title (at most 300) and 0 to 20 evidence notes
  (note at most 500, optional verbatim quote at most 1,000);
- sources are stored now (migration 0007, ``SourceRepository``), add only, one
  per normalised URL; the id is random and ``Evidence.source_ref`` holds it.
"""

import sqlite3
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ai_youtube_agent.content.script import Evidence
from ai_youtube_agent.content.source import (
    MAX_NOTES,
    EvidenceNote,
    Source,
    normalize_title,
    normalize_url,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.research import SourceRepository
from ai_youtube_agent.providers.research import FetchedDocument, SearchHit

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
URL = "https://example.com/guide"


def document(**changes) -> FetchedDocument:
    values = dict(
        url=URL,
        final_url=URL,
        title="A  budgeting\n guide",
        text="Spend less than you earn. Save the rest.",
        media_type="text/html",
        fetched_at=T0,
        provider="mock",
    )
    return FetchedDocument(**{**values, **changes})


# URL normalisation


@pytest.mark.parametrize(
    ("url", "normalized"),
    [
        ("HTTPS://Example.COM/Guide", "https://example.com/Guide"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("http://example.com:8080/a", "http://example.com:8080/a"),
        ("https://example.com/a#part-2", "https://example.com/a"),
        ("https://example.com/a/", "https://example.com/a"),
        ("https://example.com", "https://example.com/"),
        ("https://example.com/", "https://example.com/"),
        ("https://user:pw@example.com/a", "https://example.com/a"),
        ("https://example.com/a?b=2&a=1", "https://example.com/a?a=1&b=2"),
        (
            "https://example.com/a?utm_source=x&id=7&UTM_Medium=y&fbclid=z",
            "https://example.com/a?id=7",
        ),
        ("https://example.com/a?gclid=1&mc_cid=2&mc_eid=3", "https://example.com/a"),
        ("https://example.com/a?q=&x=1", "https://example.com/a?q=&x=1"),
        ("http://example.com/a", "http://example.com/a"),
        ("https://[2001:DB8::1]:443/a", "https://[2001:db8::1]/a"),
    ],
)
def test_normalize_url(url: str, normalized: str) -> None:
    assert normalize_url(url) == normalized


def test_normalisation_is_stable() -> None:
    url = "HTTPS://Example.com:443/a/?utm_x=1&b=2&a=1#f"
    assert normalize_url(normalize_url(url)) == normalize_url(url)


@pytest.mark.parametrize("url", ["ftp://example.com/a", "example.com/a", "https://"])
def test_only_absolute_http_urls_normalise(url: str) -> None:
    with pytest.raises(ValueError):
        normalize_url(url)


def test_titles_are_collapsed_and_cut() -> None:
    assert normalize_title("  A \n\t title  ") == "A title"
    assert len(normalize_title("x " * 400)) <= 300


# The entity


def test_create_normalises_from_the_final_url() -> None:
    source = Source.create(
        "http://example.com/old?utm_source=feed",
        " Budget  guide ",
        "mock",
        T0,
        final_url="https://Example.com/guide/",
    )

    assert source.url == "http://example.com/old?utm_source=feed"
    assert source.final_url == "https://Example.com/guide/"
    assert source.normalized_url == "https://example.com/guide"
    assert source.title == "Budget guide"
    assert source.published_at is None
    assert len(source.id) == 32


def test_from_fetch_uses_page_and_hit() -> None:
    hit = SearchHit(URL, "Hit title", 1, published_at=T0 - timedelta(days=3))
    note = EvidenceNote("States the core rule", "Spend less than you earn.")

    source = Source.from_fetch(document(), hit=hit, evidence_notes=[note])

    assert source.title == "A budgeting guide"
    assert source.published_at == T0 - timedelta(days=3)
    assert source.retrieved_at == T0
    assert source.provider == "mock"
    assert source.evidence_notes == (note,)


def test_from_fetch_falls_back_to_the_hit_title_then_the_url() -> None:
    hit = SearchHit(URL, "Hit title", 1)

    assert Source.from_fetch(document(title=None), hit=hit).title == "Hit title"
    assert Source.from_fetch(document(title="  ")).title == URL


def test_quotes_must_come_from_the_fetched_text() -> None:
    with pytest.raises(ValueError):
        Source.from_fetch(
            document(), evidence_notes=[EvidenceNote("n", "Not in the page")]
        )


def valid(**changes) -> Source:
    source = Source.create(URL, "Guide", "mock", T0)
    return Source(**{**source.__dict__, **changes})


@pytest.mark.parametrize(
    "changes",
    [
        {"id": ""},
        {"url": "nope"},
        {"normalized_url": "https://example.com/other"},
        {"title": " "},
        {"title": "x" * 301},
        {"title": "two  spaces"},
        {"provider": "Mock Provider"},
        {"published_at": datetime(2026, 1, 1)},
        {"retrieved_at": T0.astimezone(timezone(timedelta(hours=7)))},
        {"evidence_notes": tuple(EvidenceNote(f"n{i}") for i in range(MAX_NOTES + 1))},
    ],
)
def test_source_limits(changes) -> None:
    with pytest.raises(ValueError):
        valid(**changes)


@pytest.mark.parametrize(
    "build",
    [
        lambda: EvidenceNote(" "),
        lambda: EvidenceNote("n" * 501),
        lambda: EvidenceNote("n", " "),
        lambda: EvidenceNote("n", "q" * 1001),
    ],
)
def test_note_limits(build) -> None:
    with pytest.raises(ValueError):
        build()


def test_notes_must_be_a_tuple_of_notes() -> None:
    with pytest.raises(TypeError):
        valid(evidence_notes=["note"])


def test_as_dict() -> None:
    source = Source.create(
        URL,
        "Guide",
        "mock",
        T0,
        published_at=T0 - timedelta(days=1),
        evidence_notes=[EvidenceNote("n", "q")],
    )

    assert source.as_dict() == {
        "id": source.id,
        "url": URL,
        "final_url": URL,
        "normalized_url": URL,
        "title": "Guide",
        "provider": "mock",
        "published_at": "2026-10-01T12:00:00+00:00",
        "retrieved_at": "2026-10-02T12:00:00+00:00",
        "evidence_notes": [{"note": "n", "quote": "q"}],
        "content_fingerprint": None,  # E-057
    }


# Persistence


def test_a_source_round_trips(database: Database) -> None:
    source = Source.create(
        URL,
        "Guide",
        "mock",
        T0,
        published_at=T0 - timedelta(days=1),
        evidence_notes=[EvidenceNote("a"), EvidenceNote("b", "q")],
    )
    with database.transaction() as connection:
        SourceRepository(connection).add(source)

    with database.transaction() as connection:
        repository = SourceRepository(connection)
        assert repository.get(source.id) == source
        assert repository.get_by_normalized_url(URL) == source
        assert repository.get("missing") is None


def test_one_source_per_normalised_url(database: Database) -> None:
    first = Source.create(URL, "Guide", "mock", T0)
    again = Source.create(URL + "/?utm_source=x", "Guide again", "mock", T0)
    with database.transaction() as connection:
        SourceRepository(connection).add(first)

    with pytest.raises(sqlite3.IntegrityError), database.transaction() as conn:
        SourceRepository(conn).add(again)
    with database.transaction() as connection:
        assert SourceRepository(connection).add_or_get(again) == first


def test_add_or_get_stores_a_new_source(database: Database) -> None:
    source = Source.create(URL, "Guide", "mock", T0)
    with database.transaction() as connection:
        assert SourceRepository(connection).add_or_get(source) == source

    with database.transaction() as connection:
        assert SourceRepository(connection).get(source.id) == source


def test_list_by_ids_keeps_the_asked_order(database: Database) -> None:
    a = Source.create("https://example.com/a", "A", "mock", T0)
    b = Source.create("https://example.com/b", "B", "mock", T0)
    with database.transaction() as connection:
        repository = SourceRepository(connection)
        repository.add(a)
        repository.add(b)

    with database.transaction() as connection:
        repository = SourceRepository(connection)
        assert repository.list_by_ids([b.id, "missing", a.id, b.id]) == [b, a]
        assert repository.list_by_ids([]) == []


def test_sources_have_no_update_or_delete() -> None:
    assert not hasattr(SourceRepository, "update")
    assert not hasattr(SourceRepository, "delete")


def test_evidence_refers_to_a_source_by_id() -> None:
    source = Source.create(URL, "Guide", "mock", T0)

    evidence = Evidence.create("claim-1", source.id, "Spend less", clock=lambda: T0)

    assert evidence.source_ref == source.id


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("title", ""),
        ("retrieved_at", "2026-10-02 12:00"),
        ("evidence_notes_json", "{}"),
    ],
)
def test_the_table_checks_its_columns(database: Database, column, value) -> None:
    with database.transaction() as connection:
        SourceRepository(connection).add(Source.create(URL, "Guide", "mock", T0))

    with pytest.raises(sqlite3.IntegrityError), database.transaction() as conn:
        conn.execute(f"UPDATE sources SET {column} = ?", (value,))
