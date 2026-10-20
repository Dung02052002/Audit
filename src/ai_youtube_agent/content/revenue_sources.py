"""Revenue sources registry and extensible revenue stream recording (Prompt Pack v8, prompt #173), context C14 Economics.

Formalises the built-in ``RevenueSource`` catalogue (#052) and supports extensible
revenue stream registration and recording across ads, memberships, fan funding,
affiliate, sponsorships, merchandise, and custom streams:

- ``RevenueStreamCategory`` classifies streams into ``youtube_ads`` (``ads``,
  ``shorts_ads``), ``youtube_fan_funding`` (``memberships``, ``super_thanks``,
  ``super_chat``), ``partner_external`` (``sponsorships``, ``affiliate``,
  ``merchandise``), and ``custom`` (extensible lowercase stream names matching
  ``REVENUE_TYPE_PATTERN``).
- ``RevenueStreamRegistry`` holds the 8 built-in ``RevenueSource`` specs and allows
  registering custom ``RevenueStreamSpec`` entries without schema changes.
- ``RevenueSourceService`` records and queries estimated or finalized revenue rows
  for any registered stream (channel or published video scope), keeping
  ``RevenueStage.ESTIMATED`` and ``RevenueStage.FINAL`` strictly separate (#171,
  #172) and optionally comparing recorded figures against ``StrategyProfile.monetization``
  targets (always marked ``is_guaranteed=False``; targets are never treated as
  earned revenue).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.analytics import SOURCE_PATTERN as DATA_SOURCE_PATTERN
from ai_youtube_agent.content.analytics import MetricScope
from ai_youtube_agent.content.channel_metrics import (
    CHANNEL_ENTITY,
    MetricsSyncError,
    MetricsSyncErrorCode,
    load_syncable_channel,
    require_ended_period,
)
from ai_youtube_agent.content.estimated_revenue import require_started_period
from ai_youtube_agent.content.revenue import (
    REVENUE_TYPE_PATTERN,
    RevenueRecord,
    RevenueStage,
)
from ai_youtube_agent.content.strategy import (
    CURRENCY_PATTERN,
    RevenueGoal,
    RevenueSource,
)
from ai_youtube_agent.core.audit import Actor, AuditLog, AuditResult, EntityRef
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.economics import RevenueRecordRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.pipeline.publish_result import PublishResultService

Clock = Callable[[], datetime]

REVENUE_SOURCE_RECORDED: Final[str] = "revenue.source_recorded"


class RevenueStreamCategory(StrEnum):
    YOUTUBE_ADS = "youtube_ads"
    YOUTUBE_FAN_FUNDING = "youtube_fan_funding"
    PARTNER_EXTERNAL = "partner_external"
    CUSTOM = "custom"


class RevenueSourceErrorCode(StrEnum):
    UNKNOWN_REVENUE_TYPE = "domain.revenue_source_unknown_type"
    DUPLICATE_REVENUE_TYPE = "domain.revenue_source_duplicate_type"
    SCOPE_NOT_SUPPORTED = "domain.revenue_source_scope_not_supported"
    STAGE_NOT_SUPPORTED = "domain.revenue_source_stage_not_supported"
    CURRENCY_MISMATCH = "domain.revenue_source_currency_mismatch"


USER_MESSAGES: Final[dict[RevenueSourceErrorCode, str]] = {
    RevenueSourceErrorCode.UNKNOWN_REVENUE_TYPE: (
        "The revenue stream type is not registered."
    ),
    RevenueSourceErrorCode.DUPLICATE_REVENUE_TYPE: (
        "A revenue stream with that name is already registered."
    ),
    RevenueSourceErrorCode.SCOPE_NOT_SUPPORTED: (
        "This revenue stream does not support the requested scope."
    ),
    RevenueSourceErrorCode.STAGE_NOT_SUPPORTED: (
        "This revenue stream does not support the requested revenue stage."
    ),
    RevenueSourceErrorCode.CURRENCY_MISMATCH: (
        "Revenue records across multiple currencies cannot be summed without conversion."
    ),
}

_STATUS: Final[dict[RevenueSourceErrorCode, HTTPStatus]] = {
    RevenueSourceErrorCode.UNKNOWN_REVENUE_TYPE: HTTPStatus.UNPROCESSABLE_ENTITY,
    RevenueSourceErrorCode.DUPLICATE_REVENUE_TYPE: HTTPStatus.CONFLICT,
    RevenueSourceErrorCode.SCOPE_NOT_SUPPORTED: HTTPStatus.UNPROCESSABLE_ENTITY,
    RevenueSourceErrorCode.STAGE_NOT_SUPPORTED: HTTPStatus.UNPROCESSABLE_ENTITY,
    RevenueSourceErrorCode.CURRENCY_MISMATCH: HTTPStatus.CONFLICT,
}


class RevenueSourceError(DomainError):
    default_code = RevenueSourceErrorCode.UNKNOWN_REVENUE_TYPE.value
    default_user_message = USER_MESSAGES[RevenueSourceErrorCode.UNKNOWN_REVENUE_TYPE]
    default_http_status = HTTPStatus.UNPROCESSABLE_ENTITY

    def __init__(self, code: RevenueSourceErrorCode, detail: str) -> None:
        if not isinstance(code, RevenueSourceErrorCode):
            raise TypeError("code must be a RevenueSourceErrorCode")
        self.source_code = code
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        self.default_http_status = _STATUS[code]


@dataclass(frozen=True)
class RevenueStreamSpec:
    revenue_type: str
    category: RevenueStreamCategory
    default_source: str
    supports_estimated: bool = True
    supports_final: bool = True
    supports_video_scope: bool = True
    builtin: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.revenue_type, str) or not REVENUE_TYPE_PATTERN.match(
            self.revenue_type
        ):
            raise ValueError(
                f"revenue_type {self.revenue_type!r} must be a lowercase name such as 'ads'"
            )
        if not isinstance(self.category, RevenueStreamCategory):
            raise TypeError("category must be a RevenueStreamCategory")
        if not isinstance(self.default_source, str) or not DATA_SOURCE_PATTERN.match(
            self.default_source
        ):
            raise ValueError(
                f"default_source {self.default_source!r} must be a lowercase source name"
            )
        for flag_name in (
            "supports_estimated",
            "supports_final",
            "supports_video_scope",
            "builtin",
        ):
            if not isinstance(getattr(self, flag_name), bool):
                raise TypeError(f"{flag_name} must be a bool")
        if not self.supports_estimated and not self.supports_final:
            raise ValueError("a revenue stream must support at least one stage")

    def supports_stage(self, stage: RevenueStage) -> bool:
        if stage is RevenueStage.ESTIMATED:
            return self.supports_estimated
        return self.supports_final

    def supports_scope(self, scope: MetricScope) -> bool:
        if scope is MetricScope.CHANNEL:
            return True
        return self.supports_video_scope

    def as_dict(self) -> dict[str, Any]:
        return {
            "revenue_type": self.revenue_type,
            "category": self.category.value,
            "default_source": self.default_source,
            "supports_estimated": self.supports_estimated,
            "supports_final": self.supports_final,
            "supports_video_scope": self.supports_video_scope,
            "builtin": self.builtin,
        }


BUILTIN_REVENUE_STREAMS: Final[tuple[RevenueStreamSpec, ...]] = (
    RevenueStreamSpec(
        revenue_type=RevenueSource.ADS.value,
        category=RevenueStreamCategory.YOUTUBE_ADS,
        default_source="youtube_analytics",
        builtin=True,
    ),
    RevenueStreamSpec(
        revenue_type=RevenueSource.SHORTS_ADS.value,
        category=RevenueStreamCategory.YOUTUBE_ADS,
        default_source="youtube_analytics",
        builtin=True,
    ),
    RevenueStreamSpec(
        revenue_type=RevenueSource.MEMBERSHIPS.value,
        category=RevenueStreamCategory.YOUTUBE_FAN_FUNDING,
        default_source="youtube_analytics",
        builtin=True,
    ),
    RevenueStreamSpec(
        revenue_type=RevenueSource.SUPER_THANKS.value,
        category=RevenueStreamCategory.YOUTUBE_FAN_FUNDING,
        default_source="youtube_analytics",
        builtin=True,
    ),
    RevenueStreamSpec(
        revenue_type=RevenueSource.SUPER_CHAT.value,
        category=RevenueStreamCategory.YOUTUBE_FAN_FUNDING,
        default_source="youtube_analytics",
        builtin=True,
    ),
    RevenueStreamSpec(
        revenue_type=RevenueSource.SPONSORSHIPS.value,
        category=RevenueStreamCategory.PARTNER_EXTERNAL,
        default_source="partner_ledger",
        builtin=True,
    ),
    RevenueStreamSpec(
        revenue_type=RevenueSource.AFFILIATE.value,
        category=RevenueStreamCategory.PARTNER_EXTERNAL,
        default_source="affiliate_ledger",
        builtin=True,
    ),
    RevenueStreamSpec(
        revenue_type=RevenueSource.MERCHANDISE.value,
        category=RevenueStreamCategory.PARTNER_EXTERNAL,
        default_source="merch_ledger",
        builtin=True,
    ),
)


class RevenueStreamRegistry:
    """Catalogue of built-in and extensible custom revenue streams."""

    def __init__(self, extra_streams: tuple[RevenueStreamSpec, ...] = ()) -> None:
        self._streams: dict[str, RevenueStreamSpec] = {
            spec.revenue_type: spec for spec in BUILTIN_REVENUE_STREAMS
        }
        for spec in extra_streams:
            self.register(spec)

    def register(self, spec: RevenueStreamSpec) -> RevenueStreamSpec:
        if not isinstance(spec, RevenueStreamSpec):
            raise TypeError("spec must be a RevenueStreamSpec")
        if spec.revenue_type in self._streams:
            raise RevenueSourceError(
                RevenueSourceErrorCode.DUPLICATE_REVENUE_TYPE,
                f"revenue_type {spec.revenue_type!r} is already registered",
            )
        self._streams[spec.revenue_type] = spec
        return spec

    def get(self, revenue_type: str) -> RevenueStreamSpec:
        spec = self._streams.get(revenue_type)
        if spec is None:
            raise RevenueSourceError(
                RevenueSourceErrorCode.UNKNOWN_REVENUE_TYPE,
                f"revenue_type {revenue_type!r} is not registered",
            )
        return spec

    def list_all(self) -> tuple[RevenueStreamSpec, ...]:
        return tuple(self._streams.values())


@dataclass(frozen=True)
class RecordedRevenueSourceOutcome:
    record: RevenueRecord
    spec: RevenueStreamSpec
    created: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "record": self.record.as_dict(),
            "spec": self.spec.as_dict(),
            "created": self.created,
        }


@dataclass(frozen=True)
class RevenueGoalProgress:
    """Comparison of recorded revenue for a stream against a strategy goal.

    ``is_guaranteed`` is always ``False``: strategy goals are tracking targets,
    never promised or guaranteed revenue (#052, #173).
    """

    revenue_type: str
    recorded_amount: Decimal
    monthly_target: Decimal | None
    currency: str

    @property
    def is_guaranteed(self) -> bool:
        return False

    @property
    def target_delta(self) -> Decimal | None:
        if self.monthly_target is None:
            return None
        return self.recorded_amount - self.monthly_target

    def as_dict(self) -> dict[str, Any]:
        return {
            "revenue_type": self.revenue_type,
            "recorded_amount": str(self.recorded_amount),
            "monthly_target": (
                str(self.monthly_target) if self.monthly_target is not None else None
            ),
            "target_delta": (
                str(self.target_delta) if self.target_delta is not None else None
            ),
            "currency": self.currency,
            "is_guaranteed": False,
        }


@dataclass(frozen=True)
class RevenueSourceBreakdown:
    """Single-stage breakdown of recorded revenue by stream and category."""

    scope: MetricScope
    subject_id: str
    stage: RevenueStage
    period_start: datetime
    period_end: datetime
    currency: str | None
    total_amount: Decimal
    by_revenue_type: dict[str, Decimal]
    by_category: dict[str, Decimal]
    records: tuple[RevenueRecord, ...]
    goal_progress: tuple[RevenueGoalProgress, ...] = ()

    @property
    def is_final(self) -> bool:
        return self.stage is RevenueStage.FINAL

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope.value,
            "subject_id": self.subject_id,
            "stage": self.stage.value,
            "is_final": self.is_final,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "currency": self.currency,
            "total_amount": str(self.total_amount),
            "by_revenue_type": {k: str(v) for k, v in self.by_revenue_type.items()},
            "by_category": {k: str(v) for k, v in self.by_category.items()},
            "records": [record.as_dict() for record in self.records],
            "goal_progress": [item.as_dict() for item in self.goal_progress],
        }


class RevenueSourceService:
    """Records and summarises multi-source revenue across ads, memberships, affiliate, sponsorships, and custom streams."""

    def __init__(
        self,
        database: Database,
        audit_log: AuditLog,
        *,
        registry: RevenueStreamRegistry | None = None,
        results: PublishResultService | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._audit = audit_log
        self._registry = registry or RevenueStreamRegistry()
        self._results = results or PublishResultService(database)
        self._clock = clock

    @property
    def registry(self) -> RevenueStreamRegistry:
        return self._registry

    def record(
        self,
        channel_id: str,
        *,
        stage: RevenueStage,
        revenue_type: str,
        amount: Decimal,
        currency: str,
        period_start: datetime,
        period_end: datetime,
        actor: Actor,
        source: str | None = None,
        youtube_video_id: str | None = None,
    ) -> RecordedRevenueSourceOutcome:
        """Record an estimated or final revenue figure for a channel or published video."""
        if not isinstance(stage, RevenueStage):
            raise TypeError("stage must be a RevenueStage")
        spec = self._registry.get(revenue_type)
        if not spec.supports_stage(stage):
            raise RevenueSourceError(
                RevenueSourceErrorCode.STAGE_NOT_SUPPORTED,
                f"revenue stream {revenue_type!r} does not support stage {stage.value}",
            )
        scope = MetricScope.VIDEO if youtube_video_id is not None else MetricScope.CHANNEL
        if not spec.supports_scope(scope):
            raise RevenueSourceError(
                RevenueSourceErrorCode.SCOPE_NOT_SUPPORTED,
                f"revenue stream {revenue_type!r} does not support scope {scope.value}",
            )
        now = self._now()
        if stage is RevenueStage.FINAL:
            require_ended_period(period_end, now)
        else:
            require_started_period(period_start, now)

        channel = load_syncable_channel(self._database, channel_id)
        subject_id = channel.id
        if youtube_video_id is not None:
            published = {
                rec.youtube_video_id
                for rec in self._results.list_for_channel(channel.id)
            }
            if youtube_video_id not in published:
                raise MetricsSyncError(
                    MetricsSyncErrorCode.VIDEO_NOT_PUBLISHED,
                    f"video {youtube_video_id} was not published by channel {channel.id}",
                )
            subject_id = youtube_video_id

        resolved_source = source if source is not None else spec.default_source
        with self._database.transaction() as connection:
            repository = RevenueRecordRepository(connection)
            same_stream = [
                stored
                for stored in repository.list_by_subject(scope, subject_id)
                if stored.stage is stage
                and stored.source == resolved_source
                and stored.revenue_type == revenue_type
                and stored.period_start == period_start
                and stored.period_end == period_end
            ]
            if (
                same_stream
                and same_stream[-1].amount == amount
                and same_stream[-1].currency == currency
            ):
                return RecordedRevenueSourceOutcome(same_stream[-1], spec, False)
            record = RevenueRecord.create(
                stage,
                scope,
                subject_id,
                revenue_type=revenue_type,
                source=resolved_source,
                amount=amount,
                currency=currency,
                period_start=period_start,
                period_end=period_end,
                clock=lambda: now,
            )
            repository.add(record)

        self._audit.record(
            REVENUE_SOURCE_RECORDED,
            actor,
            EntityRef(CHANNEL_ENTITY, channel.id),
            AuditResult.SUCCESS,
            metadata={
                "channel_id": channel.id,
                "record_id": record.id,
                "scope": scope.value,
                "subject_id": subject_id,
                "stage": stage.value,
                "revenue_type": revenue_type,
                "category": spec.category.value,
                "source": resolved_source,
                "currency": currency,
            },
        )
        return RecordedRevenueSourceOutcome(record, spec, True)

    def breakdown(
        self,
        scope: MetricScope,
        subject_id: str,
        *,
        stage: RevenueStage,
        period_start: datetime,
        period_end: datetime,
    ) -> RevenueSourceBreakdown:
        """Compute a single-stage revenue breakdown by stream and category for ``[period_start, period_end)``."""
        if not isinstance(scope, MetricScope):
            raise TypeError("scope must be a MetricScope")
        if not isinstance(stage, RevenueStage):
            raise TypeError("stage must be a RevenueStage")
        connection = self._database.connect()
        try:
            if scope is MetricScope.CHANNEL and ChannelRepository(connection).get(subject_id) is None:
                raise MetricsSyncError(
                    MetricsSyncErrorCode.CHANNEL_NOT_FOUND,
                    f"channel {subject_id} was not found",
                )
            all_records = RevenueRecordRepository(connection).list_by_subject(
                scope, subject_id
            )
            profile = (
                StrategyProfileRepository(connection).get_by_channel(subject_id)
                if scope is MetricScope.CHANNEL
                else None
            )
        finally:
            connection.close()

        # Select the latest record per (revenue_type, source) matching stage and exact period
        latest_by_stream: dict[tuple[str, str], RevenueRecord] = {}
        for record in all_records:
            if (
                record.stage is stage
                and record.period_start == period_start
                and record.period_end == period_end
            ):
                latest_by_stream[(record.revenue_type, record.source)] = record

        selected = tuple(latest_by_stream.values())
        currencies = {record.currency for record in selected}
        if len(currencies) > 1:
            raise RevenueSourceError(
                RevenueSourceErrorCode.CURRENCY_MISMATCH,
                f"multiple currencies in breakdown: {sorted(currencies)}",
            )
        currency = next(iter(currencies), None)
        if currency is None and profile is not None and profile.monetization and profile.monetization.currency:
            currency = profile.monetization.currency
            if not CURRENCY_PATTERN.match(currency):
                currency = None

        by_type: dict[str, Decimal] = {}
        by_category: dict[str, Decimal] = {}
        total = Decimal("0")
        for record in selected:
            spec = self._spec_or_custom(record.revenue_type, record.source)
            by_type[record.revenue_type] = (
                by_type.get(record.revenue_type, Decimal("0")) + record.amount
            )
            by_category[spec.category.value] = (
                by_category.get(spec.category.value, Decimal("0")) + record.amount
            )
            total += record.amount

        goal_progress = _build_goal_progress(
            profile.monetization.goals if profile and profile.monetization else (),
            monetization_currency=(
                profile.monetization.currency if profile and profile.monetization else None
            ),
            by_type=by_type,
            active_currency=currency,
        )
        return RevenueSourceBreakdown(
            scope=scope,
            subject_id=subject_id,
            stage=stage,
            period_start=period_start,
            period_end=period_end,
            currency=currency,
            total_amount=total,
            by_revenue_type=by_type,
            by_category=by_category,
            records=selected,
            goal_progress=goal_progress,
        )

    def _spec_or_custom(self, revenue_type: str, source: str) -> RevenueStreamSpec:
        try:
            return self._registry.get(revenue_type)
        except RevenueSourceError:
            return RevenueStreamSpec(
                revenue_type=revenue_type,
                category=RevenueStreamCategory.CUSTOM,
                default_source=source,
                builtin=False,
            )

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)


def _build_goal_progress(
    goals: tuple[RevenueGoal, ...],
    *,
    monetization_currency: str | None,
    by_type: dict[str, Decimal],
    active_currency: str | None,
) -> tuple[RevenueGoalProgress, ...]:
    if not goals:
        return ()
    effective_currency = active_currency or monetization_currency or "USD"
    if (
        monetization_currency is not None
        and active_currency is not None
        and monetization_currency != active_currency
    ):
        raise RevenueSourceError(
            RevenueSourceErrorCode.CURRENCY_MISMATCH,
            f"monetization goal currency {monetization_currency} != recorded revenue currency {active_currency}",
        )
    progress: list[RevenueGoalProgress] = []
    for goal in goals:
        rtype = goal.source.value
        recorded = by_type.get(rtype, Decimal("0"))
        progress.append(
            RevenueGoalProgress(
                revenue_type=rtype,
                recorded_amount=recorded,
                monthly_target=goal.monthly_target,
                currency=effective_currency,
            )
        )
    return tuple(progress)
