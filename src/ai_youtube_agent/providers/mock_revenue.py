"""Seeded in-memory revenue provider (Prompt Pack v8, prompt #170), context C14.

``MockRevenueProvider`` is the deterministic revenue source for tests and local runs:

- It never connects anywhere and **never invents or converts revenue**: ``ESTIMATED``
  and ``FINAL`` figures are seeded separately with ``set_channel_revenue`` and
  ``set_video_revenue``, and querying one stage never returns figures seeded for the
  other (#024, #171, #172).
- Querying ``RevenueStage.FINAL`` before ``query.period_end`` raises
  ``RevenueProviderError(RevenueErrorCode.INVALID_QUERY, ...)``.
- When a token is supplied, it must carry
  ``OAuthScope.YT_ANALYTICS_MONETARY_READONLY``, else ``UNAUTHORIZED`` is raised.
- Video revenue queries page over ``query.video_ids`` in order with offset tokens
  (``o25``).
- ``fail_next(code, times)``, ``set_healthy`` and ``calls`` support failure testing.
"""

import re
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ai_youtube_agent.content.analytics import MetricScope
from ai_youtube_agent.content.revenue import RevenueStage
from ai_youtube_agent.content.strategy import CURRENCY_PATTERN
from ai_youtube_agent.providers.revenue import (
    KNOWN_REVENUE_TYPES,
    RevenueErrorCode,
    RevenuePage,
    RevenueProviderError,
    RevenueQuery,
    RevenueRow,
)
from ai_youtube_agent.providers.youtube_oauth import (
    OAuthScope,
    OAuthToken,
    YouTubeOAuthError,
)

Clock = Callable[[], datetime]
_OFFSET_TOKEN = re.compile(r"o(\d{1,9})")


