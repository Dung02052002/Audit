"""Tests for the analytics provider contract and its seeded mock (N-158, prompt #158)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ai_youtube_agent.content.analytics import MetricScope
from ai_youtube_agent.providers.analytics import (
    KNOWN_METRICS,
    AnalyticsErrorCode,
    AnalyticsPage,
    AnalyticsProvider,
    AnalyticsProviderError,
    AnalyticsQuery,
    AnalyticsRow,
)
from ai_youtube_agent.providers.mock_analytics import MockAnalyticsProvider
from ai_youtube_agent.providers.mock_youtube_oauth import MockYouTubeOAuthProvider
from ai_youtube_agent.providers.youtube_oauth import OAuthScope

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
START = datetime(2026, 10, 1, tzinfo=UTC)
END = datetime(2026, 10, 15, tzinfo=UTC)
CHANNEL = "UC" + "A" * 22
VIDEOS = tuple(f"vid{n:06d}ab" for n in range(5))  # 11 characters each


def _clock() -> datetime:
    return NOW


def _query(**overrides) -> AnalyticsQuery:
    values = dict(
        youtube_channel_id=CHANNEL,
        scope=MetricScope.CHANNEL,
        period_start=START,
        period_end=END,
        metrics=("views", "subscribers_gained"),
    )
    values.update(overrides)
    return AnalyticsQuery(**values)


def _video_query(**overrides) -> AnalyticsQuery:
    return _query(scope=MetricScope.VIDEO, video_ids=VIDEOS, **overrides)


@pytest.fixture
def provider() -> MockAnalyticsProvider:
    return MockAnalyticsProvider(clock=_clock)


# --- AnalyticsQuery ------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"youtube_channel_id": "not-a-channel"},
        {"period_start": datetime(2026, 10, 1)},
        {"period_end": START},
        {"period_end": START + timedelta(days=367)},
        {"metrics": ()},
        {"metrics": ("views", "views")},
        {"metrics": ("not_a_metric",)},
        {"metrics": tuple(sorted(KNOWN_METRICS))[:10] + ("views",)},
        {"video_ids": VIDEOS},
        {"page_size": 0},
        {"page_size": 201},
        {"page_size": True},
        {"page_token": "has space"},
        {"page_token": ""},
    ],
)
def test_a_bad_channel_query_is_refused(overrides: dict) -> None:
    with pytest.raises(ValueError):
        _query(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"video_ids": ()},
        {"video_ids": ("short",)},
        {"video_ids": (VIDEOS[0], VIDEOS[0])},
        {"video_ids": tuple(f"vid{n:06d}ab" for n in range(51))},
    ],
)
def test_a_bad_video_query_is_refused(overrides: dict) -> None:
    with pytest.raises(ValueError):
        _query(scope=MetricScope.VIDEO, **overrides)


def test_a_valid_video_query_is_accepted() -> None:
    query = _video_query(page_size=2, page_token="o2")

    assert query.video_ids == VIDEOS and query.page_token == "o2"


# --- rows and pages --------------------------------------------------------------------


def test_a_row_holds_only_finite_known_metrics_and_is_never_empty() -> None:
    row = AnalyticsRow("subject", {"views": Decimal("5")})
    assert row.as_dict() == {"subject_id": "subject", "metrics": {"views": "5"}}
    with pytest.raises(TypeError):
        row.metrics["views"] = Decimal("6")  # type: ignore[index]

    with pytest.raises(ValueError):
        AnalyticsRow("subject", {})
    with pytest.raises(ValueError):
        AnalyticsRow("subject", {"views": 5})  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        AnalyticsRow("subject", {"views": Decimal("NaN")})
    with pytest.raises(ValueError):
        AnalyticsRow("subject", {"not_a_metric": Decimal("1")})


def test_a_page_refuses_two_rows_for_one_subject_and_reports_its_last_page() -> None:
    row = AnalyticsRow("subject", {"views": Decimal("1")})

    with pytest.raises(ValueError):
        AnalyticsPage((row, row), None, "mock_analytics", START, END, NOW)

    page = AnalyticsPage((row,), None, "mock_analytics", START, END, NOW)
    assert page.is_last
    assert not AnalyticsPage((row,), "o1", "mock_analytics", START, END, NOW).is_last
    with pytest.raises(ValueError):
        AnalyticsPage((row,), None, "Bad Name", START, END, NOW)
    with pytest.raises(ValueError):
        AnalyticsPage(
            (row,), None, "mock_analytics", START, END, NOW, data_through=START - timedelta(days=1)
        )


# --- the mock ----------------------------------------------------------------------------


def test_the_mock_is_an_analytics_provider(provider: MockAnalyticsProvider) -> None:
    assert isinstance(provider, AnalyticsProvider)
    assert provider.name == "mock_analytics"


def test_channel_metrics_are_exactly_the_seeded_ones(
    provider: MockAnalyticsProvider,
) -> None:
    provider.set_channel_metrics(CHANNEL, {"views": Decimal("1200"), "likes": Decimal("30")})

    page = provider.query(
        _query(metrics=("views", "subscribers_gained", "average_view_percentage"))
    )

    [row] = page.rows
    assert row.subject_id == CHANNEL
    assert dict(row.metrics) == {"views": Decimal("1200")}
    assert "subscribers_gained" not in row.metrics
    assert "average_view_percentage" not in row.metrics
    assert page.is_last
    assert page.source == "mock_analytics"
    assert (page.period_start, page.period_end, page.retrieved_at) == (START, END, NOW)


def test_a_channel_without_requested_data_has_no_row_and_an_unknown_one_fails(
    provider: MockAnalyticsProvider,
) -> None:
    provider.set_channel_metrics(CHANNEL, {"likes": Decimal("3")})

    assert provider.query(_query(metrics=("views",))).rows == ()

    with pytest.raises(AnalyticsProviderError) as caught:
        provider.query(_query(youtube_channel_id="UC" + "B" * 22))
    assert caught.value.analytics_code is AnalyticsErrorCode.NOT_FOUND
    assert caught.value.retryable is False


def test_video_queries_page_over_the_requested_ids_and_skip_videos_without_data(
    provider: MockAnalyticsProvider,
) -> None:
    for index in (0, 1, 3, 4):  # VIDEOS[2] has no data at all
        provider.set_video_metrics(
            CHANNEL, VIDEOS[index], {"views": Decimal(index * 10 + 1)}
        )

    first = provider.query(_video_query(page_size=2))
    second = provider.query(_video_query(page_size=2, page_token=first.next_page_token))
    third = provider.query(_video_query(page_size=2, page_token=second.next_page_token))

    assert [row.subject_id for row in first.rows] == [VIDEOS[0], VIDEOS[1]]
    assert first.next_page_token == "o2"
    assert [row.subject_id for row in second.rows] == [VIDEOS[3]]
    assert second.next_page_token == "o4"
    assert [row.subject_id for row in third.rows] == [VIDEOS[4]]
    assert third.next_page_token is None
    seen = [row.subject_id for page in (first, second, third) for row in page.rows]
    assert VIDEOS[2] not in seen


def test_a_page_token_from_another_query_is_refused(
    provider: MockAnalyticsProvider,
) -> None:
    provider.set_video_metrics(CHANNEL, VIDEOS[0], {"views": Decimal("1")})

    for token in ("o99", "p2", "o-1"):
        with pytest.raises(AnalyticsProviderError) as caught:
            provider.query(_video_query(page_token=token))
        assert caught.value.analytics_code is AnalyticsErrorCode.INVALID_QUERY

    provider.set_channel_metrics(CHANNEL, {"views": Decimal("1")})
    with pytest.raises(AnalyticsProviderError):
        provider.query(_query(page_token="o1"))


def test_data_through_follows_the_provider_lag(provider: MockAnalyticsProvider) -> None:
    provider.set_channel_metrics(CHANNEL, {"views": Decimal("1")})

    past = provider.query(_query())
    recent = provider.query(
        _query(period_start=NOW - timedelta(days=10), period_end=NOW)
    )
    too_new = provider.query(
        _query(period_start=NOW - timedelta(days=1), period_end=NOW + timedelta(days=1))
    )

    assert past.data_through == END
    assert recent.data_through == NOW - timedelta(days=2)
    assert too_new.data_through is None


def test_failures_can_be_injected_and_retryable_ones_say_so(
    provider: MockAnalyticsProvider,
) -> None:
    provider.set_channel_metrics(CHANNEL, {"views": Decimal("1")})
    provider.fail_next(AnalyticsErrorCode.RATE_LIMITED, times=2)
    provider.fail_next(AnalyticsErrorCode.QUOTA_EXCEEDED)

    expected = [
        (AnalyticsErrorCode.RATE_LIMITED, True),
        (AnalyticsErrorCode.RATE_LIMITED, True),
        (AnalyticsErrorCode.QUOTA_EXCEEDED, False),
    ]
    for code, retryable in expected:
        with pytest.raises(AnalyticsProviderError) as caught:
            provider.query(_query())
        assert caught.value.analytics_code is code
        assert caught.value.retryable is retryable

    assert provider.query(_query()).rows
    assert provider.calls == 4


def test_the_token_needs_the_read_only_analytics_scope(
    provider: MockAnalyticsProvider,
) -> None:
    provider.set_channel_metrics(CHANNEL, {"views": Decimal("1")})
    good = MockYouTubeOAuthProvider(clock=_clock).get_token("channel-1")
    limited = MockYouTubeOAuthProvider(
        clock=_clock, default_scopes=[OAuthScope.YOUTUBE_READONLY]
    ).get_token("channel-1")

    assert provider.query(_query(), token=good).rows
    with pytest.raises(AnalyticsProviderError) as caught:
        provider.query(_query(), token=limited)
    assert caught.value.analytics_code is AnalyticsErrorCode.UNAUTHORIZED


def test_health_follows_the_switch(provider: MockAnalyticsProvider) -> None:
    provider.check()
    provider.set_healthy(False)

    with pytest.raises(AnalyticsProviderError) as caught:
        provider.check()

    assert caught.value.analytics_code is AnalyticsErrorCode.UNAVAILABLE


def test_only_known_finite_metrics_can_be_seeded(
    provider: MockAnalyticsProvider,
) -> None:
    with pytest.raises(ValueError):
        provider.set_channel_metrics(CHANNEL, {"mystery": Decimal("1")})
    with pytest.raises(ValueError):
        provider.set_video_metrics(CHANNEL, VIDEOS[0], {"views": 5})  # type: ignore[dict-item]
