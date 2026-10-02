"""E-054 Research Provider (Prompt Pack v8, prompt #054).

Rules the user approved on 2026-10-02:

- ``ResearchProvider`` is a synchronous Protocol with ``search``, ``fetch`` and
  ``check``;
- typed values: ``SearchQuery`` (text, language, market, max_results 1-50),
  ``SearchResults`` of ranked ``SearchHit``s, and ``FetchedDocument`` (text at
  most 200,000 characters, ``truncated``);
- failures are ``ResearchProviderError`` (a ``ProviderError``) whose
  ``retryable`` follows the ``ResearchErrorCode``;
- ``MockResearchProvider`` is in memory and deterministic, with scripted
  results, pages, failures and health; ``Settings.research_provider`` selects
  it (``mock`` is the only kind), and bootstrap registers it with a
  ``research_provider`` provider health check.
"""

import threading
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.core.config import Environment, ResearchProviderKind, Settings
from ai_youtube_agent.core.errors import ErrorCategory, ProviderError
from ai_youtube_agent.main import create_app
from ai_youtube_agent.providers.mock_research import (
    MockHit,
    MockResearchProvider,
    Operation,
)
from ai_youtube_agent.providers.research import (
    MAX_DOCUMENT_TEXT,
    FetchedDocument,
    ResearchErrorCode,
    ResearchProvider,
    ResearchProviderError,
    SearchHit,
    SearchQuery,
    SearchResults,
)
from ai_youtube_agent.providers.research_cache import ResearchCache

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
URL = "https://example.com/a"
HITS = [
    MockHit("https://example.com/a", "Budgeting basics", "How to budget"),
    MockHit("https://example.com/b", "Index funds", published_at=T0),
    MockHit("https://example.com/c", "Saving tips"),
]


def mock() -> MockResearchProvider:
    provider = MockResearchProvider(clock=lambda: T0)
    provider.add_results("personal finance", HITS)
    provider.add_page(URL, "Budget every month.", title="Budgeting basics")
    return provider


# Values


def test_query_defaults() -> None:
    query = SearchQuery("personal finance")

    assert (query.language, query.market, query.max_results) == (None, None, 10)


@pytest.mark.parametrize(
    "build",
    [
        lambda: SearchQuery(" "),
        lambda: SearchQuery("x" * 501),
        lambda: SearchQuery("q", language="Vietnamese"),
        lambda: SearchQuery("q", market="vn"),
        lambda: SearchQuery("q", max_results=0),
        lambda: SearchQuery("q", max_results=51),
        lambda: SearchQuery("q", max_results=True),
        lambda: SearchHit("ftp://example.com/a", "T", 1),
        lambda: SearchHit("example.com/a", "T", 1),
        lambda: SearchHit("https:///nohost", "T", 1),
        lambda: SearchHit("https://example.com/" + "a" * 2048, "T", 1),
        lambda: SearchHit(URL, " ", 1),
        lambda: SearchHit(URL, "T", 0),
        lambda: SearchHit(URL, "T", 1, published_at=datetime(2026, 1, 1)),
    ],
)
def test_value_limits(build) -> None:
    with pytest.raises(ValueError):
        build()


def test_query_accepts_the_edges() -> None:
    query = SearchQuery("x" * 500, language="vi-VN", market="VN", max_results=50)
    assert query.max_results == 50


def hit(rank: int, url: str = URL) -> SearchHit:
    return SearchHit(url, "Title", rank)


@pytest.mark.parametrize(
    "hits",
    [
        (hit(2),),
        (hit(1), hit(1, "https://example.com/b")),
        (hit(1), hit(2)),
        tuple(hit(n, f"https://example.com/{n}") for n in range(1, 3)),
    ],
    ids=["gap", "rank-repeat", "url-repeat", "too-many"],
)
def test_results_must_be_ranked_unique_and_within_the_limit(hits) -> None:
    with pytest.raises(ValueError):
        SearchResults(SearchQuery("q", max_results=1), hits, "mock", T0)


def test_empty_results_are_valid() -> None:
    assert SearchResults(SearchQuery("q"), (), "mock", T0).hits == ()


@pytest.mark.parametrize(
    "changes",
    [
        {"text": "x" * (MAX_DOCUMENT_TEXT + 1)},
        {"final_url": "not a url"},
        {"media_type": "html"},
        {"fetched_at": T0.astimezone(timezone(timedelta(hours=7)))},
        {"provider": ""},
    ],
)
def test_document_limits(changes) -> None:
    values = dict(
        url=URL,
        final_url=URL,
        title=None,
        text="t",
        media_type="text/html",
        fetched_at=T0,
        provider="mock",
    )
    with pytest.raises(ValueError):
        FetchedDocument(**{**values, **changes})


# Errors


@pytest.mark.parametrize(
    ("code", "retryable"),
    [
        (ResearchErrorCode.UNAVAILABLE, True),
        (ResearchErrorCode.RATE_LIMITED, True),
        (ResearchErrorCode.TIMEOUT, True),
        (ResearchErrorCode.NOT_FOUND, False),
        (ResearchErrorCode.BLOCKED, False),
        (ResearchErrorCode.INVALID_QUERY, False),
    ],
)
def test_errors_are_provider_errors_with_retryable_codes(code, retryable) -> None:
    error = ResearchProviderError(code, "upstream said 503 at 10.0.0.5", provider="x")

    assert isinstance(error, ProviderError)
    assert error.code == code.value
    assert error.retryable is retryable
    assert error.provider == "x"
    public = error.to_public()
    assert public.category is ErrorCategory.PROVIDER
    assert "10.0.0.5" not in public.message


# Mock: search


def test_the_mock_is_a_research_provider() -> None:
    assert isinstance(mock(), ResearchProvider)