class MockRevenueProvider:
    def __init__(
        self,
        *,
        name: str = "mock_revenue",
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
        self._known_channels: set[str] = set()
        self._channels: dict[
            tuple[str, RevenueStage], dict[str, tuple[Decimal, str]]
        ] = {}
        self._videos: dict[
            tuple[str, str, RevenueStage], dict[str, tuple[Decimal, str]]
        ] = {}
        self._failures: list[RevenueErrorCode] = []
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

    def fail_next(self, code: RevenueErrorCode, times: int = 1) -> None:
        if not isinstance(code, RevenueErrorCode):
            raise TypeError("code must be a RevenueErrorCode")
        if isinstance(times, bool) or not isinstance(times, int) or times < 1:
            raise ValueError("times must be a whole number of 1 or more")
        with self._lock:
            self._failures.extend([code] * times)

    def set_channel_revenue(
        self,
        youtube_channel_id: str,
        *,
        stage: RevenueStage,
        amounts: Mapping[str, Decimal],
        currency: str = "USD",
    ) -> None:
        if not isinstance(stage, RevenueStage):
            raise TypeError("stage must be a RevenueStage")
        checked = _checked(amounts, currency)
        with self._lock:
            self._known_channels.add(youtube_channel_id)
            self._channels.setdefault((youtube_channel_id, stage), {}).update(checked)

    def set_video_revenue(
        self,
        youtube_channel_id: str,
        video_id: str,
        *,
        stage: RevenueStage,
        amounts: Mapping[str, Decimal],
        currency: str = "USD",
    ) -> None:
        if not isinstance(stage, RevenueStage):
            raise TypeError("stage must be a RevenueStage")
        checked = _checked(amounts, currency)
        with self._lock:
            self._known_channels.add(youtube_channel_id)
            self._videos.setdefault((youtube_channel_id, video_id, stage), {}).update(
                checked
            )

    def query(
        self, query: RevenueQuery, *, token: OAuthToken | None = None
    ) -> RevenuePage:
        if not isinstance(query, RevenueQuery):
            raise TypeError("query must be a RevenueQuery")
        now = self._now()
        if token is not None:
            try:
                token.require_valid(
                    now,
                    required_scopes=(OAuthScope.YT_ANALYTICS_MONETARY_READONLY,),
                    provider=self._name,
                )
            except YouTubeOAuthError as error:
                raise RevenueProviderError(
                    RevenueErrorCode.UNAUTHORIZED,
                    f"revenue token check failed: {error.code}",
                    provider=self._name,
                    cause=error,
                ) from error

        if query.stage is RevenueStage.FINAL and now < query.period_end:
            raise RevenueProviderError(
                RevenueErrorCode.INVALID_QUERY,
                "final revenue can only be queried after period_end",
                provider=self._name,
            )

        with self._lock:
            self._calls += 1
            if self._failures:
                code = self._failures.pop(0)
                raise RevenueProviderError(
                    code,
                    f"injected revenue failure ({code.value})",
                    provider=self._name,
                )
            if query.youtube_channel_id not in self._known_channels:
                raise RevenueProviderError(
                    RevenueErrorCode.NOT_FOUND,
                    f"no revenue seeded for channel {query.youtube_channel_id}",
                    provider=self._name,
                )
            if query.scope is MetricScope.CHANNEL:
                rows, next_token = self._channel_rows(query)
            else:
                rows, next_token = self._video_rows(query)

        through = min(query.period_end, now - self._data_lag)
        return RevenuePage(
            rows=rows,
            stage=query.stage,
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
                raise RevenueProviderError(
                    RevenueErrorCode.UNAVAILABLE,
                    "mock revenue provider marked unhealthy",
                    provider=self._name,
                )

    def _channel_rows(
        self, query: RevenueQuery
    ) -> tuple[tuple[RevenueRow, ...], str | None]:
        if query.page_token is not None:
            raise RevenueProviderError(
                RevenueErrorCode.INVALID_QUERY,
                "a channel revenue query has a single page and takes no page token",
                provider=self._name,
            )
        seeded = self._channels.get((query.youtube_channel_id, query.stage), {})
        rows = _rows_for_subject(
            query.youtube_channel_id,
            query.stage,
            seeded,
            query.revenue_types,
        )
        return rows, None

    def _video_rows(
        self, query: RevenueQuery
    ) -> tuple[tuple[RevenueRow, ...], str | None]:
        offset = 0
        if query.page_token is not None:
            match = _OFFSET_TOKEN.fullmatch(query.page_token)
            if match is None or int(match.group(1)) >= len(query.video_ids):
                raise RevenueProviderError(
                    RevenueErrorCode.INVALID_QUERY,
                    "the page token does not belong to this revenue query",
                    provider=self._name,
                )
            offset = int(match.group(1))
        window = query.video_ids[offset : offset + query.page_size]
        rows: list[RevenueRow] = []
        for video_id in window:
            seeded = self._videos.get(
                (query.youtube_channel_id, video_id, query.stage), {}
            )
            rows.extend(
                _rows_for_subject(video_id, query.stage, seeded, query.revenue_types)
            )
        end = offset + query.page_size
        next_token = f"o{end}" if end < len(query.video_ids) else None
        return tuple(rows), next_token

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)


def _checked(
    amounts: Mapping[str, Decimal], currency: str
) -> dict[str, tuple[Decimal, str]]:
    if not isinstance(currency, str) or not CURRENCY_PATTERN.match(currency):
        raise ValueError(
            f"currency {currency!r} must be an ISO 4217 code such as 'USD'"
        )
    values: dict[str, tuple[Decimal, str]] = {}
    for rtype, amount in amounts.items():
        if rtype not in KNOWN_REVENUE_TYPES:
            raise ValueError(f"revenue_type {rtype!r} is not a known revenue source")
        if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
            raise ValueError(
                f"revenue amount for {rtype!r} must be a finite Decimal of 0 or more"
            )
        values[rtype] = (amount, currency)
    return values


def _rows_for_subject(
    subject_id: str,
    stage: RevenueStage,
    seeded: Mapping[str, tuple[Decimal, str]],
    requested_types: tuple[str, ...],
) -> tuple[RevenueRow, ...]:
    rows: list[RevenueRow] = []
    for rtype in requested_types:
        if rtype in seeded:
            amount, currency = seeded[rtype]
            rows.append(
                RevenueRow(
                    subject_id=subject_id,
                    stage=stage,
                    revenue_type=rtype,
                    amount=amount,
                    currency=currency,
                )
            )
    return tuple(rows)
