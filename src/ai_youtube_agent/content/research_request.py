"""Research request entity (Prompt Pack v8, prompt #056), context C4 Research.

A ``ResearchRequest`` asks the source collector to find sources for a
channel. The rules were approved by the user on 2026-10-02:

- ``queries`` are 1 to 10 search texts given by the user or the caller (each
  at most 500 characters, no two equal ignoring case). Nothing builds queries
  from the strategy: choosing topics stays with the user (R-09).
- ``language`` and ``market`` are copied from the channel strategy when the
  request is made, or None when not configured.
- ``limits`` (``ResearchLimits``): ``max_sources`` 1 to 100 (default 20),
  ``max_results_per_query`` 1 to 50 (default 10) and ``max_per_domain`` 1 to 20
  (default 3).
- ``status``: pending -> running -> completed (no failures), partial (some
  failures and at least one source) or failed (failures and no source). A
  request that finds nothing without failing is completed with no sources.
- ``collected`` lists the sources found, in collection order, with the query
  and rank that found each; ``failures`` lists what failed (search or fetch,
  target, error code, attempts).

``query_warnings`` (#057) lists pairs of queries that are near-duplicates
(word Jaccard from 0.8); they are allowed, only reported.

Failure recovery (#062), approved by the user on 2026-10-03:

- A running request is saved after each collected source or failure
  (``progress``); ``queries_done`` counts the queries fully handled and
  ``lease_expires_at`` is renewed on every save (``LEASE``, 10 minutes). Only
  a running request has a lease; once it has expired the request may be
  resumed from what was saved.
- ``reopen`` turns a partial or failed request back into a running one for a
  retry of its failures; ``retries`` counts these runs. A failure records the
  ``round`` (the value of ``retries``) it happened in, and a fetch failure the
  ``query`` and ``rank`` of its hit, so that a retry can collect the page.

Any actor may make a request; ``requested_by`` records who. A request is
frozen: ``start`` and ``finish`` return new values.
"""

import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from ai_youtube_agent.content.similarity import SHORT_TEXT_THRESHOLD, word_jaccard
from ai_youtube_agent.core.audit import Actor
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.providers.research import MAX_QUERY_LENGTH

MAX_QUERIES = 10
LEASE = timedelta(minutes=10)
Clock = Callable[[], datetime]


class ResearchStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


FINAL_STATUSES = frozenset(
    {ResearchStatus.COMPLETED, ResearchStatus.PARTIAL, ResearchStatus.FAILED}
)


class ResearchRequestStateError(DomainError):
    default_code = "domain.research_request_state"
    default_user_message = "This research request cannot do that in its state."


class CollectionOperation(StrEnum):
    SEARCH = "search"
    FETCH = "fetch"


