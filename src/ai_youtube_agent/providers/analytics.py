"""Analytics provider interface (Prompt Pack v8, prompt #158), context C13 Analytics.

Defines how the agent reads channel and video metrics from an analytics source
(the YouTube Analytics API in production, a seeded mock in tests), without ever
inventing a number:

- ``AnalyticsQuery``: one page request. The channel (``UC`` + 22 characters), the
  ``MetricScope`` (channel or video), the video ids (1 to 50, VIDEO scope only), a
  half-open UTC period of at most 366 days, 1 to 10 distinct metric names from
  ``KNOWN_METRICS`` (a name outside the catalogue is refused, so a typo can never
  become a silent gap) and a page size and opaque page token.
- ``AnalyticsRow``: the metrics the source *has* for one subject, as finite
  ``Decimal`` values. A metric the source does not have is absent from the row,
  never 0 (#164, #165). A subject with no data has no row.
- ``AnalyticsPage``: the rows, the token of the next page (``None`` on the last),
  the provider name as ``source``, the period asked, ``retrieved_at`` and
  ``data_through``, the end of the period for which the source says its data is
  complete (YouTube data lags by days), so freshness can be judged later (#167).
- ``AnalyticsErrorCode`` and ``AnalyticsProviderError``: typed failures; unavailable,
  timeout and rate limited are retryable.
- ``AnalyticsProvider``: a synchronous ``@runtime_checkable`` Protocol (``name``,
  ``query``, ``check``). Reading analytics needs the read-only analytics scope
  (``OAuthScope.YT_ANALYTICS_READONLY``); monetary scopes are not used here.

The mock lives in ``providers/mock_analytics.py``; storing and syncing snapshots is
#159 and #160.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final, Protocol, runtime_checkable

from ai_youtube_agent.content.analytics import (
    METRIC_NAME_PATTERN,
    SOURCE_PATTERN,
    MetricScope,
)
from ai_youtube_agent.core.errors import ProviderError
from ai_youtube_agent.pipeline.publish import VIDEO_ID_PATTERN
from ai_youtube_agent.providers.youtube_channel import YOUTUBE_CHANNEL_ID_PATTERN
from ai_youtube_agent.providers.youtube_oauth import OAuthToken

MAX_VIDEO_IDS: Final[int] = 50
MAX_METRICS: Final[int] = 10
MAX_PERIOD: Final[timedelta] = timedelta(days=366)
MAX_PAGE_SIZE: Final[int] = 200
DEFAULT_PAGE_SIZE: Final[int] = 50
MAX_PAGE_TOKEN_LENGTH: Final[int] = 200

KNOWN_METRICS: Final[frozenset[str]] = frozenset(
    {
        "views",
        "estimated_minutes_watched",
        "average_view_duration",
        "average_view_percentage",
        "subscribers_gained",
        "subscribers_lost",
        "likes",
        "comments",
        "shares",
        "impressions",
        "impressions_click_through_rate",
    }
)
"""The metric names the agent can ask for. Monetary metrics belong to Phase O."""

_PAGE_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_.:=-]+")


class AnalyticsErrorCode(StrEnum):
    UNAVAILABLE = "analytics.unavailable"
    TIMEOUT = "analytics.timeout"
    RATE_LIMITED = "analytics.rate_limited"
    QUOTA_EXCEEDED = "analytics.quota_exceeded"
    UNAUTHORIZED = "analytics.unauthorized"
    NOT_FOUND = "analytics.not_found"
    INVALID_QUERY = "analytics.invalid_query"


RETRYABLE_ANALYTICS_CODES: Final[frozenset[AnalyticsErrorCode]] = frozenset(
    {
        AnalyticsErrorCode.UNAVAILABLE,
        AnalyticsErrorCode.TIMEOUT,
        AnalyticsErrorCode.RATE_LIMITED,
    }
)

USER_MESSAGES: Final[dict[AnalyticsErrorCode, str]] = {
    AnalyticsErrorCode.UNAVAILABLE: "The analytics service is temporarily unavailable.",
    AnalyticsErrorCode.TIMEOUT: "The analytics request timed out.",
    AnalyticsErrorCode.RATE_LIMITED: "Analytics requests are currently rate-limited.",
    AnalyticsErrorCode.QUOTA_EXCEEDED: "The analytics API quota has been exceeded.",
    AnalyticsErrorCode.UNAUTHORIZED: (
        "Analytics could not be read with the current YouTube authorization."
    ),
    AnalyticsErrorCode.NOT_FOUND: "The requested channel has no analytics data source.",
    AnalyticsErrorCode.INVALID_QUERY: "The analytics request was invalid.",
}


class AnalyticsProviderError(ProviderError):
    default_code = AnalyticsErrorCode.UNAVAILABLE.value
    default_user_message = USER_MESSAGES[AnalyticsErrorCode.UNAVAILABLE]

    def __init__(
        self,
        code: AnalyticsErrorCode,
        detail: str,
        *,
        provider: str = "analytics",
        cause: Exception | None = None,
    ) -> None:
        if not isinstance(code, AnalyticsErrorCode):
            raise TypeError("code must be an AnalyticsErrorCode")
        self.analytics_code = code
        super().__init__(
            detail,
            provider=provider,
            code=code.value,
            user_message=USER_MESSAGES[code],
            retryable=code in RETRYABLE_ANALYTICS_CODES,
        )
        self.__cause__ = cause


def _require_utc(moment: object, name: str) -> None:
    if not isinstance(moment, datetime) or moment.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")


@dataclass(frozen=True)
class AnalyticsQuery:
    youtube_channel_id: str
    scope: MetricScope
    period_start: datetime
    period_end: datetime
    metrics: tuple[str, ...]
    video_ids: tuple[str, ...] = ()
    page_size: int = DEFAULT_PAGE_SIZE
    page_token: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.youtube_channel_id, str) or not (
            YOUTUBE_CHANNEL_ID_PATTERN.match(self.youtube_channel_id)
        ):
            raise ValueError("youtube_channel_id must be 'UC' followed by 22 characters")
        if not isinstance(self.scope, MetricScope):
            raise TypeError("scope must be a MetricScope")
        _require_utc(self.period_start, "period_start")
        _require_utc(self.period_end, "period_end")
        if self.period_start >= self.period_end:
            raise ValueError("period_start must be earlier than period_end")
        if self.period_end - self.period_start > MAX_PERIOD:
            raise ValueError("the period must not be longer than 366 days")
        if not isinstance(self.metrics, tuple) or not 1 <= len(self.metrics) <= MAX_METRICS:
            raise ValueError(f"metrics must be a tuple of 1 to {MAX_METRICS} names")
        if len(set(self.metrics)) != len(self.metrics):
            raise ValueError("metrics must not repeat")
        for name in self.metrics:
            if not isinstance(name, str) or not METRIC_NAME_PATTERN.match(name):
                raise ValueError(f"metric name {name!r} must be a lowercase name")
            if name not in KNOWN_METRICS:
                raise ValueError(f"metric {name!r} is not a known analytics metric")
        if not isinstance(self.video_ids, tuple):
            raise TypeError("video_ids must be a tuple")
        if self.scope is MetricScope.CHANNEL:
            if self.video_ids:
                raise ValueError("a channel query takes no video ids")
        else:
            if not 1 <= len(self.video_ids) <= MAX_VIDEO_IDS:
                raise ValueError(f"a video query needs 1 to {MAX_VIDEO_IDS} video ids")
            if len(set(self.video_ids)) != len(self.video_ids):
                raise ValueError("video_ids must not repeat")
            for video_id in self.video_ids:
                if not isinstance(video_id, str) or not VIDEO_ID_PATTERN.match(video_id):
                    raise ValueError(f"{video_id!r} is not a YouTube video id")
        if (
            isinstance(self.page_size, bool)
            or not isinstance(self.page_size, int)
            or not 1 <= self.page_size <= MAX_PAGE_SIZE
        ):
            raise ValueError(f"page_size must be a whole number from 1 to {MAX_PAGE_SIZE}")
        if self.page_token is not None and (
            not isinstance(self.page_token, str)
            or not 1 <= len(self.page_token) <= MAX_PAGE_TOKEN_LENGTH
            or not _PAGE_TOKEN_PATTERN.fullmatch(self.page_token)
        ):
            raise ValueError("page_token must be a short opaque token")


@dataclass(frozen=True)
class AnalyticsRow:
    subject_id: str
    metrics: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        if not isinstance(self.subject_id, str) or not self.subject_id.strip():
            raise ValueError("subject_id must not be empty")
        if not self.metrics:
            raise ValueError("a row needs at least one metric; no data means no row")
        for name, value in self.metrics.items():
            if not isinstance(name, str) or name not in KNOWN_METRICS:
                raise ValueError(f"metric {name!r} is not a known analytics metric")
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"metric {name!r} must be a finite Decimal")
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject_id": self.subject_id,
            "metrics": {name: str(value) for name, value in self.metrics.items()},
        }


@dataclass(frozen=True)
class AnalyticsPage:
    rows: tuple[AnalyticsRow, ...]
    next_page_token: str | None
    source: str
    period_start: datetime
    period_end: datetime
    retrieved_at: datetime
    data_through: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.rows, tuple) or any(
            not isinstance(row, AnalyticsRow) for row in self.rows
        ):
            raise TypeError("rows must be a tuple of AnalyticsRow values")
        ids = [row.subject_id for row in self.rows]
        if len(set(ids)) != len(ids):
            raise ValueError("a page must not hold two rows for one subject")
        if not isinstance(self.source, str) or not SOURCE_PATTERN.match(self.source):
            raise ValueError("source must be a lowercase provider name")
        for name in ("period_start", "period_end", "retrieved_at"):
            _require_utc(getattr(self, name), name)
        if self.period_start >= self.period_end:
            raise ValueError("period_start must be earlier than period_end")
        if self.data_through is not None:
            _require_utc(self.data_through, "data_through")
            if self.data_through < self.period_start:
                raise ValueError("data_through must not be earlier than period_start")
        if self.next_page_token is not None and not self.next_page_token.strip():
            raise ValueError("next_page_token must not be blank")

    @property
    def is_last(self) -> bool:
        return self.next_page_token is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows": [row.as_dict() for row in self.rows],
            "next_page_token": self.next_page_token,
            "source": self.source,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "retrieved_at": self.retrieved_at.isoformat(),
            "data_through": self.data_through.isoformat() if self.data_through else None,
        }


@runtime_checkable
class AnalyticsProvider(Protocol):
    @property
    def name(self) -> str: ...

    def query(
        self, query: AnalyticsQuery, *, token: OAuthToken | None = None
    ) -> AnalyticsPage: ...

    def check(self) -> None: ...
