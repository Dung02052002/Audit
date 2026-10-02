"""Research Cache (Prompt Pack v8, prompt #061), context C4 infrastructure.

``ResearchCache`` wraps any ``ResearchProvider`` and is one itself, so callers
do not change. The rules were approved by the user on 2026-10-03:

- Searches and fetches are cached; research reports are not.
- A search is keyed by its text (trimmed, case-folded, spaces collapsed),
  language, market and ``max_results``; a fetch by the normalised URL
  (``normalize_url``), so tracking parameters and fragments share an entry.
- Searches stay fresh for 6 hours and fetches for 24 hours.
- A fresh entry is returned without calling the provider (``hit``). Without an
  entry the provider is called and its answer stored (``miss``). A stale entry
  is not deleted: the provider is called and the entry replaced
  (``refresh``). When that call fails with a retryable error, the stale value
  is returned instead, marked ``stale_fallback`` (``CacheInfo.stale``). Other
  errors, and any error without a cached value, are raised.
- Only successful answers are cached ("safe"): errors are never stored. The
  values themselves were already validated (http(s) URLs, size limits).
- Entries live in SQLite (migration 0013). The cache never touches the
  strategy.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, TypeVar

from ai_youtube_agent.content.source import normalize_url
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.research import ResearchCacheRepository
from ai_youtube_agent.providers.research import (
    CacheInfo,
    CacheOutcome,
    FetchedDocument,
    ResearchProvider,
    ResearchProviderError,
    SearchHit,
    SearchQuery,
    SearchResults,
)

SEARCH_TTL = timedelta(hours=6)
FETCH_TTL = timedelta(hours=24)
Clock = Callable[[], datetime]
T = TypeVar("T")


class CacheKind(StrEnum):
    SEARCH = "search"
    FETCH = "fetch"


def search_key(query: SearchQuery) -> str:
    text = " ".join(query.text.casefold().split())
    return "|".join(
        (text, query.language or "", query.market or "", str(query.max_results))
    )


def fetch_key(url: str) -> str:
    return normalize_url(url)


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def _parse(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def results_payload(results: SearchResults) -> dict[str, Any]:
    return {
        "provider": results.provider,
        "searched_at": _iso(results.searched_at),
        "hits": [
            {
                "url": hit.url,
                "title": hit.title,
                "rank": hit.rank,
                "snippet": hit.snippet,
                "published_at": _iso(hit.published_at),
            }
            for hit in results.hits
        ],
    }


def results_from(
    query: SearchQuery, payload: dict[str, Any], cache: CacheInfo
) -> SearchResults:
    return SearchResults(
        query=query,
        hits=tuple(
            SearchHit(
                h["url"], h["title"], h["rank"], h["snippet"], _parse(h["published_at"])
            )
            for h in payload["hits"]
        ),
        provider=payload["provider"],
        searched_at=_parse(payload["searched_at"]),
        cache=cache,
    )


def document_payload(document: FetchedDocument) -> dict[str, Any]:
    return {
        "url": document.url,
        "final_url": document.final_url,
        "title": document.title,
        "text": document.text,
        "media_type": document.media_type,
        "fetched_at": _iso(document.fetched_at),
        "provider": document.provider,
        "truncated": document.truncated,
    }


def document_from(
    url: str, payload: dict[str, Any], cache: CacheInfo
) -> FetchedDocument:
    # The requested URL is the caller's; the cached page may have been asked
    # for with another spelling of the same normalised URL.
    return FetchedDocument(
        url=url,
        final_url=payload["final_url"],
        title=payload["title"],
        text=payload["text"],
        media_type=payload["media_type"],
        fetched_at=_parse(payload["fetched_at"]),
        provider=payload["provider"],
        truncated=payload["truncated"],
        cache=cache,
    )


class ResearchCache:
    def __init__(
        self,
        provider: ResearchProvider,
        database: Database,
        *,
        clock: Clock | None = None,
        search_ttl: timedelta = SEARCH_TTL,
        fetch_ttl: timedelta = FETCH_TTL,
    ) -> None:
        self.inner = provider
        self.name = provider.name
        self._database = database
        self._clock = clock
        self._ttl = {CacheKind.SEARCH: search_ttl, CacheKind.FETCH: fetch_ttl}

    def search(self, query: SearchQuery) -> SearchResults:
        return self._serve(
            CacheKind.SEARCH,
            search_key(query),
            lambda: self.inner.search(query),
            results_payload,
            lambda payload, cache: results_from(query, payload, cache),
        )

    def fetch(self, url: str) -> FetchedDocument:
        return self._serve(
            CacheKind.FETCH,
            fetch_key(url),
            lambda: self.inner.fetch(url),
            document_payload,
            lambda payload, cache: document_from(url, payload, cache),
        )

    def check(self) -> None:
        self.inner.check()

    def _serve(
        self,
        kind: CacheKind,
        key: str,
        call: Callable[[], T],
        to_payload: Callable[[T], dict[str, Any]],
        from_payload: Callable[[dict[str, Any], CacheInfo], T],
    ) -> T:
        now = self._now()
        with self._database.transaction() as connection:
            entry = ResearchCacheRepository(connection).get(kind.value, key)
        if entry is not None:
            payload, cached_at = entry
            if now - cached_at < self._ttl[kind]:
                return from_payload(payload, CacheInfo(CacheOutcome.HIT, cached_at))
        try:
            value = call()
        except ResearchProviderError as error:
            if entry is not None and error.retryable:
                payload, cached_at = entry
                return from_payload(
                    payload, CacheInfo(CacheOutcome.STALE_FALLBACK, cached_at)
                )
            raise
        with self._database.transaction() as connection:
            ResearchCacheRepository(connection).put(
                kind.value, key, self.name, to_payload(value), now
            )
        outcome = CacheOutcome.MISS if entry is None else CacheOutcome.REFRESH
        return from_payload(to_payload(value), CacheInfo(outcome, now))

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)
