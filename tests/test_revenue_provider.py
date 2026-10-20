"""Tests for the revenue provider contract and its seeded mock (O-170, prompt #170)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ai_youtube_agent.content.analytics import MetricScope
from ai_youtube_agent.content.revenue import RevenueStage
from ai_youtube_agent.providers.mock_revenue import MockRevenueProvider
from ai_youtube_agent.providers.mock_youtube_oauth import MockYouTubeOAuthProvider
from ai_youtube_agent.providers.revenue import (
    RevenueErrorCode,
    RevenuePage,
    RevenueProvider,
    RevenueProviderError,
    RevenueQuery,
    RevenueRow,
)
from ai_youtube_agent.providers.youtube_oauth import OAuthScope

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
START = datetime(2026, 10, 1, tzinfo=UTC)
END = datetime(2026, 10, 15, tzinfo=UTC)
CHANNEL = "UC" + "A" * 22
VIDEOS = ("vid000001ab", "vid000002ab", "vid000003ab")


def _clock() -> datetime:
    return NOW


def _query(**overrides) -> RevenueQuery:
    values = dict(
        youtube_channel_id=CHANNEL,
        stage=RevenueStage.ESTIMATED,
        scope=MetricScope.CHANNEL,
        period_start=START,
        period_end=END,
        revenue_types=("ads", "memberships"),
    )
    values.update(overrides)
    return RevenueQuery(**values)


def test_revenue_query_row_and_page_validation() -> None:
    with pytest.raises(ValueError):
        _query(youtube_channel_id="invalid")
    with pytest.raises(ValueError):
        _query(revenue_types=("unknown_stream",))
    with pytest.raises(ValueError):
        _query(revenue_types=("ads", "ads"))
    with pytest.raises(ValueError):
        _query(video_ids=VIDEOS)
    with pytest.raises(ValueError):
        _query(scope=MetricScope.VIDEO, video_ids=())

    row = RevenueRow(CHANNEL, RevenueStage.ESTIMATED, "ads", Decimal("12.34"), "USD")
    assert row.as_dict()["amount"] == "12.34"

    with pytest.raises(ValueError):
        RevenueRow(CHANNEL, RevenueStage.ESTIMATED, "ads", Decimal("-1"), "USD")
    with pytest.raises(ValueError):
        RevenueRow(CHANNEL, RevenueStage.ESTIMATED, "ads", Decimal("1"), "usd")

    # Stage mismatch between row and page is refused.
    with pytest.raises(ValueError):
        RevenuePage(
            rows=(row,),
            stage=RevenueStage.FINAL,
            next_page_token=None,
            source="mock_revenue",
            period_start=START,
            period_end=END,
            retrieved_at=NOW,
        )

    # Final revenue retrieved before period_end is refused.
    final_row = RevenueRow(CHANNEL, RevenueStage.FINAL, "ads", Decimal("10.00"), "USD")
    with pytest.raises(ValueError):
        RevenuePage(
            rows=(final_row,),
            stage=RevenueStage.FINAL,
            next_page_token=None,
            source="mock_revenue",
            period_start=START,
            period_end=END,
            retrieved_at=END - timedelta(minutes=1),
        )


def test_mock_keeps_estimated_and_final_strictly_separate_and_never_invents_revenue() -> None:
    provider = MockRevenueProvider(clock=_clock)
    assert isinstance(provider, RevenueProvider)
    assert provider.name == "mock_revenue"

    provider.set_channel_revenue(
        CHANNEL,
        stage=RevenueStage.ESTIMATED,
        amounts={"ads": Decimal("125.50"), "shorts_ads": Decimal("18.25")},
        currency="USD",
    )
    provider.set_channel_revenue(
        CHANNEL,
        stage=RevenueStage.FINAL,
        amounts={"ads": Decimal("122.10")},
        currency="USD",
    )

    est_page = provider.query(
        _query(
            stage=RevenueStage.ESTIMATED,
            revenue_types=("ads", "shorts_ads", "memberships"),
        )
    )
    assert [(r.revenue_type, r.amount) for r in est_page.rows] == [
        ("ads", Decimal("125.50")),
        ("shorts_ads", Decimal("18.25")),
    ]

    final_page = provider.query(
        _query(
            stage=RevenueStage.FINAL,
            revenue_types=("ads", "shorts_ads", "memberships"),
        )
    )
    assert [(r.revenue_type, r.amount) for r in final_page.rows] == [
        ("ads", Decimal("122.10")),
    ]

    # Querying final revenue before period_end is refused.
    with pytest.raises(RevenueProviderError) as early_final:
        provider.query(
            _query(
                stage=RevenueStage.FINAL,
                period_start=NOW - timedelta(days=5),
                period_end=NOW + timedelta(days=1),
            )
        )
    assert early_final.value.revenue_code is RevenueErrorCode.INVALID_QUERY


def test_video_pagination_oauth_monetary_scope_and_injected_failures() -> None:
    provider = MockRevenueProvider(clock=_clock)
    provider.set_video_revenue(
        CHANNEL,
        VIDEOS[0],
        stage=RevenueStage.ESTIMATED,
        amounts={"ads": Decimal("10.00")},
    )
    provider.set_video_revenue(
        CHANNEL,
        VIDEOS[2],
        stage=RevenueStage.ESTIMATED,
        amounts={"ads": Decimal("30.00")},
    )

    first = provider.query(
        _query(
            scope=MetricScope.VIDEO,
            video_ids=VIDEOS,
            revenue_types=("ads",),
            page_size=2,
        )
    )
    second = provider.query(
        _query(
            scope=MetricScope.VIDEO,
            video_ids=VIDEOS,
            revenue_types=("ads",),
            page_size=2,
            page_token=first.next_page_token,
        )
    )
    assert [r.subject_id for r in first.rows] == [VIDEOS[0]]
    assert first.next_page_token == "o2"
    assert [r.subject_id for r in second.rows] == [VIDEOS[2]]
    assert second.next_page_token is None

    # Token must carry YT_ANALYTICS_MONETARY_READONLY.
    good_token = MockYouTubeOAuthProvider(clock=_clock).get_token("ch-1")
    non_monetary = MockYouTubeOAuthProvider(
        clock=_clock, default_scopes=[OAuthScope.YT_ANALYTICS_READONLY]
    ).get_token("ch-2")

    assert provider.query(_query(), token=good_token).rows == ()
    with pytest.raises(RevenueProviderError) as auth_err:
        provider.query(_query(), token=non_monetary)
    assert auth_err.value.revenue_code is RevenueErrorCode.UNAUTHORIZED

    provider.fail_next(RevenueErrorCode.RATE_LIMITED)
    with pytest.raises(RevenueProviderError) as rate_err:
        provider.query(_query())
    assert rate_err.value.revenue_code is RevenueErrorCode.RATE_LIMITED
    assert rate_err.value.retryable is True
