"""E-063 Research Tests (Prompt Pack v8, prompt #063).

End-to-end scenarios for the research chain, as the user approved on
2026-10-03: each scenario makes a request and runs it through the whole chain
- collect (``SourceCollector`` through ``ResearchCache`` around the mock
provider), deduplicate, extract topics, score and build the report - and
checks the end result. The four groups of the prompt are covered: mocked
sources, duplicates, empty results and provider failures (retryable then
successful, permanent or exhausted, the cache's stale fallback, a crash
followed by resume and a retry).

The modules are tested one by one in the E-054..E-062 files; these tests only
check that they work together. Known gap, recorded and not fixed here (user
decision): the collector adds no evidence notes, so a real report has no
claims and its uncertainty is high for that reason.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.report_generator import ResearchReportGenerator
from ai_youtube_agent.content.research_report import ResearchReport, Uncertainty
from ai_youtube_agent.content.research_request import (
    LEASE,
    ResearchLimits,
    ResearchRequest,
    ResearchStatus,
)
from ai_youtube_agent.content.source_collector import (
    ResearchRequestHasResultsError,
    SourceCollector,
)
from ai_youtube_agent.content.source_dedup import SourceDeduplicator
from ai_youtube_agent.content.topic_extractor import TopicExtractor
from ai_youtube_agent.content.topic_scorer import TopicScorer
from ai_youtube_agent.core.audit import Actor, ActorKind, AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.research import SourceRepository
from ai_youtube_agent.providers.mock_research import (
    MockHit,
    MockResearchProvider,
    Operation,
)
from ai_youtube_agent.providers.research import ResearchErrorCode, ResearchProvider
from ai_youtube_agent.providers.research_cache import FETCH_TTL, ResearchCache
from factories import make_channel, make_strategy_profile

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
SYSTEM = Actor(ActorKind.SYSTEM, "pipeline")
NO_CLAIMS = "no claims: the sources have no evidence notes"


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now

    def jump(self, delta: timedelta) -> None:
        self.now += delta


class Crash(Exception):
    """Stands for the worker process dying in the middle of a run."""


class Provider(MockResearchProvider):
    """The mock, able to crash once on a URL."""

    def __init__(self, clock: Clock) -> None:
        super().__init__(clock=clock)
        self.crash_on: set[str] = set()

    def fetch(self, url: str):
        if url in self.crash_on:
            self.crash_on.discard(url)
            raise Crash(url)
        return super().fetch(url)

    def page(self, url: str, title: str, text: str | None = None, **kw) -> MockHit:
        self.add_page(
            url, text or f"{title}. Notes written for {url}.", title=title, **kw
        )
        return MockHit(url, title)

    def fetched(self) -> list[str]:
        return [url for operation, url in self.calls if operation == "fetch"]

    def searched(self) -> list[str]:
        return [text for operation, text in self.calls if operation == "search"]


class Flow:
    """The research chain wired like bootstrap, with a shared test clock."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.clock = Clock()
        self.provider = Provider(self.clock)
        self.cache = ResearchCache(self.provider, database, clock=self.clock)
        self.sink = InMemoryAuditSink()
        self.sleeps: list[float] = []
        self.collector = SourceCollector(
            database,
            self.cache,
            AuditLog(self.sink),
            clock=self.clock,
            sleep=self.sleeps.append,
        )
        self.deduplicator = SourceDeduplicator(database, clock=self.clock)
        extractor = TopicExtractor(database, self.deduplicator, clock=self.clock)
        self.scorer = TopicScorer(
            database, self.deduplicator, extractor, clock=self.clock
        )
        self.reports = ResearchReportGenerator(
            database, self.deduplicator, self.scorer, clock=self.clock
        )
        self.channel = make_channel()
        with database.transaction() as connection:
            ChannelRepository(connection).add(self.channel)
            StrategyProfileRepository(connection).add(
                make_strategy_profile(self.channel)
            )

    def request(self, queries, limits: ResearchLimits | None = None) -> ResearchRequest:
        return self.collector.request(
            self.channel.id, queries, limits=limits, actor=SYSTEM
        )

    def run(
        self, queries, limits: ResearchLimits | None = None
    ) -> tuple[ResearchRequest, ResearchReport]:
        request = self.collector.collect(self.request(queries, limits).id)
        return request, self.reports.generate(request.id)

    def urls(self, source_ids) -> list[str]:
        with self.database.transaction() as connection:
            return [
                s.normalized_url
                for s in SourceRepository(connection).list_by_ids(list(source_ids))
            ]

    def actions(self) -> list[str]:
        return [event.action for event in self.sink.events()]


