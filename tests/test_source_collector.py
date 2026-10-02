"""E-056 Source Collector (Prompt Pack v8, prompt #056).

Rules the user approved on 2026-10-02:

- a ``ResearchRequest`` is stored (migration 0008) with the caller's 1-10
  queries, language and market copied from the strategy, limits and a status
  (pending, running, completed, partial, failed); the sources it collects are
  linked in order with the query and rank that found each;
- the collector runs sequentially and retries each retryable search or fetch
  up to 3 attempts (waits 1 s and 2 s); other failures are recorded at once;
  resuming a whole request is #062;
- limits: ``max_sources`` (1-100, default 20), ``max_results_per_query``
  (1-50, default 10), ``max_per_domain`` (1-20, default 3);
- a hit whose fetch fails is not a source, a failed search skips that query,
  and both are recorded; no cost records.
"""

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.channel import ChannelArchivedError, ChannelStatus
from ai_youtube_agent.content.channel_settings import ChannelNotFoundError
from ai_youtube_agent.content.research_request import (
    CollectedSource,
    CollectionFailure,
    CollectionOperation,
    ResearchLimits,
    ResearchRequest,
    ResearchRequestStateError,
    ResearchStatus,
)
from ai_youtube_agent.content.source import Source
from ai_youtube_agent.content.source_collector import (
    ResearchRequestNotFoundError,
    SourceCollector,
)
from ai_youtube_agent.core.audit import Actor, ActorKind, AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.db.database import ConcurrencyError, Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.research import (
    ResearchRequestRepository,
    SourceRepository,
)
from ai_youtube_agent.providers.mock_research import (
    MockHit,
    MockResearchProvider,
    Operation,
)
from ai_youtube_agent.providers.research import ResearchErrorCode, SearchQuery
from factories import make_channel, make_strategy_profile

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner")
SYSTEM = Actor(ActorKind.SYSTEM, "pipeline")


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


class SpyProvider(MockResearchProvider):
    def __init__(self) -> None:
        super().__init__(clock=lambda: T0)
        self.queries: list[SearchQuery] = []

    def search(self, query: SearchQuery):
        self.queries.append(query)
        return super().search(query)


def page(provider: MockResearchProvider, url: str, **kwargs) -> MockHit:
    provider.add_page(url, f"Text of {url}", title=f"Title {url}", **kwargs)
    return MockHit(url, f"Hit {url}")


class World:
    def __init__(self, database: Database, *, strategy: bool = True) -> None:
        self.database = database
        self.provider = SpyProvider()
        self.sink = InMemoryAuditSink()
        self.sleeps: list[float] = []
        self.collector = SourceCollector(
            database,
            self.provider,
            AuditLog(self.sink),
            clock=Clock(),
            sleep=self.sleeps.append,
        )
        self.channel = make_channel()
        with database.transaction() as connection:
            ChannelRepository(connection).add(self.channel)
            if strategy:
                StrategyProfileRepository(connection).add(
                    make_strategy_profile(self.channel)
                )

    def run(self, queries, limits: ResearchLimits | None = None) -> ResearchRequest:
        request = self.collector.request(
            self.channel.id, queries, limits=limits, actor=SYSTEM
        )
        return self.collector.collect(request.id)

    def sources(self, request: ResearchRequest) -> list[Source]:
        with self.database.transaction() as connection:
            return SourceRepository(connection).list_by_ids(
                [c.source_id for c in request.collected]
            )

    def actions(self) -> list[str]:
        return [event.action for event in self.sink.events()]


# The entity


def test_limits_defaults_and_ranges() -> None:
    assert ResearchLimits() == ResearchLimits(20, 10, 3)
    ResearchLimits(100, 50, 20)
    for bad in ((0, 10, 3), (101, 10, 3), (20, 51, 3), (20, 10, 21), (True, 10, 3)):
        with pytest.raises(ValueError):
            ResearchLimits(*bad)


@pytest.mark.parametrize(
    "queries",
    [[], [f"q{n}" for n in range(11)], [" "], ["x" * 501], ["Budget", "budget "]],
)
def test_queries_are_1_to_10_unique_texts(queries) -> None:
    with pytest.raises(ValueError):
        ResearchRequest.create("c1", queries, actor=USER)


def test_a_request_starts_pending_and_moves_once() -> None:
    clock = Clock()
    request = ResearchRequest.create("c1", [" budget "], actor=USER, clock=clock)

    assert request.queries == ("budget",)
    assert request.status is ResearchStatus.PENDING
    running = request.start(clock=clock)
    assert running.status is ResearchStatus.RUNNING and running.started_at
    with pytest.raises(ResearchRequestStateError):
        running.start(clock=clock)
    with pytest.raises(ResearchRequestStateError):
        request.finish([], [], clock=clock)
    done = running.finish([], [], clock=clock)
    assert done.status is ResearchStatus.COMPLETED and done.is_finished
    with pytest.raises(ResearchRequestStateError):
        done.finish([], [], clock=clock)


