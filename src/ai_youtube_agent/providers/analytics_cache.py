"""Analytics cache with safe invalidation (Prompt Pack v8, prompt #168), context C13.

``AnalyticsCache`` wraps any ``AnalyticsProvider`` and implements ``AnalyticsProvider``
itself so sync services (#159, #160, #164, #165, #166) can use it transparently:

- Keyed deterministically by ``youtube_channel_id``, ``scope``, ``period_start``,
  ``period_end``, ``metrics``, ``video_ids``, ``page_size`` and ``page_token``.
- Authorisation is checked on **every** call (even on a cache hit) when a token is
  supplied, so an expired or unauthorized token never reads cached channel data.
- Freshness follows ``AnalyticsFreshnessPolicy`` (#167): settled (complete) periods
  stay fresh for ``settled_period_max_age`` (default 24 h), while lagging periods use
  the shorter ``channel_max_age`` (6 h) or ``video_max_age`` (12 h).
- Only successful ``AnalyticsPage`` responses are cached; errors are never cached.
- When a refresh fails with a retryable ``AnalyticsProviderError`` and a prior
  (non-invalidated) entry exists, ``allow_stale_fallback=True`` serves the stale page
  and records ``AnalyticsCacheOutcome.STALE_FALLBACK``.
- Safe invalidation: ``invalidate_channel``, ``invalidate_video``,
  ``invalidate_period`` (overlapping windows) and ``clear`` remove entries
  immediately under a lock so invalidated data is never served again.
"""

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from ai_youtube_agent.content.analytics import MetricScope
from ai_youtube_agent.content.analytics_freshness import (
     DEFAULT_FRESHNESS_POLICY,
    AnalyticsFreshnessPolicy,
)
from ai_youtube_agent.providers.analytics import (
    AnalyticsErrorCode,
    AnalyticsPage,
    AnalyticsProvider,
    AnalyticsProviderError,
    AnalyticsQuery,
)
from ai_youtube_agent.providers.youtube_oauth import (
    OAuthScope,
    OAuthToken,
    YouTubeOAuthError,
)

Clock = Callable[[], datetime]


class AnalyticsCacheOutcome(StrEnum):
    HIT = "hit"
    MISS = "miss"
    REFRESH = "refresh"
    STALE_FALLBACK = "stale_fallback"


@dataclass(frozen=True)
class AnalyticsCacheInfo:
    outcome: AnalyticsCacheOutcome
    cached_at: datetime
    stale: bool
    is_complete: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "cached_at": self.cached_at.isoformat(),
            "stale": self.stale,
            "is_complete": self.is_complete,
        }


@dataclass(frozen=True)
class AnalyticsCacheStats:
    entries: int
    hits: int
    misses: int
    refreshes: int
    stale_fallbacks: int
    invalidations: int

    def as_dict(self) -> dict[str, int]:
        return {
            "entries": self.entries,
            "hits": self.hits,
            "misses": self.misses,
            "refreshes": self.refreshes,
            "stale_fallbacks": self.stale_fallbacks,
            "invalidations": self.invalidations,
        }


@dataclass(frozen=True)
class _CacheEntry:
    query: AnalyticsQuery
    page: AnalyticsPage
    cached_at: datetime
    is_complete: bool


def analytics_cache_key(query: AnalyticsQuery) -> str:
    """Deterministic key for one ``AnalyticsQuery`` page."""
    if not isinstance(query, AnalyticsQuery):
        raise TypeError("query must be an AnalyticsQuery")
    return json.dumps(
        [
            query.youtube_channel_id,
            query.scope.value,
            query.period_start.isoformat(),
            query.period_end.isoformat(),
            list(query.metrics),
            list(query.video_ids),
            query.page_size,
            query.page_token or "",
        ],
        separators=(",", ":"),
    )


