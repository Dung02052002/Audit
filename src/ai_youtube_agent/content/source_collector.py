"""Source Collector (Prompt Pack v8, prompts #056 and #062), context C4 Research.

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
  collected or tried by this request or its host already has
  ``max_per_domain`` sources; a page that redirects to an already collected
  URL is not counted.
- Each search and fetch is tried up to 3 times when the provider error is
  retryable, waiting 1 then 2 seconds (``sleep`` is injectable). A failure
  that is not retryable, or that lasts all attempts, is recorded as a
  ``CollectionFailure`` and collection goes on: a failed fetch is not a source,
  a failed search skips that query.
- The request is finished (completed, partial or failed) at the end;
  ``research.<status>`` is audited after commit. No provider cost is recorded
  yet: the mock has none.

Failure recovery (#062), approved by the user on 2026-10-03:

- Progress is saved after each item: a collected source is stored together
  with the request's progress in one transaction, a failed fetch is saved at
  once, and a query is saved as done once its search and hits are handled.
  Every save renews the request's lease (``LEASE``, 10 minutes, injectable).
  A save that finds the request changed by someone else raises
  ``ConcurrencyError`` and this run stops.
- ``resume`` continues a running request whose lease has expired (its worker
  stopped): it takes the lease over and goes on from the saved progress (the
  first unfinished query, or the failures a retry still has to redo); pages
  already collected or tried are not fetched again. A running request with a
  lease that has not expired is refused (``ResearchRequestBusyError``).
  ``research.resumed`` is audited.
- ``retry`` runs a partial or failed request again for its failures only:
  a failed search is searched again and its hits handled, a failed fetch is
  fetched again (its query and rank are kept) unless the page or its host is
  no longer wanted; sources already collected stay. A failure that happens
  again is recorded with the new round. Retry is refused once deduplication,
  topics, scores or a report are stored for the request
  (``ResearchRequestHasResultsError``), since those are stored once.
  ``research.retried`` is audited. A page collected by a retry has no
  publication time, which only a search hit gives.
"""

import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import TypeVar
from urllib.parse import urlsplit

from ai_youtube_agent.content.channel import ChannelArchivedError
from ai_youtube_agent.content.channel_settings import ChannelNotFoundError
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
    SearchHit,
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


class ResearchRequestBusyError(DomainError):
    default_code = "domain.research_request_busy"
    default_user_message = "This research request is still running."
    default_http_status = HTTPStatus.CONFLICT


class ResearchRequestHasResultsError(DomainError):
    default_code = "domain.research_request_has_results"
    default_user_message = (
        "This research request already has results; make a new request instead."
    )
    default_http_status = HTTPStatus.CONFLICT