@pytest.fixture
def flow(database: Database) -> Flow:
    return Flow(database)


def travel_pages(provider: Provider) -> list[MockHit]:
    return [
        provider.page("https://hanoi.org/", "Budget travel in Hanoi"),
        provider.page("https://danang.org/", "Budget travel around Da Nang"),
        provider.page("https://food.org/", "Budget travel street food guide"),
    ]


def report_urls(flow: Flow, report: ResearchReport) -> list[str]:
    return flow.urls(s.source_id for s in report.sources)


# Mocked sources


def test_mocked_sources_go_through_the_whole_chain(flow: Flow) -> None:
    flow.provider.add_results("budget travel", travel_pages(flow.provider))

    request, report = flow.run(["budget travel"])

    assert request.status is ResearchStatus.COMPLETED
    assert report.request_id == request.id
    assert report.request_status == "completed"
    assert (report.language, report.market) == (request.language, request.market)
    assert report_urls(flow, report) == [
        "https://hanoi.org/",
        "https://danang.org/",
        "https://food.org/",
    ]
    assert report.failures == ()
    labels = [topic.label for topic in report.topics]
    assert "budget travel" in labels
    topic = report.topics[labels.index("budget travel")]
    assert set(topic.source_ids) == {s.source_id for s in report.sources}
    assert 0.0 <= topic.score <= 1.0 and topic.reasons
    # Known gap: no evidence notes, so no claims and a high uncertainty.
    assert report.claims == ()
    assert report.uncertainty is Uncertainty.HIGH
    assert report.uncertainty_reasons == (NO_CLAIMS,)
    assert flow.actions() == ["research.requested", "research.completed"]
    assert "Budget travel in Hanoi" in report.to_markdown()


def test_a_second_request_is_served_from_the_cache(flow: Flow) -> None:
    flow.provider.add_results("budget travel", travel_pages(flow.provider))
    first, _ = flow.run(["budget travel"])
    calls = len(flow.provider.calls)

    second, report = flow.run(["Budget  Travel"])

    assert len(flow.provider.calls) == calls  # every search and fetch was a hit
    assert [c.source_id for c in second.collected] == [
        c.source_id for c in first.collected
    ]
    assert len(report.sources) == 3


def test_the_chain_works_through_the_bootstrap_container(
    tmp_path: Path, database_copy
) -> None:
    path = database_copy(tmp_path / "app.db")
    container = build_container(
        Settings(environment=Environment.TEST, database_path=path)
    )
    provider = container.resolve(ResearchProvider)
    assert isinstance(provider, ResearchCache)
    mock = provider.inner
    for url, title in (
        ("https://a.org/", "Saving money on groceries"),
        ("https://b.org/", "Saving money on rent"),
    ):
        mock.add_page(url, f"{title} text", title=title)
    mock.add_results(
        "saving money", [MockHit("https://a.org/", "a"), MockHit("https://b.org/", "b")]
    )
    channel = make_channel()
    with container.resolve(Database).transaction() as connection:
        ChannelRepository(connection).add(channel)
    collector = container.resolve(SourceCollector)

    request = collector.request(channel.id, ["saving money"], actor=SYSTEM)
    collector.collect(request.id)
    report = container.resolve(ResearchReportGenerator).generate(request.id)

    assert report.request_status == "completed"
    assert len(report.sources) == 2
    assert [t.label for t in report.topics] == ["saving money"]
    assert report.uncertainty is Uncertainty.HIGH


# Duplicates