@pytest.mark.parametrize(
    ("collected", "failed", "status"),
    [
        (False, False, ResearchStatus.COMPLETED),
        (True, False, ResearchStatus.COMPLETED),
        (True, True, ResearchStatus.PARTIAL),
        (False, True, ResearchStatus.FAILED),
    ],
)
def test_the_final_status_follows_sources_and_failures(collected, failed, status):
    running = ResearchRequest.create("c1", ["q"], actor=USER).start()
    sources = [CollectedSource("s1", "q", 1)] if collected else []
    failures = (
        [CollectionFailure(CollectionOperation.FETCH, "https://x.org", "e.x", 3)]
        if failed
        else []
    )

    assert running.finish(sources, failures).status is status


def test_a_request_cannot_hold_more_than_max_sources() -> None:
    running = ResearchRequest.create(
        "c1", ["q"], limits=ResearchLimits(max_sources=1), actor=USER
    ).start()

    with pytest.raises(ValueError):
        running.finish(
            [CollectedSource("s1", "q", 1), CollectedSource("s2", "q", 2)], []
        )


# Making a request


def test_request_copies_language_and_market_from_the_strategy(database) -> None:
    world = World(database)

    request = world.collector.request(world.channel.id, ["budgeting"], actor=USER)

    assert (request.language, request.market) == ("vi", "VN")
    assert request.requested_by == USER
    with database.transaction() as connection:
        assert ResearchRequestRepository(connection).get(request.id) == request
    assert world.actions() == ["research.requested"]


def test_request_without_a_strategy_has_no_language_or_market(database) -> None:
    world = World(database, strategy=False)

    request = world.collector.request(world.channel.id, ["q"], actor=USER)

    assert (request.language, request.market) == (None, None)


def test_request_refuses_unknown_and_archived_channels(database) -> None:
    world = World(database)
    with pytest.raises(ChannelNotFoundError):
        world.collector.request("missing", ["q"], actor=USER)

    archived = dataclasses.replace(make_channel(), status=ChannelStatus.ARCHIVED)
    with database.transaction() as connection:
        ChannelRepository(connection).add(archived)
    with pytest.raises(ChannelArchivedError):
        world.collector.request(archived.id, ["q"], actor=USER)


# Collecting


def test_collect_stores_sources_in_order(database) -> None:
    world = World(database)
    p = world.provider
    p.add_results("budget", [page(p, "https://a.org/1"), page(p, "https://b.org/1")])
    p.add_results("index funds", [page(p, "https://c.org/1")])

    request = world.run(["budget", "index funds"])

    assert request.status is ResearchStatus.COMPLETED
    assert [(c.query, c.rank) for c in request.collected] == [
        ("budget", 1),
        ("budget", 2),
        ("index funds", 1),
    ]
    assert [s.normalized_url for s in world.sources(request)] == [
        "https://a.org/1",
        "https://b.org/1",
        "https://c.org/1",
    ]
    with database.transaction() as connection:
        assert ResearchRequestRepository(connection).get(request.id) == request
    assert world.actions() == ["research.requested", "research.completed"]
    assert world.sleeps == []


def test_searches_use_the_request_language_market_and_limit(database) -> None:
    world = World(database)

    world.run(["budget"], ResearchLimits(max_results_per_query=7))

    [query] = world.provider.queries
    assert (query.language, query.market, query.max_results) == ("vi", "VN", 7)


def test_collection_stops_at_max_sources(database) -> None:
    world = World(database)
    p = world.provider
    p.add_results("q", [page(p, f"https://s{n}.org/") for n in range(5)])

    request = world.run(["q", "other"], ResearchLimits(max_sources=2))

    assert len(request.collected) == 2
    assert [c for c in p.calls if c[0] == "fetch"] == [
        ("fetch", "https://s0.org/"),
        ("fetch", "https://s1.org/"),
    ]
    assert [c for c in p.calls if c[0] == "search"] == [("search", "q")]


def test_hosts_are_capped_without_fetching_the_rest(database) -> None:
    world = World(database)
    p = world.provider
    p.add_results(
        "q",
        [page(p, f"https://Same.org/{n}") for n in range(4)]
        + [page(p, "https://other.org/")],
    )

    request = world.run(["q"], ResearchLimits(max_per_domain=2))

    assert [s.normalized_url for s in world.sources(request)] == [
        "https://same.org/0",
        "https://same.org/1",
        "https://other.org/",
    ]
    assert ("fetch", "https://Same.org/2") not in p.calls