def test_search_returns_ranked_hits_in_order() -> None:
    results = mock().search(SearchQuery(" Personal Finance ", max_results=10))

    assert [h.url for h in results.hits] == [h.url for h in HITS]
    assert [h.rank for h in results.hits] == [1, 2, 3]
    assert results.hits[1].published_at == T0
    assert (results.provider, results.searched_at) == ("mock", T0)


def test_search_honours_max_results() -> None:
    results = mock().search(SearchQuery("personal finance", max_results=2))

    assert [h.rank for h in results.hits] == [1, 2]


def test_an_unknown_search_finds_nothing() -> None:
    assert mock().search(SearchQuery("cooking")).hits == ()


def test_repeated_canned_urls_are_returned_once() -> None:
    provider = mock()
    provider.add_results("dupes", [HITS[0], HITS[0], HITS[1]])

    results = provider.search(SearchQuery("dupes"))

    assert [h.rank for h in results.hits] == [1, 2]


def test_canned_hits_need_valid_urls() -> None:
    with pytest.raises(ValueError):
        mock().add_results("bad", [MockHit("nope", "T")])


# Mock: fetch


def test_fetch_returns_the_registered_page() -> None:
    document = mock().fetch(URL)

    assert document.text == "Budget every month."
    assert document.title == "Budgeting basics"
    assert document.final_url == URL
    assert (document.media_type, document.truncated) == ("text/html", False)
    assert document.fetched_at == T0


def test_fetch_follows_a_registered_redirect() -> None:
    provider = mock()
    provider.add_page(
        "http://example.com/old", "moved", final_url="https://example.com/new"
    )

    assert provider.fetch("http://example.com/old").final_url == (
        "https://example.com/new"
    )


def test_fetch_of_an_unknown_url_is_not_found() -> None:
    with pytest.raises(ResearchProviderError) as caught:
        mock().fetch("https://example.com/missing")

    assert caught.value.research_code is ResearchErrorCode.NOT_FOUND
    assert not caught.value.retryable


def test_long_text_is_truncated_and_marked() -> None:
    provider = mock()
    provider.add_page(URL, "x" * (MAX_DOCUMENT_TEXT + 10))

    document = provider.fetch(URL)

    assert len(document.text) == MAX_DOCUMENT_TEXT
    assert document.truncated


# Mock: scripted failures, health and calls


def test_scripted_failures_happen_once_each_then_stop() -> None:
    provider = mock()
    provider.fail_next(ResearchErrorCode.RATE_LIMITED, times=2)

    for _ in range(2):
        with pytest.raises(ResearchProviderError) as caught:
            provider.search(SearchQuery("personal finance"))
        assert caught.value.retryable

    assert len(provider.search(SearchQuery("personal finance")).hits) == 3


def test_a_failure_for_one_operation_skips_the_other() -> None:
    provider = mock()
    provider.fail_next(ResearchErrorCode.TIMEOUT, operation=Operation.FETCH)

    assert provider.search(SearchQuery("personal finance")).hits
    with pytest.raises(ResearchProviderError) as caught:
        provider.fetch(URL)
    assert caught.value.research_code is ResearchErrorCode.TIMEOUT
    assert provider.fetch(URL).text


def test_fail_next_needs_a_positive_count() -> None:
    with pytest.raises(ValueError):
        mock().fail_next(ResearchErrorCode.TIMEOUT, times=0)


def test_check_follows_the_health_switch() -> None:
    provider = mock()
    provider.check()

    provider.set_healthy(False)

    with pytest.raises(ResearchProviderError) as caught:
        provider.check()
    assert caught.value.research_code is ResearchErrorCode.UNAVAILABLE


def test_calls_are_recorded_including_failed_ones() -> None:
    provider = mock()
    provider.fail_next(ResearchErrorCode.BLOCKED, operation=Operation.FETCH)
    provider.search(SearchQuery("personal finance"))
    with pytest.raises(ResearchProviderError):
        provider.fetch(URL)

    assert provider.calls == [("search", "personal finance"), ("fetch", URL)]


def test_the_mock_is_safe_across_threads() -> None:
    provider = mock()
    provider.fail_next(ResearchErrorCode.UNAVAILABLE, times=50)
    failures = []

    def work() -> None:
        for _ in range(10):
            try:
                provider.fetch(URL)
            except ResearchProviderError:
                failures.append(1)

    threads = [threading.Thread(target=work) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(failures) == 50
    assert len(provider.calls) == 100


# Wiring


def test_the_mock_is_the_default_and_only_kind() -> None:
    assert list(ResearchProviderKind) == [ResearchProviderKind.MOCK]
    assert Settings().research_provider is ResearchProviderKind.MOCK


def test_unknown_kinds_are_refused() -> None:
    with pytest.raises(ValueError):
        Settings(research_provider="google")


def test_bootstrap_registers_one_mock(tmp_path: Path) -> None:
    container = build_container(
        Settings(environment=Environment.TEST, database_path=tmp_path / "a.db")
    )

    provider = container.resolve(ResearchProvider)

    # Since E-061 (user decision 2026-10-03) the mock is used through the cache.
    assert isinstance(provider, ResearchCache)
    assert isinstance(provider.inner, MockResearchProvider)
    assert container.resolve(ResearchProvider) is provider


def test_an_unhealthy_provider_degrades_health(tmp_path: Path) -> None:
    container = build_container(
        Settings(environment=Environment.TEST, database_path=tmp_path / "a.db")
    )
    container.resolve(ResearchProvider).inner.set_healthy(False)

    with TestClient(create_app(container)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    [check] = [c for c in body["checks"] if c["name"] == "research_provider"]
    assert (check["kind"], check["status"]) == ("provider", "degraded")
