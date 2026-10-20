"""Revenue provider interface (Prompt Pack v8, prompt #170), context C14 Economics.

Defines how the agent reads estimated and final revenue figures from a revenue source
(YouTube Analytics Monetary API or external revenue integrations in production, a
seeded mock in tests), without ever inventing a figure or converting estimated into
final (#024, #171, #172):

- ``RevenueQuery``: one page request for a YouTube channel, specifying ``RevenueStage``
  (``ESTIMATED`` or ``FINAL``, never mixed), ``MetricScope`` (channel or video), a
  half-open UTC period of at most 366 days, 1 to 8 distinct ``revenue_types`` from
  ``KNOWN_REVENUE_TYPES`` (#052, #173), ``video_ids`` (1 to 50 for video scope),
  ``page_size`` and an opaque ``page_token``.
- ``RevenueRow``: one non-negative finite ``Decimal`` amount in an ISO 4217
  ``currency`` for one ``(subject_id, stage, revenue_type)``. A stream with no
  recorded revenue from the source has no row.
- ``RevenuePage``: the rows, ``stage``, ``next_page_token``, provider ``source``,
  period, ``retrieved_at`` (for ``FINAL`` stage, ``retrieved_at`` must not be earlier
  than ``period_end``), and optional ``data_through`` for freshness.
- ``RevenueErrorCode`` and ``RevenueProviderError``: typed provider errors;
  unavailable, timeout and rate_limited are retryable.
- ``RevenueProvider``: synchronous ``@runtime_checkable`` Protocol (``name``,
  ``query``, ``check``). Reading YouTube monetary data requires
  ``OAuthScope.YT_ANALYTICS_MONETARY_READONLY``.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

from ai_youtube_agent.content.analytics import SOURCE_PATTERN, MetricScope
from ai_youtube_agent.content.revenue import RevenueStage
from ai_youtube_agent.content.strategy import CURRENCY_PATTERN, RevenueSource
from ai_youtube_agent.core.errors import ProviderError
from ai_youtube_agent.pipeline.publish import VIDEO_ID_PATTERN
from ai_youtube_agent.providers.youtube_channel import YOUTUBE_CHANNEL_ID_PATTERN
from ai_youtube_agent.providers.youtube_oauth import OAuthToken

MAX_VIDEO_IDS: Final[int] = 50
MAX_REVENUE_TYPES: Final[int] = len(RevenueSource)
MAX_PERIOD: Final[timedelta] = timedelta(days=366)
MAX_PAGE_SIZE: Final[int] = 200
DEFAULT_PAGE_SIZE: Final[int] = 50
MAX_PAGE_TOKEN_LENGTH: Final[int] = 200

KNOWN_REVENUE_TYPES: Final[frozenset[str]] = frozenset(
    source.value for source in RevenueSource
)
DEFAULT_REVENUE_TYPES: Final[tuple[str, ...]] = (RevenueSource.ADS.value,)

_PAGE_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_.:=-]+")


class RevenueErrorCode(StrEnum):
    UNAVAILABLE = "revenue.unavailable"
    TIMEOUT = "revenue.timeout"
    RATE_LIMITED = "revenue.rate_limited"
    QUOTA_EXCEEDED = "revenue.quota_exceeded"
    UNAUTHORIZED = "revenue.unauthorized"
    NOT_FOUND = "revenue.not_found"
    INVALID_QUERY = "revenue.invalid_query"


RETRYABLE_REVENUE_CODES: Final[frozenset[RevenueErrorCode]] = frozenset(
    {
        RevenueErrorCode.UNAVAILABLE,
        RevenueErrorCode.TIMEOUT,
        RevenueErrorCode.RATE_LIMITED,
    }
)

USER_MESSAGES: Final[dict[RevenueErrorCode, str]] = {
    RevenueErrorCode.UNAVAILABLE: "The revenue data service is temporarily unavailable.",
    RevenueErrorCode.TIMEOUT: "The revenue data request timed out.",
    RevenueErrorCode.RATE_LIMITED: "Revenue data requests are currently rate-limited.",
    RevenueErrorCode.QUOTA_EXCEEDED: "The revenue API quota has been exceeded.",
    RevenueErrorCode.UNAUTHORIZED: (
        "Revenue data could not be read with the current YouTube monetary authorization."
    ),
    RevenueErrorCode.NOT_FOUND: "The requested channel has no revenue data source.",
    RevenueErrorCode.INVALID_QUERY: "The revenue query was invalid.",
}


class RevenueProviderError(ProviderError):
    default_code = RevenueErrorCode.UNAVAILABLE.value
    default_user_message = USER_MESSAGES[RevenueErrorCode.UNAVAILABLE]

    def __init__(
        self,
        code: RevenueErrorCode,
        detail: str,
        *,
        provider: str = "revenue",
        cause: Exception | None = None,
    ) -> None:
        if not isinstance(code, RevenueErrorCode):
            raise TypeError("code must be a RevenueErrorCode")
        self.revenue_code = code
        super().__init__(
            detail,
            provider=provider,
            code=code.value,
            user_message=USER_MESSAGES[code],
            retryable=code in RETRYABLE_REVENUE_CODES,
        )
        self.__cause__ = cause


def _require_utc(moment: object, name: str) -> None:
    if not isinstance(moment, datetime) or moment.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")


@dataclass(frozen=True)
class RevenueQuery:
    youtube_channel_id: str
    stage: RevenueStage
    scope: MetricScope
    period_start: datetime
    period_end: datetime
    revenue_types: tuple[str, ...] = DEFAULT_REVENUE_TYPES
    video_ids: tuple[str, ...] = ()
    page_size: int = DEFAULT_PAGE_SIZE
    page_token: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.youtube_channel_id, str) or not (
            YOUTUBE_CHANNEL_ID_PATTERN.match(self.youtube_channel_id)
        ):
            raise ValueError(
                "youtube_channel_id must be 'UC' followed by 22 characters"
            )
        if not isinstance(self.stage, RevenueStage):
            raise TypeError("stage must be a RevenueStage")
        if not isinstance(self.scope, MetricScope):
            raise TypeError("scope must be a MetricScope")
        _require_utc(self.period_start, "period_start")
        _require_utc(self.period_end, "period_end")
        if self.period_start >= self.period_end:
            raise ValueError("period_start must be earlier than period_end")
        if self.period_end - self.period_start > MAX_PERIOD:
            raise ValueError("the period must not be longer than 366 days")
        if (
            not isinstance(self.revenue_types, tuple)
            or not 1 <= len(self.revenue_types) <= MAX_REVENUE_TYPES
        ):
            raise ValueError(
                f"revenue_types must be a tuple of 1 to {MAX_REVENUE_TYPES} names"
            )
        if len(set(self.revenue_types)) != len(self.revenue_types):
            raise ValueError("revenue_types must not repeat")
        for rtype in self.revenue_types:
            if not isinstance(rtype, str) or rtype not in KNOWN_REVENUE_TYPES:
                raise ValueError(f"revenue_type {rtype!r} is not a known revenue source")
        if not isinstance(self.video_ids, tuple):
            raise TypeError("video_ids must be a tuple")
        if self.scope is MetricScope.CHANNEL:
            if self.video_ids:
                raise ValueError("a channel revenue query takes no video ids")
        else:
            if not 1 <= len(self.video_ids) <= MAX_VIDEO_IDS:
                raise ValueError(
                    f"a video revenue query needs 1 to {MAX_VIDEO_IDS} video ids"
                )
            if len(set(self.video_ids)) != len(self.video_ids):
                raise ValueError("video_ids must not repeat")
            for video_id in self.video_ids:
                if not isinstance(video_id, str) or not VIDEO_ID_PATTERN.match(
                    video_id
                ):
                    raise ValueError(f"{video_id!r} is not a YouTube video id")
        if (
            isinstance(self.page_size, bool)
            or not isinstance(self.page_size, int)
            or not 1 <= self.page_size <= MAX_PAGE_SIZE
        ):
            raise ValueError(
                f"page_size must be a whole number from 1 to {MAX_PAGE_SIZE}"
            )
        if self.page_token is not None and (
            not isinstance(self.page_token, str)
            or not 1 <= len(self.page_token) <= MAX_PAGE_TOKEN_LENGTH
            or not _PAGE_TOKEN_PATTERN.fullmatch(self.page_token)
        ):
            raise ValueError("page_token must be a short opaque token")


@dataclass(frozen=True)
class RevenueRow:
    subject_id: str
    stage: RevenueStage
    revenue_type: str
    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.subject_id, str) or not self.subject_id.strip():
            raise ValueError("subject_id must not be empty")
        if not isinstance(self.stage, RevenueStage):
            raise TypeError("stage must be a RevenueStage")
        if (
            not isinstance(self.revenue_type, str)
            or self.revenue_type not in KNOWN_REVENUE_TYPES
        ):
            raise ValueError(
                f"revenue_type {self.revenue_type!r} is not a known revenue source"
            )
        if (
            not isinstance(self.amount, Decimal)
            or not self.amount.is_finite()
            or self.amount < 0
        ):
            raise ValueError("amount must be a finite Decimal of 0 or more")
        if not isinstance(self.currency, str) or not CURRENCY_PATTERN.match(
            self.currency
        ):
            raise ValueError(
                f"currency {self.currency!r} must be an ISO 4217 code such as 'USD'"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject_id": self.subject_id,
            "stage": self.stage.value,
            "revenue_type": self.revenue_type,
            "amount": str(self.amount),
            "currency": self.currency,
        }


@dataclass(frozen=True)
class RevenuePage:
    rows: tuple[RevenueRow, ...]
    stage: RevenueStage
    next_page_token: str | None
    source: str
    period_start: datetime
    period_end: datetime
    retrieved_at: datetime
    data_through: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.stage, RevenueStage):
            raise TypeError("stage must be a RevenueStage")
        if not isinstance(self.rows, tuple) or any(
            not isinstance(row, RevenueRow) for row in self.rows
        ):
            raise TypeError("rows must be a tuple of RevenueRow values")
        if any(row.stage is not self.stage for row in self.rows):
            raise ValueError("every row in a RevenuePage must match page.stage")
        pairs = [(row.subject_id, row.revenue_type) for row in self.rows]
        if len(set(pairs)) != len(pairs):
            raise ValueError(
                "a page must not hold two rows for one subject and revenue_type"
            )
        if not isinstance(self.source, str) or not SOURCE_PATTERN.match(self.source):
            raise ValueError("source must be a lowercase provider name")
        for name in ("period_start", "period_end", "retrieved_at"):
            _require_utc(getattr(self, name), name)
        if self.period_start >= self.period_end:
            raise ValueError("period_start must be earlier than period_end")
        if self.retrieved_at < self.period_start:
            raise ValueError("retrieved_at must not be earlier than period_start")
        if (
            self.stage is RevenueStage.FINAL
            and self.retrieved_at < self.period_end
        ):
            raise ValueError(
                "final revenue can only be retrieved after period_end"
            )
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
            "stage": self.stage.value,
            "next_page_token": self.next_page_token,
            "source": self.source,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "retrieved_at": self.retrieved_at.isoformat(),
            "data_through": (
                self.data_through.isoformat() if self.data_through else None
            ),
            "is_last": self.is_last,
        }


@runtime_checkable
class RevenueProvider(Protocol):
    @property
    def name(self) -> str: ...

    def query(
        self, query: RevenueQuery, *, token: OAuthToken | None = None
    ) -> RevenuePage: ...

    def check(self) -> None: ...
