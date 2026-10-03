"""E-062 Research Failure Recovery (Prompt Pack v8, prompt #062).

Rules the user approved on 2026-10-03:

- a running request saves its progress after each collected source or
  failure (and each finished query), renewing a 10-minute lease (migration
  0014: ``queries_done``, ``lease_expires_at``, ``retries``);
- ``resume`` continues a running request whose lease expired, from the saved
  progress; a running request with a live lease is refused (busy);
- ``retry`` reruns only the failed searches and fetches of a partial or
  failed request, keeping its sources; failures record the run (``round``)
  they happened in and fetch failures their hit's query and rank;
- retry is refused once dedup, topics, scores or a report are stored.
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_youtube_agent.content.research_request import (
    LEASE,
    CollectedSource,
    CollectionFailure,
    CollectionOperation,
    ResearchLimits,
    ResearchRequest,
    ResearchRequestStateError,
    ResearchStatus,
)
from ai_youtube_agent.content.source_collector import (
    ResearchRequestBusyError,
    ResearchRequestHasResultsError,
    ResearchRequestNotFoundError,
    SourceCollector,
)
from ai_youtube_agent.core.audit import Actor, ActorKind, AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.db.codec import format_datetime
from ai_youtube_agent.core.db.database import ConcurrencyError, Database
from ai_youtube_agent.core.db.migrate import default_migrations, migrate
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.research import (
    ResearchRequestRepository,
    SourceDuplicateRepository,
    SourceRepository,
)
from ai_youtube_agent.providers.mock_research import (
    MockHit,
    MockResearchProvider,
    Operation,
)
from ai_youtube_agent.providers.research import ResearchErrorCode
from factories import make_channel, make_strategy_profile

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
SYSTEM = Actor(ActorKind.SYSTEM, "pipeline")
FETCH = CollectionOperation.FETCH
SEARCH = CollectionOperation.SEARCH


class Clock:
    """Moves one second per reading; ``jump`` moves it further."""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now

    def jump(self, delta: timedelta) -> None:
        self.now += delta


class Crash(Exception):
    """Stands for the worker process dying in the middle of a run."""


class CrashingProvider(MockResearchProvider):
    def __init__(self) -> None:
        super().__init__(clock=lambda: T0)
        self.crash_on: set[str] = set()
        self.on_fetch = None

    def fetch(self, url: str):
        if url in self.crash_on:
            self.crash_on.discard(url)
            raise Crash(url)
        if self.on_fetch is not None:
            self.on_fetch(url)
        return super().fetch(url)


def page(provider: MockResearchProvider, url: str) -> MockHit:
    provider.add_page(url, f"Text of {url}", title=f"Title {url}")
    return MockHit(url, f"Hit {url}")


class World:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.provider = CrashingProvider()
        self.sink = InMemoryAuditSink()
        self.clock = Clock()
        self.collector = self.make_collector()
        self.channel = make_channel()
        with database.transaction() as connection:
            ChannelRepository(connection).add(self.channel)
            StrategyProfileRepository(connection).add(
                make_strategy_profile(self.channel)
            )

    def make_collector(self, **kwargs) -> SourceCollector:
        return SourceCollector(
            self.database,
            self.provider,
            AuditLog(self.sink),
            clock=self.clock,
            sleep=lambda _: None,
            **kwargs,
        )

    def request(self, queries, limits=None) -> ResearchRequest:
        return self.collector.request(
            self.channel.id, queries, limits=limits, actor=SYSTEM
        )

    def stored(self, request_id: str) -> ResearchRequest:
        with self.database.transaction() as connection:
            return ResearchRequestRepository(connection).get(request_id)

    def urls(self, request: ResearchRequest) -> list[str]:
        with self.database.transaction() as connection:
            return [
                s.normalized_url
                for s in SourceRepository(connection).list_by_ids(
                    [c.source_id for c in request.collected]
                )
            ]

    def fetches(self) -> list[str]:
        return [url for op, url in self.provider.calls if op == "fetch"]

    def actions(self) -> list[str]:
        return [event.action for event in self.sink.events()]


def crashed(world: World, queries, crash_on: str, limits=None) -> ResearchRequest:
    request = world.request(queries, limits)
    world.provider.crash_on.add(crash_on)
    with pytest.raises(Crash):
        world.collector.collect(request.id)
    return world.stored(request.id)


# The entity


def test_a_running_request_holds_a_lease_that_progress_renews() -> None:
    clock = Clock()
    running = ResearchRequest.create("c1", ["a", "b"], actor=SYSTEM).start(clock=clock)
    assert running.lease_expires_at == running.started_at + LEASE
    assert not running.lease_expired(running.lease_expires_at - timedelta(seconds=1))
    assert running.lease_expired(running.lease_expires_at)

    saved = running.progress(
        [CollectedSource("s1", "a", 1)], [], queries_done=1, clock=clock
    )
    assert saved.queries_done == 1 and saved.collected[0].source_id == "s1"
    assert saved.lease_expires_at == saved.updated_at + LEASE > running.lease_expires_at

    done = saved.finish(saved.collected, [], clock=clock)
    assert done.lease_expires_at is None and done.queries_done == 2
    with pytest.raises(ResearchRequestStateError):
        done.progress([], [], queries_done=0)


def test_only_running_requests_have_a_lease() -> None:
    pending = ResearchRequest.create("c1", ["a"], actor=SYSTEM)
    running = pending.start()
    with pytest.raises(ValueError):
        ResearchRequest(**{**vars(pending), "lease_expires_at": T0})
    with pytest.raises(ValueError):
        ResearchRequest(**{**vars(running), "lease_expires_at": None})
    with pytest.raises(ValueError):
        running.progress([], [], queries_done=2)


def test_failures_name_their_hit_and_round() -> None:
    failure = CollectionFailure(FETCH, "https://a.org/", "e.x", 3, "q", 2, 1)
    assert CollectionFailure.from_dict(failure.as_dict()) == failure
    assert failure.is_retryable
    assert CollectionFailure(SEARCH, "q", "e.x", 1).is_retryable
    # Stored before #062: no query, rank or round; a fetch cannot be retried.
    legacy = CollectionFailure.from_dict(
        {"operation": "fetch", "target": "https://a.org/", "code": "e.x", "attempts": 1}
    )
    assert (legacy.query, legacy.rank, legacy.round) == (None, None, 0)
    assert not legacy.is_retryable
    for bad in (
        dict(query="q", rank=None),
        dict(query=None, rank=1),
        dict(query=" ", rank=1),
        dict(query="q", rank=51),
        dict(round=-1),
    ):
        with pytest.raises(ValueError):
            CollectionFailure(FETCH, "https://a.org/", "e.x", 1, **bad)
    with pytest.raises(ValueError):
        CollectionFailure(SEARCH, "q", "e.x", 1, "q", 1)


def test_reopen_turns_a_partial_or_failed_request_into_a_retry_run() -> None:
    failure = CollectionFailure(FETCH, "https://a.org/", "e.x", 3, "q", 1)
    running = ResearchRequest.create("c1", ["q"], actor=SYSTEM).start()
    partial = running.finish([CollectedSource("s1", "q", 2)], [failure])

    retry = partial.reopen()
    assert retry.status is ResearchStatus.RUNNING
    assert retry.finished_at is None and retry.lease_expires_at is not None
    assert retry.retries == 1 and retry.retry_targets == (failure,)
    assert retry.collected == partial.collected

    for request in (running, running.finish([], [])):
        with pytest.raises(ResearchRequestStateError):
            request.reopen()
    legacy = CollectionFailure(FETCH, "https://a.org/", "e.x", 1)
    with pytest.raises(ResearchRequestStateError):
        running.finish([], [legacy]).reopen()
    with pytest.raises(ValueError):
        running.progress(
            [], [CollectionFailure(SEARCH, "q", "e", 1, round=1)], queries_done=0
        )


# Saving progress


def test_progress_is_saved_after_each_item(database) -> None:
    world = World(database)
    p = world.provider
    p.add_results("a", [page(p, "https://a1.org/"), MockHit("https://gone.org/", "x")])
    p.add_results("b", [page(p, "https://b1.org/"), page(p, "https://b2.org/")])

    stored = crashed(world, ["a", "b"], crash_on="https://b2.org/")

    assert stored.status is ResearchStatus.RUNNING
    assert stored.queries_done == 1
    assert world.urls(stored) == ["https://a1.org/", "https://b1.org/"]
    assert [(c.query, c.rank) for c in stored.collected] == [("a", 1), ("b", 1)]
    assert stored.failures == (
        CollectionFailure(FETCH, "https://gone.org/", "research.not_found", 1, "a", 2),
    )
    assert stored.lease_expires_at == stored.updated_at + LEASE


def test_a_failed_search_is_saved_with_its_query(database) -> None:
    world = World(database)
    p = world.provider
    p.add_results("b", [page(p, "https://b1.org/")])
    p.fail_next(ResearchErrorCode.INVALID_QUERY, operation=Operation.SEARCH)

    stored = crashed(world, ["a", "b"], crash_on="https://b1.org/")

    assert stored.queries_done == 1
    assert stored.failures == (
        CollectionFailure(SEARCH, "a", "research.invalid_query", 1),
    )


# Resuming


def test_resume_continues_from_the_saved_progress(database) -> None:
    world = World(database)
    p = world.provider
    p.add_results("a", [page(p, "https://a1.org/")])
    p.add_results("b", [page(p, "https://b1.org/"), page(p, "https://b2.org/")])
    stored = crashed(world, ["a", "b"], crash_on="https://b2.org/")
    p.calls.clear()
    world.clock.jump(LEASE)

    finished = world.collector.resume(stored.id)

    assert finished.status is ResearchStatus.COMPLETED
    assert world.urls(finished) == [
        "https://a1.org/",
        "https://b1.org/",
        "https://b2.org/",
    ]
    # Query "a" was done; "b" is searched again but b1 is not fetched again.
    assert [c for c in p.calls if c[0] == "search"] == [("search", "b")]
    assert world.fetches() == ["https://b2.org/"]
    assert world.actions()[-2:] == ["research.resumed", "research.completed"]
    assert world.stored(stored.id) == finished


def test_resume_does_not_retry_fetches_that_already_failed(database) -> None:
    world = World(database)
    p = world.provider
    p.add_results("a", [MockHit("https://gone.org/", "x"), page(p, "https://a2.org/")])
    stored = crashed(world, ["a"], crash_on="https://a2.org/")
    p.calls.clear()
    world.clock.jump(LEASE)

    finished = world.collector.resume(stored.id)

    assert finished.status is ResearchStatus.PARTIAL
    assert world.fetches() == ["https://a2.org/"]
    assert len(finished.failures) == 1


def test_resume_refuses_a_live_lease_and_other_states(database) -> None:
    world = World(database)
    p = world.provider
    p.add_results("a", [page(p, "https://a1.org/")])
    stored = crashed(world, ["a"], crash_on="https://a1.org/")

    with pytest.raises(ResearchRequestBusyError) as caught:
        world.collector.resume(stored.id)
    public = caught.value.to_public()
    assert (public.code, public.http_status) == ("domain.research_request_busy", 409)

    pending = world.request(["x"])
    with pytest.raises(ResearchRequestStateError):
        world.collector.resume(pending.id)
    world.clock.jump(LEASE)
    finished = world.collector.resume(stored.id)
    with pytest.raises(ResearchRequestStateError):
        world.collector.resume(finished.id)
    with pytest.raises(ResearchRequestNotFoundError):
        world.collector.resume("missing")


def test_a_worker_that_lost_its_lease_stops_at_its_next_save(database) -> None:
    world = World(database)
    p = world.provider
    p.add_results("a", [page(p, "https://a1.org/"), page(p, "https://a2.org/")])
    request = world.request(["a"])
    # Another worker resumes while the first one is still fetching a2.
    other = world.make_collector(lease=timedelta(0))

    def take_over(url: str) -> None:
        if url == "https://a2.org/":
            p.on_fetch = None
            other.resume(request.id)

    p.on_fetch = take_over
    slow = world.make_collector(lease=timedelta(0))
    with pytest.raises(ConcurrencyError):
        slow.collect(request.id)

    finished = world.stored(request.id)
    assert finished.status is ResearchStatus.COMPLETED
    assert world.urls(finished) == ["https://a1.org/", "https://a2.org/"]


# Retrying failures


def test_retry_refetches_only_the_failed_pages(database) -> None:
    world = World(database)
    p = world.provider
    p.add_results("a", [page(p, "https://a1.org/"), page(p, "https://a2.org/")])
    p.fail_next(ResearchErrorCode.TIMEOUT, times=3, operation=Operation.FETCH)
    request = world.request(["a"])
    partial = world.collector.collect(request.id)
    assert partial.status is ResearchStatus.PARTIAL
    p.calls.clear()

    retried = world.collector.retry(request.id)

    assert retried.status is ResearchStatus.COMPLETED
    assert retried.failures == () and retried.retries == 1
    assert world.urls(retried) == ["https://a2.org/", "https://a1.org/"]
    assert [(c.query, c.rank) for c in retried.collected] == [("a", 2), ("a", 1)]
    assert p.calls == [("fetch", "https://a1.org/")]
    assert world.actions()[-2:] == ["research.retried", "research.completed"]
    assert world.stored(request.id) == retried


def test_retry_searches_a_failed_query_again(database) -> None:
    world = World(database)
    p = world.provider
    p.add_results("a", [page(p, "https://a1.org/")])
    p.add_results("b", [page(p, "https://b1.org/")])
    p.fail_next(ResearchErrorCode.UNAVAILABLE, times=3, operation=Operation.SEARCH)
    request = world.request(["a", "b"])
    assert world.collector.collect(request.id).status is ResearchStatus.PARTIAL

    retried = world.collector.retry(request.id)

    assert retried.status is ResearchStatus.COMPLETED
    assert world.urls(retried) == ["https://b1.org/", "https://a1.org/"]


def test_a_failure_that_happens_again_is_kept_for_the_next_retry(database) -> None:
    world = World(database)
    p = world.provider
    p.add_results("a", [page(p, "https://a1.org/"), page(p, "https://a2.org/")])
    p.fail_next(ResearchErrorCode.TIMEOUT, times=3, operation=Operation.FETCH)
    request = world.request(["a"])
    world.collector.collect(request.id)
    p.fail_next(ResearchErrorCode.TIMEOUT, times=3, operation=Operation.FETCH)

    again = world.collector.retry(request.id)

    assert again.status is ResearchStatus.PARTIAL
    assert again.failures == (
        CollectionFailure(FETCH, "https://a1.org/", "research.timeout", 3, "a", 1, 1),
    )
    third = world.collector.retry(request.id)
    assert third.status is ResearchStatus.COMPLETED and third.retries == 2


def test_retry_drops_a_page_whose_host_is_now_full(database) -> None:
    world = World(database)
    p = world.provider
    p.add_results("a", [page(p, "https://a.org/1"), page(p, "https://a.org/2")])
    p.fail_next(ResearchErrorCode.TIMEOUT, times=3, operation=Operation.FETCH)
    request = world.request(["a"], ResearchLimits(max_per_domain=1))
    world.collector.collect(request.id)
    p.calls.clear()

    retried = world.collector.retry(request.id)

    assert retried.status is ResearchStatus.COMPLETED
    assert world.urls(retried) == ["https://a.org/2"]
    assert p.calls == []


def test_a_crashed_retry_resumes_with_the_failures_left(database) -> None:
    world = World(database)
    p = world.provider
    hits = [page(p, f"https://a{n}.org/") for n in range(1, 4)]
    p.add_results("a", hits)
    p.fail_next(ResearchErrorCode.TIMEOUT, times=6, operation=Operation.FETCH)
    request = world.request(["a"])
    world.collector.collect(request.id)
    p.crash_on.add("https://a2.org/")
    with pytest.raises(Crash):
        world.collector.retry(request.id)
    p.calls.clear()
    world.clock.jump(LEASE)

    finished = world.collector.resume(request.id)

    assert finished.status is ResearchStatus.COMPLETED
    assert p.calls == [("fetch", "https://a2.org/")]
    assert world.urls(finished) == [
        "https://a3.org/",
        "https://a1.org/",
        "https://a2.org/",
    ]


def test_retry_is_refused_once_results_are_stored(database) -> None:
    world = World(database)
    p = world.provider
    p.add_results("a", [MockHit("https://gone.org/", "x")])
    request = world.request(["a"])
    world.collector.collect(request.id)
    with database.transaction() as connection:
        SourceDuplicateRepository(connection).add(request.id, [], T0)

    with pytest.raises(ResearchRequestHasResultsError) as caught:
        world.collector.retry(request.id)
    public = caught.value.to_public()
    assert (public.code, public.http_status) == (
        "domain.research_request_has_results",
        409,
    )
    assert world.stored(request.id).status is ResearchStatus.FAILED


def test_retry_refuses_completed_and_unknown_requests(database) -> None:
    world = World(database)
    request = world.request(["a"])
    world.collector.collect(request.id)

    with pytest.raises(ResearchRequestStateError):
        world.collector.retry(request.id)
    with pytest.raises(ResearchRequestNotFoundError):
        world.collector.retry("missing")


# Migration 0014


def test_migration_0014_gives_old_running_requests_an_expired_lease(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.db"
    migrate(path, migrations=default_migrations()[:13])
    database = Database(path)
    world = World(database)
    stamp = format_datetime(T0)
    with database.transaction() as connection:
        for request_id, status, failures in (
            ("old-running", "running", "[]"),
            (
                "old-failed",
                "failed",
                '[{"operation": "fetch", "target": "https://x.org/",'
                ' "code": "e.x", "attempts": 1}]',
            ),
        ):
            connection.execute(
                "INSERT INTO research_requests (id, channel_id, queries_json,"
                " max_sources, max_results_per_query, max_per_domain, status,"
                " requested_by_kind, requested_by_id, created_at, updated_at,"
                " started_at, finished_at, failures_json)"
                " VALUES (?, ?, ?, 20, 10, 3, ?, 'system', 'pipeline', ?, ?, ?, ?, ?)",
                (
                    request_id,
                    world.channel.id,
                    '["a", "b"]',
                    status,
                    stamp,
                    stamp,
                    stamp,
                    stamp if status == "failed" else None,
                    failures,
                ),
            )

    migrate(path)

    old_running = world.stored("old-running")
    assert old_running.lease_expires_at == T0 and old_running.queries_done == 0
    assert world.collector.resume(old_running.id).status is ResearchStatus.COMPLETED
    old_failed = world.stored("old-failed")
    assert old_failed.queries_done == 2 and old_failed.lease_expires_at is None
    assert not old_failed.failures[0].is_retryable
    with pytest.raises(sqlite3.IntegrityError), database.transaction() as connection:
        connection.execute(
            "UPDATE research_requests SET retries = -1 WHERE id = ?",
            (old_failed.id,),
        )