def test_the_same_page_reached_three_ways_is_collected_once(flow: Flow) -> None:
    p = flow.provider
    p.add_results("budget travel", [p.page("https://a.org/x", "Budget travel")])
    p.add_results(
        "cheap trips",
        [
            MockHit("https://A.org/x?utm_source=feed", "same page, tracking tag"),
            p.page("https://short.link/r", "Redirect", final_url="https://a.org/x"),
            p.page("https://b.org/", "Cheap trips"),
        ],
    )

    request, report = flow.run(["budget travel", "cheap trips"])

    assert request.status is ResearchStatus.COMPLETED
    assert report_urls(flow, report) == ["https://a.org/x", "https://b.org/"]
    # The tracking-tagged URL is never fetched; the redirect is, then dropped.
    assert p.fetched() == ["https://a.org/x", "https://short.link/r", "https://b.org/"]


def test_near_duplicate_pages_are_left_out_of_the_report(flow: Flow) -> None:
    p = flow.provider
    text = (
        "Budget travel in Hanoi: eat street food, ride the bus and stay in a "
        "small guest house near the old quarter to keep costs low every day."
    )
    p.add_results(
        "budget travel",
        [
            p.page("https://hanoi.org/", "Budget travel in Hanoi", text),
            p.page("https://copy.net/", "Hanoi on a shoestring", text),
            p.page("https://danang.org/", "Budget travel around Da Nang"),
        ],
    )

    request, report = flow.run(["budget travel"])

    assert len(request.collected) == 3
    dedup = flow.deduplicator.deduplicate(request.id)
    assert flow.urls(d.source_id for d in dedup.duplicates) == ["https://copy.net/"]
    assert report_urls(flow, report) == ["https://hanoi.org/", "https://danang.org/"]
    kept = {s.source_id for s in report.sources}
    assert all(set(t.source_ids) <= kept for t in report.topics)


def test_near_duplicate_queries_are_warned_and_still_run(flow: Flow) -> None:
    flow.provider.add_results("budget travel tips", travel_pages(flow.provider))

    request = flow.request(["budget travel tips", "tips budget travel"])
    done = flow.collector.collect(request.id)

    assert len(request.query_warnings) == 1
    assert flow.provider.searched() == ["budget travel tips", "tips budget travel"]
    assert done.status is ResearchStatus.COMPLETED and len(done.collected) == 3


# Empty results


def test_a_search_with_no_hits_gives_an_empty_high_uncertainty_report(
    flow: Flow,
) -> None:
    request, report = flow.run(["nothing here"])

    assert request.status is ResearchStatus.COMPLETED and request.collected == ()
    assert (report.sources, report.topics, report.claims) == ((), (), ())
    assert report.uncertainty is Uncertainty.HIGH
    assert report.uncertainty_reasons == (
        "only 0 source(s) kept, fewer than 3",
        NO_CLAIMS,
    )
    assert flow.scorer.score(request.id).scores == ()
    assert report.to_markdown()


def test_sources_with_nothing_in_common_give_no_topics(flow: Flow) -> None:
    p = flow.provider
    p.add_results(
        "mixed",
        [
            p.page("https://a.org/", "Mountain hiking boots"),
            p.page("https://b.org/", "Sourdough bread starter"),
            p.page("https://c.org/", "Electric scooter batteries"),
        ],
    )

    request, report = flow.run(["mixed"])

    assert len(report.sources) == 3
    assert report.topics == ()
    assert report.uncertainty_reasons == (NO_CLAIMS,)


# Provider failures


def test_retryable_failures_that_pass_leave_no_trace(flow: Flow) -> None:
    flow.provider.add_results("budget travel", travel_pages(flow.provider))
    flow.provider.fail_next(ResearchErrorCode.TIMEOUT, operation=Operation.SEARCH)
    flow.provider.fail_next(
        ResearchErrorCode.RATE_LIMITED, times=2, operation=Operation.FETCH
    )

    request, report = flow.run(["budget travel"])

    assert request.status is ResearchStatus.COMPLETED
    assert flow.sleeps == [1.0, 1.0, 2.0]
    assert report.failures == () and len(report.sources) == 3
    assert report.uncertainty_reasons == (NO_CLAIMS,)


