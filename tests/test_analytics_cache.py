"""Tests for the analytics cache and safe invalidation (N-168, prompt #168)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ai_youtube_agent.content.analytics import MetricScope
from ai_youtube_agent.providers.analytics import (
    AnalyticsErrorCode,
    AnalyticsProvider,
    AnalyticsProviderError,
    AnalyticsQuery,
)
from ai_youtube_agent.providers.analytics_cache import (
    AnalyticsCache,
    AnalyticsCacheOutcome,
)
from ai_youtube_agent.providers.mock_analytics import MockAnalyticsProvider
from ai_youtube_agent.providers.mock_youtube_oauth import MockYouTubeOAuthProvider
from ai_youtube_agent.providers.youtube_oauth import OAuthScope

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
START = datetime(2026, 10, 1, tzinfo=UTC)
END = datetime(2026, 10, 15, tzinfo=UTC)
CHANNEL = "UC" + "A" * 22
VIDEOS = ("vid000001ab", "vid000002ab")


def _channel_query(**overrides) -> AnalyticsQuery:
    values = dict(
        youtube_channel_id=CHANNEL,
        scope=MetricScope.CHANNEL,
        period_start=START,
        period_end=END,
        metrics=("views", "likes"),
    )
    values.update(overrides)
    return AnalyticsQuery(**values)


def _video_query(**overrides) -> AnalyticsQuery:
    values = dict(
        youtube_channel_id=CHANNEL,
        scope=MetricScope.VIDEO,
        period_start=START,
        period_end=END,
        metrics=("views",),
        video_ids=VIDEOS,
    )
    values.update(overrides)
    return AnalyticsQuery(**values)


def test_serves_fresh_hits_refreshes_expired_and_falls_back_on_retryable_failure() -> None:
    now = NOW
    inner = MockAnalyticsProvider(clock=lambda: now)
    inner.set_channel_metrics(CHANNEL, {"views": Decimal("100"), "likes": Decimal("10")})
    cache = AnalyticsCache(inner, clock=lambda: now)

    assert isinstance(cache, AnalyticsProvider)
    assert cache.name == inner.name

    # First call: MISS -> calls inner provider.
    page1, info1 = cache.query_with_info(_channel_query())
    assert info1.outcome is AnalyticsCacheOutcome.MISS
    assert info1.stale is False and info1.is_complete is True
    assert inner.calls == 1

    # Second call 2 hours later (within 24h settled TTL): HIT -> no inner call.
    now = now + timedelta(hours=2)
    page2, info2 = cache.query_with_info(_channel_query())
    assert info2.outcome is AnalyticsCacheOutcome.HIT
    assert page2 == page1
    assert inner.calls == 1

    # Advance past 24h settled TTL: retryable provider failure -> STALE_FALLBACK.
    now = now + timedelta(hours=23)
    inner.fail_next(AnalyticsErrorCode.UNAVAILABLE)
    fallback_page, fallback_info = cache.query_with_info(_channel_query())
    assert fallback_info.outcome is AnalyticsCacheOutcome.STALE_FALLBACK
    assert fallback_info.stale is True
    assert fallback_page == page1
    assert inner.calls == 2

    # Next call succeeds -> REFRESH replaces the cached entry.
    inner.set_channel_metrics(CHANNEL, {"views": Decimal("250"), "likes": Decimal("25")})
    refreshed_page, refresh_info = cache.query_with_info(_channel_query())
    assert refresh_info.outcome is AnalyticsCacheOutcome.REFRESH
    assert refreshed_page.rows[0].metrics["views"] == Decimal("250")
    assert inner.calls == 3

    stats = cache.stats()
    assert stats.hits == 1
    assert stats.misses == 1
    assert stats.stale_fallbacks == 1
    assert stats.refreshes == 1


def test_enforces_token_validity_even_on_a_cache_hit() -> None:
    now = NOW
    inner = MockAnalyticsProvider(clock=lambda: now)
    inner.set_channel_metrics(CHANNEL, {"views": Decimal("100")})
    cache = AnalyticsCache(inner, clock=lambda: now)

    oauth = MockYouTubeOAuthProvider(clock=lambda: now)
    good_token = oauth.get_token("ch-1")
    bad_scope_token = MockYouTubeOAuthProvider(
        clock=lambda: now, default_scopes=[OAuthScope.YOUTUBE_READONLY]
    ).get_token("ch-2")

    cache.query(_channel_query(), token=good_token)
    assert inner.calls == 1

    with pytest.raises(AnalyticsProviderError) as caught:
        cache.query(_channel_query(), token=bad_scope_token)
    assert caught.value.analytics_code is AnalyticsErrorCode.UNAUTHORIZED
    assert inner.calls == 1


def test_invalidates_by_channel_video_and_overlapping_period() -> None:
    now = NOW
    inner = MockAnalyticsProvider(clock=lambda: now)
    inner.set_channel_metrics(CHANNEL, {"views": Decimal("100")})
    inner.set_video_metrics(CHANNEL, VIDEOS[0], {"views": Decimal("50")})
    inner.set_video_metrics(CHANNEL, VIDEOS[1], {"views": Decimal("60")})
    cache = AnalyticsCache(inner, clock=lambda: now)

    cache.query(_channel_query())
    cache.query(_video_query())
    assert cache.stats().entries == 2

    # Invalidate video 1 -> drops the video query entry, keeps the channel entry.
    removed = cache.invalidate_video(CHANNEL, VIDEOS[0])
    assert removed == 1
    assert cache.stats().entries == 1

    # Re-populate video entry, then invalidate overlapping period.
    cache.query(_video_query())
    assert cache.stats().entries == 2
    removed_period = cache.invalidate_period(
        CHANNEL,
        period_start=START + timedelta(days=5),
        period_end=END + timedelta(days=2),
    )
    assert removed_period == 2
    assert cache.stats().entries == 0

    # Re-populate and invalidate channel.
    cache.query(_channel_query())
    assert cache.invalidate_channel(CHANNEL) == 1
    assert cache.stats().entries == 0