class AnalyticsCache:
    def __init__(
        self,
        provider: AnalyticsProvider,
        *,
        policy: AnalyticsFreshnessPolicy = DEFAULT_FRESHNESS_POLICY,
        allow_stale_fallback: bool = True,
        clock: Clock | None = None,
    ) -> None:
        if not isinstance(provider, AnalyticsProvider):
            raise TypeError("provider must be an AnalyticsProvider")
        if not isinstance(policy, AnalyticsFreshnessPolicy):
            raise TypeError("policy must be an AnalyticsFreshnessPolicy")
        self.inner = provider
        self._policy = policy
        self._allow_stale_fallback = bool(allow_stale_fallback)
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, _CacheEntry] = {}
        self._hits = 0
        self._misses = 0
        self._refreshes = 0
        self._stale_fallbacks = 0
        self._invalidations = 0

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def policy(self) -> AnalyticsFreshnessPolicy:
        return self._policy

    def query(
        self, query: AnalyticsQuery, *, token: OAuthToken | None = None
    ) -> AnalyticsPage:
        page, _ = self.query_with_info(query, token=token)
        return page

    def query_with_info(
        self, query: AnalyticsQuery, *, token: OAuthToken | None = None
    ) -> tuple[AnalyticsPage, AnalyticsCacheInfo]:
        if not isinstance(query, AnalyticsQuery):
            raise TypeError("query must be an AnalyticsQuery")
        now = self._now()
        self._verify_token(token, now)

        key = analytics_cache_key(query)
        with self._lock:
            existing = self._entries.get(key)
            if existing is not None and not self._is_expired(existing, now):
                self._hits += 1
                return (
                    existing.page,
                    AnalyticsCacheInfo(
                        outcome=AnalyticsCacheOutcome.HIT,
                        cached_at=existing.cached_at,
                        stale=False,
                        is_complete=existing.is_complete,
                    ),
                )

        try:
            fresh_page = self.inner.query(query, token=token)
        except AnalyticsProviderError as error:
            if (
                existing is not None
                and self._allow_stale_fallback
                and error.retryable
            ):
                with self._lock:
                    # Only fall back if the entry was not invalidated while calling inner.
                    current = self._entries.get(key)
                    if current is not None:
                        self._stale_fallbacks += 1
                        return (
                            current.page,
                            AnalyticsCacheInfo(
                                outcome=AnalyticsCacheOutcome.STALE_FALLBACK,
                                cached_at=current.cached_at,
                                stale=True,
                                is_complete=current.is_complete,
                            ),
                        )
            raise

        is_complete = (
            fresh_page.data_through is not None
            and fresh_page.data_through >= fresh_page.period_end
        )
        entry = _CacheEntry(
            query=query,
            page=fresh_page,
            cached_at=now,
            is_complete=is_complete,
        )
        with self._lock:
            self._entries[key] = entry
            if existing is None:
                self._misses += 1
                outcome = AnalyticsCacheOutcome.MISS
            else:
                self._refreshes += 1
                outcome = AnalyticsCacheOutcome.REFRESH
        return (
            fresh_page,
            AnalyticsCacheInfo(
                outcome=outcome,
                cached_at=now,
                stale=False,
                is_complete=is_complete,
            ),
        )

    def check(self) -> None:
        self.inner.check()

    def invalidate_channel(self, youtube_channel_id: str) -> int:
        """Remove all cached analytics entries for a YouTube channel."""
        with self._lock:
            keys = [
                key
                for key, entry in self._entries.items()
                if entry.query.youtube_channel_id == youtube_channel_id
            ]
            for key in keys:
                del self._entries[key]
            self._invalidations += len(keys)
            return len(keys)

    def invalidate_video(self, youtube_channel_id: str, video_id: str) -> int:
        """Remove cached video-scope entries that include ``video_id``."""
        with self._lock:
            keys = [
                key
                for key, entry in self._entries.items()
                if entry.query.youtube_channel_id == youtube_channel_id
                and entry.query.scope is MetricScope.VIDEO
                and video_id in entry.query.video_ids
            ]
            for key in keys:
                del self._entries[key]
            self._invalidations += len(keys)
            return len(keys)

    def invalidate_period(
        self,
        youtube_channel_id: str,
        *,
        period_start: datetime,
        period_end: datetime,
    ) -> int:
        """Remove cached entries for a channel whose period overlaps ``[period_start, period_end)``."""
        if period_start >= period_end:
            raise ValueError("period_start must be earlier than period_end")
        with self._lock:
            keys = [
                key
                for key, entry in self._entries.items()
                if entry.query.youtube_channel_id == youtube_channel_id
                and entry.query.period_start < period_end
                and period_start < entry.query.period_end
            ]
            for key in keys:
                del self._entries[key]
            self._invalidations += len(keys)
            return len(keys)

    def clear(self) -> int:
        """Remove all cached entries."""
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
            self._invalidations += count
            return count

    def stats(self) -> AnalyticsCacheStats:
        with self._lock:
            return AnalyticsCacheStats(
                entries=len(self._entries),
                hits=self._hits,
                misses=self._misses,
                refreshes=self._refreshes,
                stale_fallbacks=self._stale_fallbacks,
                invalidations=self._invalidations,
            )

    def _is_expired(self, entry: _CacheEntry, now: datetime) -> bool:
        ttl = self._policy.max_age_for(
            entry.query.scope, is_complete=entry.is_complete
        )
        return now - entry.cached_at >= ttl

    def _verify_token(self, token: OAuthToken | None, now: datetime) -> None:
        if token is None:
            return
        try:
            token.require_valid(
                now,
                required_scopes=(OAuthScope.YT_ANALYTICS_READONLY,),
                provider=self.name,
            )
        except YouTubeOAuthError as error:
            raise AnalyticsProviderError(
                AnalyticsErrorCode.UNAUTHORIZED,
                f"analytics token check failed: {error.code}",
                provider=self.name,
                cause=error,
            ) from error

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)