def test_the_same_page_from_two_queries_is_collected_once(database) -> None:
    world = World(database)
    p = world.provider
    hit = page(p, "https://a.org/x")
    p.add_results("one", [hit])
    p.add_results("two", [MockHit("https://a.org/x/?utm_source=feed", "Again")])

    request = world.run(["one", "two"])

    assert len(request.collected) == 1
    assert len([c for c in p.calls if c[0] == "fetch"]) == 1


def test_a_redirect_to_a_collected_page_is_not_counted_twice(database) -> None:
    world = World(database)
    p = world.provider
    first = page(p, "https://a.org/new")
    moved = page(p, "https://a.org/old", final_url="https://a.org/new")
    p.add_results("q", [first, moved])

    request = world.run(["q"])

    assert len(request.collected) == 1


def test_a_page_known_from_an_earlier_request_is_reused(database) -> None:
    world = World(database)
    p = world.provider
    p.add_results("q", [page(p, "https://a.org/")])

    first = world.run(["q"])
    second = world.run(["q"])

    assert first.collected[0].source_id == second.collected[0].source_id


def test_a_search_with_no_hits_completes_with_no_sources(database) -> None:
    world = World(database)

    request = world.run(["nothing here"])

    assert request.status is ResearchStatus.COMPLETED
    assert request.collected == () and request.failures == ()


# Retries and failures


def test_retryable_failures_are_retried_with_backoff(database) -> None:
    world = World(database)
    p = world.provider
    p.add_results("q", [page(p, "https://a.org/")])
    p.fail_next(ResearchErrorCode.TIMEOUT, times=2, operation=Operation.FETCH)

    request = world.run(["q"])

    assert request.status is ResearchStatus.COMPLETED
    assert len(request.collected) == 1
    assert world.sleeps == [1.0, 2.0]


def test_retryable_failures_give_up_after_three_attempts(database) -> None:
    world = World(database)
    p = world.provider
    p.add_results("q", [page(p, "https://a.org/"), page(p, "https://b.org/")])
    p.fail_next(ResearchErrorCode.RATE_LIMITED, times=3, operation=Operation.FETCH)

    request = world.run(["q"])

    assert request.status is ResearchStatus.PARTIAL
    assert request.failures == (
        CollectionFailure(
            CollectionOperation.FETCH, "https://a.org/", "research.rate_limited", 3
        ),
    )
    assert [s.normalized_url for s in world.sources(request)] == ["https://b.org/"]
    assert world.actions()[-1] == "research.partial"


def test_permanent_failures_are_not_retried(database) -> None:
    world = World(database)
    p = world.provider
    p.add_results("q", [MockHit("https://gone.org/", "Gone")])

    request = world.run(["q"])

    assert request.status is ResearchStatus.FAILED
    assert request.failures[0].code == "research.not_found"
    assert request.failures[0].attempts == 1
    assert world.sleeps == []
    assert world.actions()[-1] == "research.failed"


def test_a_failed_search_skips_only_that_query(database) -> None:
    world = World(database)
    p = world.provider
    p.add_results("good", [page(p, "https://a.org/")])
    p.fail_next(ResearchErrorCode.INVALID_QUERY, operation=Operation.SEARCH)

    request = world.run(["bad", "good"])

    assert request.status is ResearchStatus.PARTIAL
    assert request.failures == (
        CollectionFailure(
            CollectionOperation.SEARCH, "bad", "research.invalid_query", 1
        ),
    )
    assert len(request.collected) == 1


def test_collect_runs_a_request_only_once(database) -> None:
    world = World(database)
    request = world.run(["q"])

    with pytest.raises(ResearchRequestStateError):
        world.collector.collect(request.id)
    with pytest.raises(ResearchRequestNotFoundError) as caught:
        world.collector.collect("missing")
    assert caught.value.to_public().code == "domain.research_request_not_found"


# Persistence


def test_requests_update_optimistically(database) -> None:
    world = World(database)
    request = world.collector.request(world.channel.id, ["q"], actor=USER)
    running = request.start(clock=lambda: T0 + timedelta(hours=1))
    with database.transaction() as connection:
        ResearchRequestRepository(connection).update(
            running, expected_updated_at=request.updated_at
        )

    with pytest.raises(ConcurrencyError), database.transaction() as connection:
        ResearchRequestRepository(connection).update(
            running, expected_updated_at=request.updated_at
        )


def test_requests_list_by_channel(database) -> None:
    world = World(database)
    first = world.collector.request(world.channel.id, ["a"], actor=USER)
    second = world.collector.request(world.channel.id, ["b"], actor=USER)

    with database.transaction() as connection:
        listed = ResearchRequestRepository(connection).list_by_channel(world.channel.id)

    assert [r.id for r in listed] == [first.id, second.id]


def test_bootstrap_registers_the_collector(tmp_path) -> None:
    container = build_container(
        Settings(environment=Environment.TEST, database_path=tmp_path / "a.db")
    )

    assert isinstance(container.resolve(SourceCollector), SourceCollector)