def _whole(name: str, value: object, low: int, high: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise ValueError(f"{name} must be a whole number from {low} to {high}")


def _require_utc(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")


@dataclass(frozen=True)
class ResearchLimits:
    max_sources: int = 20
    max_results_per_query: int = 10
    max_per_domain: int = 3

    def __post_init__(self) -> None:
        _whole("max_sources", self.max_sources, 1, 100)
        _whole("max_results_per_query", self.max_results_per_query, 1, 50)
        _whole("max_per_domain", self.max_per_domain, 1, 20)


@dataclass(frozen=True)
class CollectedSource:
    """A source found by a request: which query found it, at which rank."""

    source_id: str
    query: str
    rank: int

    def __post_init__(self) -> None:
        if not self.source_id:
            raise ValueError("source id must not be empty")
        if not self.query.strip():
            raise ValueError("query must not be empty")
        _whole("rank", self.rank, 1, 50)


@dataclass(frozen=True)
class CollectionFailure:
    """A search or fetch that failed after its attempts.

    A fetch failure names the ``query`` and ``rank`` of its hit (None for
    failures stored before #062, which cannot be retried); ``round`` is the
    run it happened in: 0 for the first run, then the retry number.
    """

    operation: CollectionOperation
    target: str
    code: str
    attempts: int
    query: str | None = None
    rank: int | None = None
    round: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.operation, CollectionOperation):
            raise TypeError("operation must be a CollectionOperation")
        if not self.target:
            raise ValueError("target must not be empty")
        if not self.code:
            raise ValueError("code must not be empty")
        _whole("attempts", self.attempts, 1, 10)
        if (self.query is None) != (self.rank is None):
            raise ValueError("a failure has both a query and a rank, or neither")
        if self.query is not None:
            if self.operation is not CollectionOperation.FETCH:
                raise ValueError("only a fetch failure names its query and rank")
            if not self.query.strip():
                raise ValueError("query must not be empty")
            _whole("rank", self.rank, 1, 50)
        _whole("round", self.round, 0, 1_000_000)

    @property
    def is_retryable(self) -> bool:
        """A search can always be redone; a fetch needs its hit's query and rank."""
        return self.operation is CollectionOperation.SEARCH or self.query is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation.value,
            "target": self.target,
            "code": self.code,
            "attempts": self.attempts,
            "query": self.query,
            "rank": self.rank,
            "round": self.round,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CollectionFailure":
        return cls(
            CollectionOperation(data["operation"]),
            data["target"],
            data["code"],
            data["attempts"],
            data.get("query"),
            data.get("rank"),
            data.get("round", 0),
        )


@dataclass(frozen=True)
class QueryWarning:
    """Two queries of one request that look almost the same (#057)."""

    first: str
    second: str
    similarity: float


@dataclass(frozen=True)
class ResearchRequest:
    id: str
    channel_id: str
    queries: tuple[str, ...]
    language: str | None
    market: str | None
    limits: ResearchLimits
    status: ResearchStatus
    requested_by: Actor
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    collected: tuple[CollectedSource, ...] = ()
    failures: tuple[CollectionFailure, ...] = ()
    queries_done: int = 0
    lease_expires_at: datetime | None = None
    retries: int = 0

    def __post_init__(self) -> None:
        if not self.id or not self.channel_id:
            raise ValueError("request id and channel id must not be empty")
        if not isinstance(self.queries, tuple) or not 1 <= len(self.queries) <= (
            MAX_QUERIES
        ):
            raise ValueError(f"a request needs 1 to {MAX_QUERIES} queries")
        for query in self.queries:
            if not isinstance(query, str) or not query.strip():
                raise ValueError("a query must not be empty")
            if len(query) > MAX_QUERY_LENGTH:
                raise ValueError(
                    f"a query must be at most {MAX_QUERY_LENGTH} characters"
                )
        if len({q.strip().casefold() for q in self.queries}) != len(self.queries):
            raise ValueError("queries must not repeat")
        if not isinstance(self.limits, ResearchLimits):
            raise TypeError("limits must be ResearchLimits")
        if not isinstance(self.status, ResearchStatus):
            raise TypeError("status must be a ResearchStatus")
        for name in ("created_at", "updated_at"):
            _require_utc(name, getattr(self, name))
        for name in ("started_at", "finished_at"):
            if getattr(self, name) is not None:
                _require_utc(name, getattr(self, name))
        if (self.started_at is None) != (self.status is ResearchStatus.PENDING):
            raise ValueError("only a pending request has no start time")
        if (self.finished_at is None) == (self.status in FINAL_STATUSES):
            raise ValueError("exactly the finished requests have a finish time")
        if (self.lease_expires_at is None) == (self.status is ResearchStatus.RUNNING):
            raise ValueError("exactly the running requests have a lease")
        if self.lease_expires_at is not None:
            _require_utc("lease_expires_at", self.lease_expires_at)
        _whole("queries_done", self.queries_done, 0, len(self.queries))
        _whole("retries", self.retries, 0, 1_000_000)
        if any(f.round > self.retries for f in self.failures):
            raise ValueError("a failure cannot come from a later run")
        if len(self.collected) > self.limits.max_sources:
            raise ValueError("a request must not collect more than max_sources")
        if len({c.source_id for c in self.collected}) != len(self.collected):
            raise ValueError("a source is collected once per request")

    @classmethod
    def create(
        cls,
        channel_id: str,
        queries: Iterable[str],
        *,
        language: str | None = None,
        market: str | None = None,
        limits: ResearchLimits | None = None,
        actor: Actor,
        clock: Clock | None = None,
    ) -> "ResearchRequest":
        now = _now(clock)
        return cls(
            id=uuid.uuid4().hex,
            channel_id=channel_id,
            queries=tuple(query.strip() for query in queries),
            language=language,
            market=market,
            limits=limits or ResearchLimits(),
            status=ResearchStatus.PENDING,
            requested_by=actor,
            created_at=now,
            updated_at=now,
        )

    @property
    def query_warnings(self) -> tuple[QueryWarning, ...]:
        return tuple(
            QueryWarning(first, second, round(similarity, 4))
            for i, first in enumerate(self.queries)
            for second in self.queries[i + 1 :]
            if (similarity := word_jaccard(first, second)) >= SHORT_TEXT_THRESHOLD
        )

    @property
    def is_finished(self) -> bool:
        return self.status in FINAL_STATUSES

    def lease_expired(self, now: datetime) -> bool:
        """Whether a running request's worker has stopped renewing its lease."""
        return self.lease_expires_at is not None and self.lease_expires_at <= now

    @property
    def retry_targets(self) -> tuple[CollectionFailure, ...]:
        """The failures the current retry run still has to redo."""
        return tuple(
            f for f in self.failures if f.round < self.retries and f.is_retryable
        )

    def start(
        self, *, clock: Clock | None = None, lease: timedelta = LEASE
    ) -> "ResearchRequest":
        if self.status is not ResearchStatus.PENDING:
            raise ResearchRequestStateError(
                f"research request {self.id} is {self.status.value}, not pending"
            )
        now = _now(clock)
        return replace(
            self,
            status=ResearchStatus.RUNNING,
            started_at=now,
            updated_at=now,
            lease_expires_at=now + lease,
        )

    def progress(
        self,
        collected: Iterable[CollectedSource],
        failures: Iterable[CollectionFailure],
        *,
        queries_done: int,
        clock: Clock | None = None,
        lease: timedelta = LEASE,
    ) -> "ResearchRequest":
        """Save how far a running request got, and renew its lease."""
        self._require_running()
        now = _now(clock)
        return replace(
            self,
            collected=tuple(collected),
            failures=tuple(failures),
            queries_done=queries_done,
            updated_at=now,
            lease_expires_at=now + lease,
        )

    def reopen(
        self, *, clock: Clock | None = None, lease: timedelta = LEASE
    ) -> "ResearchRequest":
        """Run a partial or failed request again to retry its failures."""
        if self.status not in (ResearchStatus.PARTIAL, ResearchStatus.FAILED):
            raise ResearchRequestStateError(
                f"research request {self.id} is {self.status.value}, "
                "not partial or failed"
            )
        if not any(f.is_retryable for f in self.failures):
            raise ResearchRequestStateError(
                f"research request {self.id} has no failure that can be retried"
            )
        now = _now(clock)
        return replace(
            self,
            status=ResearchStatus.RUNNING,
            finished_at=None,
            updated_at=now,
            lease_expires_at=now + lease,
            retries=self.retries + 1,
        )

    def finish(
        self,
        collected: Iterable[CollectedSource],
        failures: Iterable[CollectionFailure],
        *,
        clock: Clock | None = None,
    ) -> "ResearchRequest":
        self._require_running()
        collected, failures = tuple(collected), tuple(failures)
        if not failures:
            status = ResearchStatus.COMPLETED
        elif collected:
            status = ResearchStatus.PARTIAL
        else:
            status = ResearchStatus.FAILED
        now = _now(clock)
        return replace(
            self,
            status=status,
            collected=collected,
            failures=failures,
            finished_at=now,
            updated_at=now,
            lease_expires_at=None,
            queries_done=len(self.queries),
        )

    def _require_running(self) -> None:
        if self.status is not ResearchStatus.RUNNING:
            raise ResearchRequestStateError(
                f"research request {self.id} is {self.status.value}, not running"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "channel_id": self.channel_id,
            "queries": list(self.queries),
            "language": self.language,
            "market": self.market,
            "limits": {
                "max_sources": self.limits.max_sources,
                "max_results_per_query": self.limits.max_results_per_query,
                "max_per_domain": self.limits.max_per_domain,
            },
            "status": self.status.value,
            "requested_by": {
                "kind": self.requested_by.kind.value,
                "id": self.requested_by.id,
            },
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "collected": [
                {"source_id": c.source_id, "query": c.query, "rank": c.rank}
                for c in self.collected
            ],
            "failures": [failure.as_dict() for failure in self.failures],
            "queries_done": self.queries_done,
            "lease_expires_at": (
                self.lease_expires_at.isoformat() if self.lease_expires_at else None
            ),
            "retries": self.retries,
        }


def _now(clock: Clock | None) -> datetime:
    return clock() if clock else datetime.now(UTC)
