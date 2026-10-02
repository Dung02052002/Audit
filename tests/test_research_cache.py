"""E-061 Research Cache (Prompt Pack v8, prompt #061).

Rules the user approved on 2026-10-03:

- searches and fetches are cached, research reports are not;
- search key = query + language + market + limit; fetch key = normalised URL;
- TTL: searches 6 hours, fetches 24 hours;
- stale entries are not deleted; a stale entry is refreshed through the
  provider, and on a retryable provider error the stale value is returned and
  marked stale (``CacheInfo``, an optional field on the results);
- SQLite storage; ``ResearchCache`` wraps a ``ResearchProvider`` without
  changing its interface; the strategy is never written.
"""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from ai_youtube_agent.content.source_collector import SourceCollector
from ai_youtube_agent.core.audit import Actor, ActorKind, AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.providers.mock_research import (
    MockHit,
    MockResearchProvider,
    Operation,
)
from ai_youtube_agent.providers.research import (
    CacheInfo,
    CacheOutcome,
    ResearchErrorCode,
    ResearchProvider,
    ResearchProviderError,
    SearchQuery,
)
from ai_youtube_agent.providers.research_cache import (
    FETCH_TTL,
    SEARCH_TTL,
    ResearchCache,
    fetch_key,
    search_key,
)
from factories import make_channel, make_strategy_profile

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
URL = "https://example.com/guide"
QUERY = SearchQuery("Index funds", language="vi", market="VN", max_results=5)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class World:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.clock = Clock()
        self.mock = MockResearchProvider(clock=self.clock)
        self.mock.add_results(
            "index funds",
            [MockHit(URL, "Guide", "snippet", published_at=T0 - timedelta(days=2))],
        )
        self.mock.add_page(URL, "Body text", title="Guide")
        self.cache = ResearchCache(self.mock, database, clock=self.clock)

    def provider_calls(self, operation: str) -> int:
        return len([c for c in self.mock.calls if c[0] == operation])


# Keys


def test_search_key_uses_query_language_market_and_limit() -> None:
    assert search_key(QUERY) == "index funds|vi|VN|5"
    assert search_key(SearchQuery("  INDEX   funds ", "vi", "VN", 5)) == search_key(
        QUERY
    )
    others = [
        SearchQuery("Index funds", "en", "VN", 5),
        SearchQuery("Index funds", "vi", "US", 5),
        SearchQuery("Index funds", "vi", "VN", 6),
        SearchQuery("Index fund", "vi", "VN", 5),
    ]
    assert len({search_key(q) for q in others} | {search_key(QUERY)}) == 5


def test_fetch_key_is_the_normalised_url() -> None:
    assert fetch_key("HTTPS://Example.com/guide/?utm_source=x#top") == URL


# Search


def test_search_miss_then_hit(database: Database) -> None:
    world = World(database)

    first = world.cache.search(QUERY)
    world.clock.advance(SEARCH_TTL - timedelta(seconds=1))
    second = world.cache.search(QUERY)

    assert first.cache == CacheInfo(CacheOutcome.MISS, T0)
    assert second.cache == CacheInfo(CacheOutcome.HIT, T0)
    assert not second.cache.stale
    assert world.provider_calls("search") == 1
    assert second.hits == first.hits
    assert second.hits[0].published_at == T0 - timedelta(days=2)
    assert second.query == QUERY


def test_stale_search_is_refreshed(database: Database) -> None:
    world = World(database)
    world.cache.search(QUERY)
    world.clock.advance(SEARCH_TTL)

    refreshed = world.cache.search(QUERY)

    assert refreshed.cache == CacheInfo(CacheOutcome.REFRESH, T0 + SEARCH_TTL)
    assert world.provider_calls("search") == 2
    assert world.cache.search(QUERY).cache.outcome is CacheOutcome.HIT


def test_a_different_limit_is_a_separate_entry(database: Database) -> None:
    world = World(database)
    world.cache.search(QUERY)

    other = world.cache.search(SearchQuery("Index funds", "vi", "VN", 1))

    assert other.cache.outcome is CacheOutcome.MISS
    assert world.provider_calls("search") == 2


# Fetch


def test_fetch_miss_hit_and_shared_key(database: Database) -> None:
    world = World(database)

    first = world.cache.fetch(URL)
    world.clock.advance(FETCH_TTL - timedelta(seconds=1))
    second = world.cache.fetch(URL + "?utm_source=feed")

    assert first.cache.outcome is CacheOutcome.MISS
    assert second.cache == CacheInfo(CacheOutcome.HIT, T0)
    assert second.url == URL + "?utm_source=feed"  # the caller's URL
    assert (second.final_url, second.text, second.title) == (URL, "Body text", "Guide")
    assert world.provider_calls("fetch") == 1


