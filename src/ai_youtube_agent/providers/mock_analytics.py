"""Seeded in-memory analytics provider (Prompt Pack v8, prompt #158), context C13.

``MockAnalyticsProvider`` is the only analytics source until a real one exists. It
never connects anywhere and **never invents a number**: it returns exactly the values a
test or a local run seeded with ``set_channel_metrics`` / ``set_video_metrics``.

- A metric that was not seeded is absent from the row (never 0), so retention and
  click-through rate stay absent unless someone seeds them (#164, #165).
- A video with none of the requested metrics has no row. An unknown channel is
  ``NOT_FOUND``.
- Video queries page over the requested ``video_ids`` in the order given; the page
  token is the opaque offset (``o25``). A page may hold fewer rows than ``page_size``
  (or none) when some videos have no data, and the next token is still given.
- The values do not depend on the period: the mock has no time dimension, it only
  echoes the period asked. ``data_through`` is the end of the period capped at
  ``now - data_lag`` (default 2 days), or ``None`` when no day of the period is
  complete yet, which lets freshness logic be tested.
- ``fail_next(code, times)``, ``set_healthy`` and ``calls`` support failure tests.
"""

import re
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ai_youtube_agent.content.analytics import MetricScope
from ai_youtube_agent.providers.analytics import (
    KNOWN_METRICS,
    AnalyticsErrorCode,
    AnalyticsPage,
    AnalyticsProviderError,
    AnalyticsQuery,
    AnalyticsRow,
)
from ai_youtube_agent.providers.youtube_oauth import (
    OAuthScope,
    OAuthToken,
    YouTubeOAuthError,
)

Clock = Callable[[], datetime]
_OFFSET_TOKEN = re.compile(r"o(\d{1,9})")


class MockAnalyticsProvider:
    def __init__(
        self,
        *,
        name: str = "mock_analytics",
        data_lag: timedelta = timedelta(days=2),
        clock: Clock | None = None,
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name must not be empty")
        if not isinstance(data_lag, timedelta) or data_lag < timedelta(0):
            raise ValueError("data_lag must be a non-negative timedelta")
        self._name = name.strip()
        self._data_lag = data_lag
        self._clock = clock
        self._lock = threading.Lock()
        self._channels: dict[str, dict[str, Decimal]] = {}
        self._videos: dict[tuple[str, str], dict[str, Decimal]] = {}
        self._failures: list[AnalyticsErrorCode] = []
        self._healthy = True
        self._calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def calls(self) -> int:
        with self._lock:
            return self._calls

    def set_healthy(self, healthy: bool) -> None:
        with self._lock:
            self._healthy = bool(healthy)

    def fail_next(self, code: AnalyticsErrorCode, times: int = 1) -> None:
        if not isinstance(code, AnalyticsErrorCode):
            raise TypeError("code must be an AnalyticsErrorCode")
        if isinstance(times, bool) or not isinstance(times, int) or times < 1:
            raise ValueError("times must be a whole number of 1 or more")
        with self._lock:
            self._failures.extend([code] * times)

    def set_channel_metrics(
        self, youtube_channel_id: str, metrics: Mapping[str, Decimal]
    ) -> None:
        values = _checked(metrics)
        with self._lock:
            self._channels.setdefault(youtube_channel_id, {}).update(values)

    def set_video_metrics(
        self, youtube_channel_id: str, video_id: str, metrics: Mapping[str, Decimal]
    ) -> None:
        values = _checked(metrics)
        with self._lock:
            self._channels.setdefault(youtube_channel_id, {})
            self._videos.setdefault((youtube_channel_id, video_id), {}).update(values)

    def query(
        self, query: AnalyticsQuery, *, token: OAuthToken | None = None
    ) -> AnalyticsPage:
        if not isinstance(query, AnalyticsQuery):
            raise TypeError("query must be an AnalyticsQuery")
        now = self._now()
        if token is not None:
            try:
                token.require_valid(
                    now,
                    required_scopes=(OAuthScope.YT_ANALYTICS_READONLY,),
                    provider=self._name,
                )
            except YouTubeOAuthError as error:
                raise AnalyticsProviderError(
                    AnalyticsErrorCode.UNAUTHORIZED,
                    f"analytics token check failed: {error.code}",
                    provider=self._name,
                    cause=error,
                ) from error

        with self._lock:
            self._calls += 1
            if self._failures:
                code = self._failures.pop(0)
                raise AnalyticsProviderError(
                    code, f"injected analytics failure ({code.value})", provider=self._name
                )
            if query.youtube_channel_id not in self._channels:
                raise AnalyticsProviderError(
                    AnalyticsErrorCode.NOT_FOUND,
                    f"no analytics seeded for channel {query.youtube_channel_id}",
                    provider=self._name,
                )
            if query.scope is MetricScope.CHANNEL:
                rows, next_token = self._channel_rows(query)
            else:
                rows, next_token = self._video_rows(query)

        through = min(query.period_end, now - self._data_lag)
        return AnalyticsPage(
            rows=rows,
            next_page_token=next_token,
            source=self._name,
            period_start=query.period_start,
            period_end=query.period_end,
            retrieved_at=now,
            data_through=through if through >= query.period_start else None,
        )

    def check(self) -> None:
        with self._lock:
            if not self._healthy:
                raise AnalyticsProviderError(
                    AnalyticsErrorCode.UNAVAILABLE,
                    "mock analytics provider marked unhealthy",
                    provider=self._name,
                )

    def _channel_rows(
        self, query: AnalyticsQuery
    ) -> tuple[tuple[AnalyticsRow, ...], str | None]:
        if query.page_token is not None:
            raise AnalyticsProviderError(
                AnalyticsErrorCode.INVALID_QUERY,
                "a channel query has a single page and takes no page token",
                provider=self._name,
            )
        row = _row(
            query.youtube_channel_id,
            self._channels[query.youtube_channel_id],
            query.metrics,
        )
        return ((row,) if row else ()), None

    def _video_rows(
        self, query: AnalyticsQuery
    ) -> tuple[tuple[AnalyticsRow, ...], str | None]:
        offset = 0
        if query.page_token is not None:
            match = _OFFSET_TOKEN.fullmatch(query.page_token)
            if match is None or int(match.group(1)) >= len(query.video_ids):
                raise AnalyticsProviderError(
                    AnalyticsErrorCode.INVALID_QUERY,
                    "the page token does not belong to this query",
                    provider=self._name,
                )
            offset = int(match.group(1))
        window = query.video_ids[offset : offset + query.page_size]
        rows = []
        for video_id in window:
            values = self._videos.get((query.youtube_channel_id, video_id))
            row = _row(video_id, values, query.metrics) if values else None
            if row is not None:
                rows.append(row)
        end = offset + query.page_size
        next_token = f"o{end}" if end < len(query.video_ids) else None
        return tuple(rows), next_token

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)


def _checked(metrics: Mapping[str, Decimal]) -> dict[str, Decimal]:
    values: dict[str, Decimal] = {}
    for name, value in metrics.items():
        if name not in KNOWN_METRICS:
            raise ValueError(f"metric {name!r} is not a known analytics metric")
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError(f"metric {name!r} must be a finite Decimal")
        values[name] = value
    return values


def _row(
    subject_id: str, values: Mapping[str, Decimal] | None, requested: tuple[str, ...]
) -> AnalyticsRow | None:
    if not values:
        return None
    present = {name: values[name] for name in requested if name in values}
    return AnalyticsRow(subject_id, present) if present else None