class SourceCollector:
    def __init__(
        self,
        database: Database,
        provider: ResearchProvider,
        audit: AuditLog,
        *,
        clock: Clock | None = None,
        sleep: Callable[[float], None] = time.sleep,
        lease: timedelta = LEASE,
    ) -> None:
        self._database = database
        self._provider = provider
        self._audit = audit
        self._clock = clock
        self._sleep = sleep
        self._lease = lease

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
            {
                "channel_id": channel_id,
                "queries": len(request.queries),
                "similar_queries": len(request.query_warnings),
            },
        )
        return request

    def collect(self, request_id: str) -> ResearchRequest:
        """Run a pending request."""
        with self._database.transaction() as connection:
            requests = ResearchRequestRepository(connection)
            pending = self._get(requests, request_id)
            running = pending.start(clock=self._clock, lease=self._lease)
            requests.update(running, expected_updated_at=pending.updated_at)
        return self._run(running)

    def resume(self, request_id: str) -> ResearchRequest:
        """Continue a running request whose worker stopped renewing its lease."""
        with self._database.transaction() as connection:
            requests = ResearchRequestRepository(connection)
            stopped = self._get(requests, request_id)
            if stopped.status is not ResearchStatus.RUNNING:
                raise ResearchRequestStateError(
                    f"research request {request_id} is {stopped.status.value}, "
                    "not running"
                )
            if not stopped.lease_expired(self._now()):
                raise ResearchRequestBusyError(
                    f"research request {request_id} is running until "
                    f"{stopped.lease_expires_at}"
                )
            running = stopped.progress(
                stopped.collected,
                stopped.failures,
                queries_done=stopped.queries_done,
                clock=self._clock,
                lease=self._lease,
            )
            requests.update(running, expected_updated_at=stopped.updated_at)
        self._audit.record(
            "research.resumed",
            running.requested_by,
            EntityRef("research_request", running.id),
            AuditResult.SUCCESS,
            {
                "channel_id": running.channel_id,
                "sources": len(running.collected),
                "failures": len(running.failures),
                "queries_done": running.queries_done,
                "retries": running.retries,
            },
        )
        return self._run(running)

    def retry(self, request_id: str) -> ResearchRequest:
        """Run a partial or failed request again for its failures only."""
        with self._database.transaction() as connection:
            requests = ResearchRequestRepository(connection)
            finished = self._get(requests, request_id)
            if requests.has_results(request_id):
                raise ResearchRequestHasResultsError(
                    f"research request {request_id} already has stored results"
                )
            running = finished.reopen(clock=self._clock, lease=self._lease)
            requests.update(running, expected_updated_at=finished.updated_at)
        self._audit.record(
            "research.retried",
            running.requested_by,
            EntityRef("research_request", running.id),
            AuditResult.SUCCESS,
            {
                "channel_id": running.channel_id,
                "failures": len(running.retry_targets),
                "retry": running.retries,
            },
        )
        return self._run(running)

    def _run(self, running: ResearchRequest) -> ResearchRequest:
        run = _Run(self, running)
        if running.retries:
            run.retry_failures()
        else:
            run.queries()
        finished = run.finish()
        self._audit.record(
            f"research.{finished.status.value}",
            running.requested_by,
            EntityRef("research_request", finished.id),
            AuditResult.SUCCESS if not finished.failures else AuditResult.FAILURE,
            {
                "channel_id": finished.channel_id,
                "sources": len(finished.collected),
                "failures": len(finished.failures),
            },
        )
        return finished

    def _attempt(
        self,
        call: Callable[[], T],
        operation: CollectionOperation,
        target: str,
        **details,
    ) -> T | CollectionFailure:
        """The call's result, or the failure once it stops being worth retrying."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return call()
            except ResearchProviderError as error:
                if not error.retryable or attempt == MAX_ATTEMPTS:
                    return CollectionFailure(
                        operation, target, error.code, attempt, **details
                    )
                self._sleep(BACKOFF_SECONDS[attempt - 1])
        raise AssertionError("unreachable: the last attempt always returns")

    @staticmethod
    def _get(requests: ResearchRequestRepository, request_id: str) -> ResearchRequest:
        request = requests.get(request_id)
        if request is None:
            raise ResearchRequestNotFoundError(
                f"research request {request_id} does not exist"
            )
        return request

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)


class _Run:
    """One run of a request, saving its progress after each item."""

    def __init__(self, collector: SourceCollector, request: ResearchRequest) -> None:
        self._collector = collector
        self._request = request  # as last saved
        self._collected = list(request.collected)
        self._failures = list(request.failures)
        self._queries_done = request.queries_done
        with collector._database.transaction() as connection:
            sources = SourceRepository(connection).list_by_ids(
                [c.source_id for c in request.collected]
            )
        self._found = {source.normalized_url for source in sources}
        self._per_host: dict[str, int] = {}
        for url in self._found:
            self._per_host[_host(url)] = self._per_host.get(_host(url), 0) + 1
        # Normalised hit URLs already fetched or tried.
        self._tried = {
            normalize_url(f.target)
            for f in request.failures
            if f.operation is CollectionOperation.FETCH
        }

    def queries(self) -> None:
        queries = self._request.queries
        for index in range(self._queries_done, len(queries)):
            if self._full():
                break
            self._search(queries[index])
            self._queries_done = index + 1
            self._save()

    def retry_failures(self) -> None:
        for failure in self._request.retry_targets:
            if self._full():
                break
            if failure.operation is CollectionOperation.SEARCH:
                self._search(failure.target)
                self._failures.remove(failure)
                self._save()
                continue
            url = normalize_url(failure.target)
            if url in self._found or self._host_full(url):
                self._failures.remove(failure)  # no longer wanted
                self._save()
                continue
            self._fetch(failure.target, failure.query, failure.rank, replacing=failure)

    def finish(self) -> ResearchRequest:
        collector = self._collector
        with collector._database.transaction() as connection:
            finished = self._request.finish(
                self._collected, self._failures, clock=collector._clock
            )
            ResearchRequestRepository(connection).update(
                finished, expected_updated_at=self._request.updated_at
            )
        return finished

    def _search(self, text: str) -> None:
        """Search ``text`` and fetch its hits; a failed search is kept unsaved."""
        request = self._request
        query = SearchQuery(
            text,
            language=request.language,
            market=request.market,
            max_results=request.limits.max_results_per_query,
        )
        results = self._collector._attempt(
            lambda: self._collector._provider.search(query),
            CollectionOperation.SEARCH,
            text,
            round=request.retries,
        )
        if isinstance(results, CollectionFailure):
            self._failures.append(results)
            return
        for hit in results.hits:
            if self._full():
                return
            url = normalize_url(hit.url)
            if url in self._tried or url in self._found or self._host_full(url):
                continue
            self._fetch(hit.url, text, hit.rank, hit=hit)

    def _fetch(
        self,
        url: str,
        query: str,
        rank: int,
        *,
        hit: SearchHit | None = None,
        replacing: CollectionFailure | None = None,
    ) -> None:
        self._tried.add(normalize_url(url))
        document = self._collector._attempt(
            lambda: self._collector._provider.fetch(url),
            CollectionOperation.FETCH,
            url,
            query=query,
            rank=rank,
            round=self._request.retries,
        )
        if replacing is not None:
            self._failures.remove(replacing)
        if isinstance(document, CollectionFailure):
            self._failures.append(document)
            self._save()
            return
        source = Source.from_fetch(document, hit=hit)
        final = source.normalized_url
        # A redirect may land on a page or host already collected.
        if final in self._found or self._host_full(final):
            if replacing is not None:
                self._save()
            return
        with self._collector._database.transaction() as connection:
            source = SourceRepository(connection).add_or_get(source)
            self._collected.append(CollectedSource(source.id, query, rank))
            self._save(connection)
        self._found.add(final)
        self._per_host[_host(final)] = self._per_host.get(_host(final), 0) + 1

    def _save(self, connection=None) -> None:
        if connection is None:
            with self._collector._database.transaction() as connection:
                self._save(connection)
            return
        collector = self._collector
        saved = self._request.progress(
            self._collected,
            self._failures,
            queries_done=self._queries_done,
            clock=collector._clock,
            lease=collector._lease,
        )
        ResearchRequestRepository(connection).update(
            saved, expected_updated_at=self._request.updated_at
        )
        self._request = saved

    def _full(self) -> bool:
        return len(self._collected) >= self._request.limits.max_sources

    def _host_full(self, url: str) -> bool:
        return self._per_host.get(_host(url), 0) >= self._request.limits.max_per_domain


def _host(normalized_url: str) -> str:
    return urlsplit(normalized_url).netloc