def test_fetch_ttl_is_24_hours(database: Database) -> None:
    world = World(database)
    world.cache.fetch(URL)
    world.clock.advance(timedelta(hours=7))
    assert world.cache.fetch(URL).cache.outcome is CacheOutcome.HIT

    world.clock.advance(timedelta(hours=17))

    assert world.cache.fetch(URL).cache.outcome is CacheOutcome.REFRESH


def test_truncation_is_kept(database: Database) -> None:
    world = World(database)
    world.mock.add_page(URL, "x" * 200_005)
    world.cache.fetch(URL)

    assert world.cache.fetch(URL).truncated


# Stale fallback and errors


@pytest.mark.parametrize(
    "code",
    [
        ResearchErrorCode.UNAVAILABLE,
        ResearchErrorCode.RATE_LIMITED,
        ResearchErrorCode.TIMEOUT,
    ],
)
def test_retryable_errors_fall_back_to_stale(database: Database, code) -> None:
    world = World(database)
    world.cache.fetch(URL)
    world.clock.advance(FETCH_TTL)
    world.mock.fail_next(code, operation=Operation.FETCH)

    document = world.cache.fetch(URL)

    assert document.cache == CacheInfo(CacheOutcome.STALE_FALLBACK, T0)
    assert document.cache.stale
    assert document.text == "Body text"


def test_search_falls_back_to_stale_too(database: Database) -> None:
    world = World(database)
    world.cache.search(QUERY)
    world.clock.advance(SEARCH_TTL)
    world.mock.fail_next(ResearchErrorCode.TIMEOUT, operation=Operation.SEARCH)

    results = world.cache.search(QUERY)

    assert results.cache.stale
    assert results.hits[0].url == URL


def test_a_stale_fallback_keeps_the_entry_for_the_next_refresh(database) -> None:
    world = World(database)
    world.cache.fetch(URL)
    world.clock.advance(FETCH_TTL)
    world.mock.fail_next(ResearchErrorCode.TIMEOUT, operation=Operation.FETCH)
    world.cache.fetch(URL)

    refreshed = world.cache.fetch(URL)

    assert refreshed.cache.outcome is CacheOutcome.REFRESH


def test_permanent_errors_are_raised_even_with_a_stale_entry(database) -> None:
    world = World(database)
    world.cache.fetch(URL)
    world.clock.advance(FETCH_TTL)
    world.mock.fail_next(ResearchErrorCode.BLOCKED, operation=Operation.FETCH)

    with pytest.raises(ResearchProviderError) as caught:
        world.cache.fetch(URL)
    assert caught.value.code == "research.blocked"


def test_errors_without_an_entry_are_raised_and_never_cached(database) -> None:
    world = World(database)
    world.mock.fail_next(ResearchErrorCode.TIMEOUT, operation=Operation.FETCH)

    with pytest.raises(ResearchProviderError):
        world.cache.fetch(URL)
    with pytest.raises(ResearchProviderError):
        world.cache.fetch("https://example.com/missing")

    with database.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM research_cache").fetchone() == (
            0,
        )
    assert world.cache.fetch(URL).cache.outcome is CacheOutcome.MISS


def test_stale_entries_are_never_deleted(database: Database) -> None:
    world = World(database)
    world.cache.search(QUERY)
    world.cache.fetch(URL)
    world.clock.advance(timedelta(days=30))

    with database.transaction() as connection:
        rows = connection.execute(
            "SELECT kind, cached_at FROM research_cache ORDER BY kind"
        ).fetchall()

    assert [r[0] for r in rows] == ["fetch", "search"]


def test_the_table_checks_its_kind(database: Database) -> None:
    World(database).cache.fetch(URL)

    with pytest.raises(sqlite3.IntegrityError), database.transaction() as conn:
        conn.execute("UPDATE research_cache SET kind = 'report'")


# Wrapping


def test_the_cache_is_a_research_provider(database: Database) -> None:
    world = World(database)

    assert isinstance(world.cache, ResearchProvider)
    assert world.cache.name == "mock"
    world.cache.check()
    world.mock.set_healthy(False)
    with pytest.raises(ResearchProviderError):
        world.cache.check()


def test_provider_values_have_no_cache_info(database: Database) -> None:
    world = World(database)

    assert world.mock.search(QUERY).cache is None
    assert world.mock.fetch(URL).cache is None


def test_collection_through_the_cache_does_not_touch_the_strategy(database) -> None:
    world = World(database)
    channel = make_channel()
    strategy = make_strategy_profile(channel)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(strategy)
    collector = SourceCollector(
        database,
        world.cache,
        AuditLog(InMemoryAuditSink()),
        sleep=lambda seconds: None,
    )
    world.mock.add_results("index funds", [MockHit(URL, "Guide")])

    for _ in range(2):
        request = collector.request(
            channel.id, ["index funds"], actor=Actor(ActorKind.USER, "u")
        )
        collector.collect(request.id)

    assert world.provider_calls("search") == 1
    assert world.provider_calls("fetch") == 1
    with database.transaction() as connection:
        assert StrategyProfileRepository(connection).get_by_channel(channel.id) == (
            strategy
        )
