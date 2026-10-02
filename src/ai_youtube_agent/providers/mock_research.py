"""Mock Research Provider (Prompt Pack v8, prompt #054), context C4.

``MockResearchProvider`` is the deterministic, in-memory ``ResearchProvider``
used in tests and, until a real provider exists, by the application
(``Settings.research_provider = "mock"``). It never touches the network.

- ``add_results(text, hits)`` sets the hits for a search text (matched ignoring
  case and surrounding spaces); any other text finds nothing. A search returns
  at most ``max_results`` hits, ranked 1..n in the order given.
- ``add_page(url, text, ...)`` makes ``fetch(url)`` return that page; other
  URLs raise ``research.not_found``. Text longer than 200,000 characters is
  cut and marked ``truncated``.
- ``fail_next(code, times=1, operation=...)`` makes the next calls of
  ``search``, ``fetch`` or either raise ``ResearchProviderError`` with that
  code, so callers can test retries (#062).
- ``set_healthy(False)`` makes ``check`` fail with ``research.unavailable``.
- ``calls`` records every call as ``(operation, argument)``.

It is thread-safe, so a collector may call it from several threads.
"""

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from ai_youtube_agent.providers.research import (
    MAX_DOCUMENT_TEXT,
    FetchedDocument,
    ResearchErrorCode,
    ResearchProviderError,
    SearchHit,
    SearchQuery,
    SearchResults,
    check_url,
)

Clock = Callable[[], datetime]


class Operation(StrEnum):
    SEARCH = "search"
    FETCH = "fetch"
    ANY = "any"


@dataclass(frozen=True)
class MockHit:
    """A canned search hit; the mock assigns the rank."""

    url: str
    title: str
    snippet: str = ""
    published_at: datetime | None = None


@dataclass(frozen=True)
class _Page:
    title: str | None
    text: str
    media_type: str
    final_url: str


class MockResearchProvider:
    def __init__(self, *, name: str = "mock", clock: Clock | None = None) -> None:
        self.name = name
        self._clock = clock
        self._lock = threading.Lock()
        self._results: dict[str, tuple[MockHit, ...]] = {}
        self._pages: dict[str, _Page] = {}
        self._failures: list[tuple[Operation, ResearchErrorCode]] = []
        self._healthy = True
        self.calls: list[tuple[str, str]] = []

    # Setup

    def add_results(self, text: str, hits: Iterable[MockHit]) -> None:
        hits = tuple(hits)
        for hit in hits:
            check_url("hit url", hit.url)
        with self._lock:
            self._results[_key(text)] = hits

    def add_page(
        self,
        url: str,
        text: str,
        *,
        title: str | None = None,
        media_type: str = "text/html",
        final_url: str | None = None,
    ) -> None:
        check_url("url", url)
        with self._lock:
            self._pages[url] = _Page(title, text, media_type, final_url or url)

    def fail_next(
        self,
        code: ResearchErrorCode,
        *,
        times: int = 1,
        operation: Operation = Operation.ANY,
    ) -> None:
        if times < 1:
            raise ValueError("times must be 1 or more")
        with self._lock:
            self._failures.extend([(operation, code)] * times)

    def set_healthy(self, healthy: bool) -> None:
        with self._lock:
            self._healthy = healthy

    # ResearchProvider

    def search(self, query: SearchQuery) -> SearchResults:
        with self._lock:
            self.calls.append((Operation.SEARCH.value, query.text))
            self._raise_if_failing(Operation.SEARCH)
            canned = self._results.get(_key(query.text), ())
        hits = tuple(
            SearchHit(hit.url, hit.title, rank, hit.snippet, hit.published_at)
            for rank, hit in enumerate(_unique(canned)[: query.max_results], start=1)
        )
        return SearchResults(query, hits, self.name, self._now())

    def fetch(self, url: str) -> FetchedDocument:
        with self._lock:
            self.calls.append((Operation.FETCH.value, url))
            self._raise_if_failing(Operation.FETCH)
            page = self._pages.get(url)
        if page is None:
            raise ResearchProviderError(
                ResearchErrorCode.NOT_FOUND, f"no page for {url}", provider=self.name
            )
        truncated = len(page.text) > MAX_DOCUMENT_TEXT
        return FetchedDocument(
            url=url,
            final_url=page.final_url,
            title=page.title,
            text=page.text[:MAX_DOCUMENT_TEXT],
            media_type=page.media_type,
            fetched_at=self._now(),
            provider=self.name,
            truncated=truncated,
        )

    def check(self) -> None:
        with self._lock:
            healthy = self._healthy
        if not healthy:
            raise ResearchProviderError(
                ResearchErrorCode.UNAVAILABLE, "mock set unhealthy", provider=self.name
            )

    # Helpers

    def _raise_if_failing(self, operation: Operation) -> None:
        # Called with the lock held.
        for index, (wanted, code) in enumerate(self._failures):
            if wanted in (operation, Operation.ANY):
                del self._failures[index]
                raise ResearchProviderError(
                    code, f"scripted {operation.value} failure", provider=self.name
                )

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)


def _key(text: str) -> str:
    return text.strip().casefold()


def _unique(hits: tuple[MockHit, ...]) -> list[MockHit]:
    seen: set[str] = set()
    unique = []
    for hit in hits:
        if hit.url not in seen:
            seen.add(hit.url)
            unique.append(hit)
    return unique