def test_permanent_and_exhausted_failures_reach_the_report(flow: Flow) -> None:
    p = flow.provider
    p.add_results(
        "budget travel", [*travel_pages(p), MockHit("https://gone.org/", "Gone")]
    )
    p.add_results("cheap trips", [p.page("https://b.org/", "Cheap trips")])
    p.fail_next(ResearchErrorCode.UNAVAILABLE, times=3, operation=Operation.SEARCH)

    request, report = flow.run(["cheap trips", "budget travel"])

    assert request.status is ResearchStatus.PARTIAL
    assert [(f.operation, f.target, f.code, f.attempts) for f in report.failures] == [
        ("search", "cheap trips", "research.unavailable", 3),
        ("fetch", "https://gone.org/", "research.not_found", 1),
    ]
    assert len(report.sources) == 3
    assert report.uncertainty is Uncertainty.HIGH
    assert report.uncertainty_reasons == ("2 search or fetch step(s) failed", NO_CLAIMS)
    assert flow.actions()[-1] == "research.partial"


def test_a_request_where_everything_fails_gives_a_failed_report(flow: Flow) -> None:
    flow.provider.add_results(
        "budget travel",
        [MockHit("https://gone.org/", "x"), MockHit("https://lost.org/", "y")],
    )

    request, report = flow.run(["budget travel"])

    assert request.status is ResearchStatus.FAILED
    assert report.request_status == "failed" and report.sources == ()
    assert report.uncertainty is Uncertainty.HIGH
    assert report.uncertainty_reasons[0] == "the research request failed"


def test_stale_cache_entries_cover_a_provider_outage(flow: Flow) -> None:
    p = flow.provider
    p.add_results("budget travel", travel_pages(p))
    first, _ = flow.run(["budget travel"])
    flow.clock.jump(FETCH_TTL + timedelta(hours=1))
    p.fail_next(ResearchErrorCode.UNAVAILABLE, times=10)

    request, report = flow.run(["budget travel", "never cached"])

    # Cached search and pages are served stale; the new query has no entry.
    assert [c.source_id for c in request.collected] == [
        c.source_id for c in first.collected
    ]
    assert request.status is ResearchStatus.PARTIAL
    assert [(f.target, f.code) for f in request.failures] == [
        ("never cached", "research.unavailable")
    ]
    assert len(report.sources) == 3
    assert flow.sleeps == [1.0, 2.0]  # only the uncached search was retried


def test_a_crashed_run_resumes_and_then_reports(flow: Flow) -> None:
    p = flow.provider
    p.add_results("budget travel", travel_pages(p))
    request = flow.request(["budget travel"])
    p.crash_on.add("https://food.org/")
    with pytest.raises(Crash):
        flow.collector.collect(request.id)
    flow.clock.jump(LEASE)

    resumed = flow.collector.resume(request.id)
    report = flow.reports.generate(request.id)

    assert resumed.status is ResearchStatus.COMPLETED
    assert p.fetched().count("https://hanoi.org/") == 1
    assert len(report.sources) == 3 and report.failures == ()
    assert "research.resumed" in flow.actions()


def test_a_partial_request_is_retried_then_reported_and_then_frozen(
    flow: Flow,
) -> None:
    p = flow.provider
    p.add_results("budget travel", travel_pages(p))
    p.fail_next(ResearchErrorCode.TIMEOUT, times=3, operation=Operation.FETCH)
    request = flow.request(["budget travel"])
    assert flow.collector.collect(request.id).status is ResearchStatus.PARTIAL

    retried = flow.collector.retry(request.id)
    report = flow.reports.generate(request.id)

    assert retried.status is ResearchStatus.COMPLETED
    assert report.request_status == "completed" and report.failures == ()
    assert len(report.sources) == 3
    assert "budget travel" in [t.label for t in report.topics]
    with pytest.raises(ResearchRequestHasResultsError):
        flow.collector.retry(request.id)
