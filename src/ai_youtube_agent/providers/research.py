"""Research Provider (Prompt Pack v8, prompt #054), context C4 Research.

The interface every research source implements, as the user approved on
2026-10-02:

- ``ResearchProvider`` is a synchronous ``Protocol`` with ``search``,
  ``fetch`` and ``check`` (the health probe). ``name`` identifies the provider
  in results and errors.
- ``search(SearchQuery)`` takes a text (at most 500 characters), an optional
  BCP-47 ``language``, an optional ISO 3166-1 ``market`` and ``max_results``
  (1 to 50), and returns ``SearchResults``: ordered ``SearchHit`` values (an
  http(s) ``url``, ``title``, ``snippet``, optional UTC ``published_at``,
  ``rank`` 1..n), no URL twice, at most ``max_results``. No results is a valid
  answer, not an error.
- ``fetch(url)`` returns a ``FetchedDocument``: the requested and final URL
  (after redirects), optional ``title``, plain ``text`` of at most 200,000
  characters (``truncated`` says whether the provider cut it), ``media_type``
  and UTC ``fetched_at``.
- Failures raise ``ResearchProviderError`` (a ``ProviderError``) with a
  ``ResearchErrorCode``: ``research.unavailable``, ``research.rate_limited``
  and ``research.timeout`` are retryable; ``research.not_found``,
  ``research.blocked`` and ``research.invalid_query`` are not. #062 retries
  only retryable failures.

These values are what providers return. #055 normalises them into the Source
model. The mock is ``providers/mock_research.py``.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from ai_youtube_agent.content.strategy import COUNTRY_PATTERN, LANGUAGE_TAG_PATTERN
from ai_youtube_agent.core.errors import ProviderError

MAX_QUERY_LENGTH = 500
MAX_RESULTS = 50
DEFAULT_MAX_RESULTS = 10
MAX_DOCUMENT_TEXT = 200_000
MAX_URL_LENGTH = 2048


def check_url(name: str, url: str) -> None:
    """An absolute http(s) URL with a host, at most 2048 characters."""
    if not isinstance(url, str) or len(url) > MAX_URL_LENGTH:
        raise ValueError(f"{name} must be a URL of at most {MAX_URL_LENGTH} characters")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{name} {url!r} must be an absolute http(s) URL")


def _check_utc(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")


@dataclass(frozen=True)
class SearchQuery:
    text: str
    language: str | None = None
    market: str | None = None
    max_results: int = DEFAULT_MAX_RESULTS

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("search text must not be empty")
        if len(self.text) > MAX_QUERY_LENGTH:
            raise ValueError(
                f"search text must be at most {MAX_QUERY_LENGTH} characters"
            )
        if self.language is not None and not LANGUAGE_TAG_PATTERN.match(self.language):
            raise ValueError(f"language {self.language!r} must be a BCP-47 tag")
        if self.market is not None and not COUNTRY_PATTERN.match(self.market):
            raise ValueError(f"market {self.market!r} must be an ISO 3166-1 code")
        if (
            isinstance(self.max_results, bool)
            or not isinstance(self.max_results, int)
            or not 1 <= self.max_results <= MAX_RESULTS
        ):
            raise ValueError(
                f"max_results must be a whole number from 1 to {MAX_RESULTS}"
            )


@dataclass(frozen=True)
class SearchHit:
    url: str
    title: str
    rank: int
    snippet: str = ""
    published_at: datetime | None = None

    def __post_init__(self) -> None:
        check_url("hit url", self.url)
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("hit title must not be empty")
        if not isinstance(self.snippet, str):
            raise TypeError("snippet must be text")
        if (
            isinstance(self.rank, bool)
            or not isinstance(self.rank, int)
            or self.rank < 1
        ):
            raise ValueError("rank must be a whole number of 1 or more")
        if self.published_at is not None:
            _check_utc("published_at", self.published_at)


@dataclass(frozen=True)
class SearchResults:
    query: SearchQuery
    hits: tuple[SearchHit, ...]
    provider: str
    searched_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.hits, tuple) or not all(
            isinstance(hit, SearchHit) for hit in self.hits
        ):
            raise TypeError("hits must be a tuple of SearchHit values")
        if len(self.hits) > self.query.max_results:
            raise ValueError("a search must not return more than max_results hits")
        if [hit.rank for hit in self.hits] != list(range(1, len(self.hits) + 1)):
            raise ValueError("hits must be ranked 1..n in order")
        if len({hit.url for hit in self.hits}) != len(self.hits):
            raise ValueError("a search must not return the same URL twice")
        if not self.provider:
            raise ValueError("provider must not be empty")
        _check_utc("searched_at", self.searched_at)


@dataclass(frozen=True)
class FetchedDocument:
    url: str
    final_url: str
    title: str | None
    text: str
    media_type: str
    fetched_at: datetime
    provider: str
    truncated: bool = False

    def __post_init__(self) -> None:
        check_url("url", self.url)
        check_url("final_url", self.final_url)
        if not isinstance(self.text, str):
            raise TypeError("text must be text")
        if len(self.text) > MAX_DOCUMENT_TEXT:
            raise ValueError(
                f"document text must be at most {MAX_DOCUMENT_TEXT} characters"
            )
        if "/" not in self.media_type:
            raise ValueError(
                f"media type {self.media_type!r} must look like 'text/html'"
            )
        if not self.provider:
            raise ValueError("provider must not be empty")
        if not isinstance(self.truncated, bool):
            raise TypeError("truncated must be true or false")
        _check_utc("fetched_at", self.fetched_at)


class ResearchErrorCode(StrEnum):
    UNAVAILABLE = "research.unavailable"
    RATE_LIMITED = "research.rate_limited"
    TIMEOUT = "research.timeout"
    NOT_FOUND = "research.not_found"
    BLOCKED = "research.blocked"
    INVALID_QUERY = "research.invalid_query"

    @property
    def retryable(self) -> bool:
        return self in RETRYABLE_CODES


RETRYABLE_CODES = frozenset(
    {
        ResearchErrorCode.UNAVAILABLE,
        ResearchErrorCode.RATE_LIMITED,
        ResearchErrorCode.TIMEOUT,
    }
)

USER_MESSAGES = {
    ResearchErrorCode.UNAVAILABLE: "The research service is not available. "
    "Please try again later.",
    ResearchErrorCode.RATE_LIMITED: "The research service is busy. "
    "Please try again later.",
    ResearchErrorCode.TIMEOUT: "The research service took too long to answer. "
    "Please try again later.",
    ResearchErrorCode.NOT_FOUND: "The source could not be found.",
    ResearchErrorCode.BLOCKED: "The source does not allow access.",
    ResearchErrorCode.INVALID_QUERY: "The research request was not accepted.",
}


class ResearchProviderError(ProviderError):
    """A research provider failed; ``retryable`` follows the code."""

    default_code = ResearchErrorCode.UNAVAILABLE.value

    def __init__(
        self, code: ResearchErrorCode, detail: str = "", *, provider: str
    ) -> None:
        super().__init__(
            detail or code.value,
            provider=provider,
            code=code.value,
            user_message=USER_MESSAGES[code],
            retryable=code.retryable,
        )
        self.research_code = code


@runtime_checkable
class ResearchProvider(Protocol):
    name: str

    def search(self, query: SearchQuery) -> SearchResults: ...

    def fetch(self, url: str) -> FetchedDocument: ...

    def check(self) -> None:
        """Raise when the provider cannot be used (health probe)."""
        ...
