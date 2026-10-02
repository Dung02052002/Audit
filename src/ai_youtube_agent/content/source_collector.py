"""Source Collector (Prompt Pack v8, prompt #056), context C4 Research.

``SourceCollector`` collects sources for a ``ResearchRequest``. The rules were
approved by the user on 2026-10-02:

- ``request`` stores a pending request for a channel with the caller's
  queries; language and market are copied from the channel strategy (None when
  not configured). Unknown channels are 404 and archived channels refused.
- ``collect`` runs a pending request sequentially: each query is searched
  (``max_results_per_query`` hits, the request's language and market), and
  the hits are fetched in rank order. A fetched page becomes a ``Source``
  (``Source.from_fetch``, stored with ``add_or_get``, so a known page is
  reused). Collection stops once ``max_sources`` sources are collected.
- A hit is skipped, without a fetch, when its normalised URL was already
  collected by this request or its host already has ``max_per_domain``
  sources; a page that redirects to an already collected URL is not counted.
- Each search and fetch is tried up to 3 times when the provider error is
  retryable, waiting 1 then 2 seconds (``sleep`` is injectable). A failure
  that is not retryable, or that lasts all attempts, is recorded as a
  ``CollectionFailure`` and collection goes on: a failed fetch is not a source,
  a failed search skips that query. Resuming an interrupted request is #062.
- The request is stored as running before collection and finished (completed,
  partial or failed) after it; ``research.<status>`` is audited after commit.
- No provider cost is recorded yet: the mock has none.
"""

import time
from collections.abc import Callable, Iterable
from datetime import datetime
from http import HTTPStatus
from typing import TypeVar
from urllib.parse import urlsplit

from ai_youtube_agent.content.channel import ChannelArchivedError
from ai_youtube_agent.content.channel_settings import ChannelNotFoundError
from ai_youtube_agent.content.research_request import (
    CollectedSource,
    CollectionFailure,
    CollectionOperation,
    ResearchLimits,
    ResearchRequest,
)
from ai_youtube_agent.content.source import Source, normalize_url
from ai_youtube_agent.core.audit import Actor, AuditLog, AuditResult, EntityRef
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.research import (
    ResearchRequestRepository,
    SourceRepository,
)
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.providers.research import (
    ResearchProvider,
    ResearchProviderError,
    SearchQuery,
)

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (1.0, 2.0)
Clock = Callable[[], datetime]
T = TypeVar("T")


class ResearchRequestNotFoundError(DomainError):
    default_code = "domain.research_request_not_found"
    default_user_message = "This research request does not exist."
    default_http_status = HTTPStatus.NOT_FOUND


class SourceCollector:
    def __init__(
        self,
        database: Database,
        provider: ResearchProvider,
        audit: AuditLog,
        *,
        clock: Clock | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._database = database
        self._provider = provider
        self._audit = audit
        self._clock = clock
        self._sleep = sleep

    def request(
        self,
        channel_id: str,
        queries: Iterable[str],
        *,
        limits: ResearchLimits | None = None,
        actor: Actor,
    ) -> ResearchRequest:
        with self._database.transaction() as connection:
            channel = ChannelRepository(connection).get(channel_id)
            if channel is None:
                raise ChannelNotFoundError(f"channel {channel_id} does not exist")
            if channel.is_archived:
                raise ChannelArchivedError(f"channel {channel_id} is archived")
            strategy = StrategyProfileRepository(connection).get_by_channel(channel_id)
            request = ResearchRequest.create(
                channel_id,
                queries,
                language=(
                    strategy.languages.primary
                    if strategy and strategy.languages
                    else None
                ),
                market=strategy.market.country
                if strategy and strategy.market
                else None,
                limits=limits,
                actor=actor,
                clock=self._clock,
            )
            ResearchRequestRepository(connection).add(request)
        self._audit.record(
            "research.requested",
            actor,
            EntityRef("research_request", request.id),
            AuditResult.SUCCESS,
            {"channel_id": channel_id, "queries": len(request.queries)},
        )
        return request

    def collect(self, request_id: str) -> ResearchRequest:
        with self._database.transaction() as connection:
            requests = ResearchRequestRepository(connection)
            pending = requests.get(request_id)
            if pending is None:
                raise ResearchRequestNotFoundError(
                    f"research request {request_id} does not exist"
                )
            running = pending.start(clock=self._clock)
            requests.update(running, expected_updated_at=pending.updated_at)

        collected, failures = self._run(running)

        with self._database.transaction() as connection:
            finished = running.finish(collected, failures, clock=self._clock)
            ResearchRequestRepository(connection).update(
                finished, expected_updated_at=running.updated_at
            )
        self._audit.record(
            f"research.{finished.status.value}",
            running.requested_by,
            EntityRef("research_request", finished.id),
            AuditResult.SUCCESS if not failures else AuditResult.FAILURE,
            {
                "channel_id": finished.channel_id,
                "sources": len(finished.collected),
                "failures": len(finished.failures),
            },
        )
        return finished

    def _run(
        self, request: ResearchRequest
    ) -> tuple[list[CollectedSource], list[CollectionFailure]]:
        limits = request.limits
        collected: list[CollectedSource] = []
        failures: list[CollectionFailure] = []
        tried: set[str] = set()  # normalised hit URLs already fetched or tried
        found: set[str] = set()  # normalised URLs of collected sources
        per_host: dict[str, int] = {}

        for text in request.queries:
            query = SearchQuery(
                text,
                language=request.language,
                market=request.market,
                max_results=limits.max_results_per_query,
            )
            results = self._attempt(
                lambda query=query: self._provider.search(query),
                CollectionOperation.SEARCH,
                text,
                failures,
            )
            if results is None:
                continue
            for hit in results.hits:
                if len(collected) >= limits.max_sources:
                    return collected, failures
                url = normalize_url(hit.url)
                if url in tried or url in found or self._full(per_host, url, limits):
                    continue
                tried.add(url)
                document = self._attempt(
                    lambda hit=hit: self._provider.fetch(hit.url),
                    CollectionOperation.FETCH,
                    hit.url,
                    failures,
                )
                if document is None:
                    continue
                source = Source.from_fetch(document, hit=hit)
                final = source.normalized_url
                # A redirect may land on a page or host already collected.
                if final in found or self._full(per_host, final, limits):
                    continue
                with self._database.transaction() as connection:
                    source = SourceRepository(connection).add_or_get(source)
                found.add(final)
                per_host[_host(final)] = per_host.get(_host(final), 0) + 1
                collected.append(CollectedSource(source.id, text, hit.rank))
        return collected, failures

    def _attempt(
        self,
        call: Callable[[], T],
        operation: CollectionOperation,
        target: str,
        failures: list[CollectionFailure],
    ) -> T | None:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return call()
            except ResearchProviderError as error:
                if not error.retryable or attempt == MAX_ATTEMPTS:
                    failures.append(
                        CollectionFailure(operation, target, error.code, attempt)
                    )
                    return None
                self._sleep(BACKOFF_SECONDS[attempt - 1])
        raise AssertionError("unreachable: the last attempt always returns")

    @staticmethod
    def _full(per_host: dict[str, int], url: str, limits: ResearchLimits) -> bool:
        return per_host.get(_host(url), 0) >= limits.max_per_domain


def _host(normalized_url: str) -> str:
    return urlsplit(normalized_url).netloc
