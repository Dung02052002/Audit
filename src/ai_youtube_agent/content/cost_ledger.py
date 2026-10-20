"""Unified contribution cost ledger (Prompt Pack v8, prompt #174), context C14 Economics.

``CostLedger`` records and aggregates all tracked contribution costs across
``CostCategory`` (``production``, ``api``, ``tts``, ``render``, ``storage``, ``llm``):

- Every cost is persisted as an immutable ``CostRecord`` (#025) in ``cost_records``.
- Validates that the channel exists, that any ``content_item_id`` exists and belongs
  to the channel, and that ``currency`` matches ``StrategyProfile.budget.currency``
  when a budget is configured on the channel.
- Idempotent when ``ref`` is supplied: if a ``CostRecord`` for ``(category, ref)``
  already exists on the same channel, it is returned with ``created=False`` and no
  duplicate row or audit event is written.
- Corrections are append-only (``record_correction`` creates a new ``CostRecord``
  referencing the original record id).
- Aggregates contribution costs by channel, content item, category, provider, and
  half-open UTC time window ``[start, end)``.
- Audits ``cost.recorded`` and ``cost.correction_recorded`` after commit with scalar
  metadata only.
"""

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.cost import CostCategory, CostRecord
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    EntityRef,
    MetadataValue,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.economics import CostRecordRepository
from ai_youtube_agent.core.errors import DomainError

Clock = Callable[[], datetime]

COST_RECORDED: Final[str] = "cost.recorded"
COST_CORRECTION_RECORDED: Final[str] = "cost.correction_recorded"
COST_ENTITY: Final[str] = "cost_record"
SYSTEM_LEDGER_ACTOR: Final[Actor] = Actor(ActorKind.SYSTEM, "cost_ledger")
COST_AMOUNT_STEP: Final[Decimal] = Decimal("0.000001")


def quantize_cost_amount(value: Decimal) -> Decimal:
    """Round a non-negative finite ``Decimal`` cost amount half-up to 6 decimal places."""
    if (
        isinstance(value, bool)
        or not isinstance(value, Decimal)
        or not value.is_finite()
        or value < 0
    ):
        raise ValueError("cost amount must be a finite Decimal of 0 or more")
    return value.quantize(COST_AMOUNT_STEP, ROUND_HALF_UP)


class CostLedgerErrorCode(StrEnum):
    CHANNEL_NOT_FOUND = "domain.cost_channel_not_found"
    ITEM_NOT_FOUND = "domain.cost_item_not_found"
    CHANNEL_MISMATCH = "domain.cost_channel_mismatch"
    CURRENCY_MISMATCH = "domain.cost_currency_mismatch"
    COST_NOT_FOUND = "domain.cost_record_not_found"
    INVALID_RANGE = "domain.cost_invalid_range"


USER_MESSAGES: Final[dict[CostLedgerErrorCode, str]] = {
    CostLedgerErrorCode.CHANNEL_NOT_FOUND: "The channel does not exist.",
    CostLedgerErrorCode.ITEM_NOT_FOUND: "The content item does not exist.",
    CostLedgerErrorCode.CHANNEL_MISMATCH: (
        "The cost record or content item belongs to another channel."
    ),
    CostLedgerErrorCode.CURRENCY_MISMATCH: (
        "The cost currency differs from the channel budget or existing cost records."
    ),
    CostLedgerErrorCode.COST_NOT_FOUND: "The referenced cost record does not exist.",
    CostLedgerErrorCode.INVALID_RANGE: "The cost time window is invalid.",
}

_STATUS: Final[dict[CostLedgerErrorCode, HTTPStatus]] = {
    CostLedgerErrorCode.CHANNEL_NOT_FOUND: HTTPStatus.NOT_FOUND,
    CostLedgerErrorCode.ITEM_NOT_FOUND: HTTPStatus.NOT_FOUND,
    CostLedgerErrorCode.CHANNEL_MISMATCH: HTTPStatus.UNPROCESSABLE_ENTITY,
    CostLedgerErrorCode.CURRENCY_MISMATCH: HTTPStatus.CONFLICT,
    CostLedgerErrorCode.COST_NOT_FOUND: HTTPStatus.NOT_FOUND,
    CostLedgerErrorCode.INVALID_RANGE: HTTPStatus.UNPROCESSABLE_ENTITY,
}


class CostLedgerError(DomainError):
    default_code = CostLedgerErrorCode.CURRENCY_MISMATCH.value
    default_user_message = USER_MESSAGES[CostLedgerErrorCode.CURRENCY_MISMATCH]
    default_http_status = HTTPStatus.CONFLICT

    def __init__(self, code: CostLedgerErrorCode, detail: str) -> None:
        if not isinstance(code, CostLedgerErrorCode):
            raise TypeError("code must be a CostLedgerErrorCode")
        self.ledger_code = code
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        self.default_http_status = _STATUS[code]


@dataclass(frozen=True)
class CostLedgerEntry:
    cost: CostRecord
    created: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "cost": self.cost.as_dict(),
            "created": self.created,
        }


@dataclass(frozen=True)
class CostLedgerSummary:
    channel_id: str
    content_item_id: str | None
    start: datetime | None
    end: datetime | None
    currency: str | None
    total_amount: Decimal
    by_category: dict[str, Decimal]
    by_provider: dict[str, Decimal]
    records: tuple[CostRecord, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "content_item_id": self.content_item_id,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "currency": self.currency,
            "total_amount": str(self.total_amount),
            "by_category": {k: str(v) for k, v in self.by_category.items()},
            "by_provider": {k: str(v) for k, v in self.by_provider.items()},
            "records": [record.as_dict() for record in self.records],
        }


class CostLedger:
    """Append-only ledger for all tracked contribution costs."""

    def __init__(
        self,
        database: Database,
        audit_log: AuditLog,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._audit = audit_log
        self._clock = clock

    def record(
        self,
        channel_id: str,
        category: CostCategory,
        *,
        provider: str,
        amount: Decimal,
        currency: str,
        content_item_id: str | None = None,
        ref: str | None = None,
        actor: Actor = SYSTEM_LEDGER_ACTOR,
        extra_metadata: Mapping[str, MetadataValue] | None = None,
    ) -> CostLedgerEntry:
        """Record a contribution cost in ``cost_records``, idempotently when ``ref`` is set."""
        if not isinstance(category, CostCategory):
            raise TypeError("category must be a CostCategory")
        rounded = quantize_cost_amount(amount)
        with self._database.transaction() as connection:
            self._validate_channel_and_item(
                connection,
                channel_id=channel_id,
                content_item_id=content_item_id,
                currency=currency,
            )
            costs = CostRecordRepository(connection)
            if ref is not None:
                existing = costs.find_by_ref(category, ref)
                if existing is not None:
                    if existing.channel_id != channel_id:
                        raise CostLedgerError(
                            CostLedgerErrorCode.CHANNEL_MISMATCH,
                            f"cost ref {ref!r} in category {category.value} belongs to channel {existing.channel_id}",
                        )
                    return CostLedgerEntry(existing, False)
            cost = CostRecord.create(
                channel_id,
                category,
                provider=provider,
                amount=rounded,
                currency=currency,
                content_item_id=content_item_id,
                ref=ref,
                clock=self._clock,
            )
            costs.add(cost)

        meta: dict[str, MetadataValue] = {
            "channel_id": cost.channel_id,
            "category": cost.category.value,
            "provider": cost.provider,
            "amount": str(cost.amount),
            "currency": cost.currency,
            "content_item_id": cost.content_item_id,
            "ref": cost.ref,
        }
        if extra_metadata:
            for key, val in extra_metadata.items():
                if key not in meta:
                    meta[key] = val
        self._audit.record(
            COST_RECORDED,
            actor,
            EntityRef(COST_ENTITY, cost.id),
            AuditResult.SUCCESS,
            meta,
        )
        return CostLedgerEntry(cost, True)

    def record_correction(
        self,
        original_cost_id: str,
        *,
        additional_amount: Decimal,
        actor: Actor = SYSTEM_LEDGER_ACTOR,
        ref: str | None = None,
    ) -> CostLedgerEntry:
        """Append a supplementary ``CostRecord`` referencing ``original_cost_id``."""
        rounded = quantize_cost_amount(additional_amount)
        with self._database.transaction() as connection:
            costs = CostRecordRepository(connection)
            original = costs.get(original_cost_id)
            if original is None:
                raise CostLedgerError(
                    CostLedgerErrorCode.COST_NOT_FOUND,
                    f"original cost record {original_cost_id} does not exist",
                )
            self._validate_channel_and_item(
                connection,
                channel_id=original.channel_id,
                content_item_id=original.content_item_id,
                currency=original.currency,
            )
            correction_ref = ref or f"correction:{original.id}"
            existing = costs.find_by_ref(original.category, correction_ref)
            if existing is not None:
                if existing.channel_id != original.channel_id:
                    raise CostLedgerError(
                        CostLedgerErrorCode.CHANNEL_MISMATCH,
                        f"correction ref {correction_ref!r} belongs to another channel",
                    )
                return CostLedgerEntry(existing, False)
            correction = CostRecord.create(
                original.channel_id,
                original.category,
                provider=original.provider,
                amount=rounded,
                currency=original.currency,
                content_item_id=original.content_item_id,
                ref=correction_ref,
                clock=self._clock,
            )
            costs.add(correction)

        self._audit.record(
            COST_CORRECTION_RECORDED,
            actor,
            EntityRef(COST_ENTITY, correction.id),
            AuditResult.SUCCESS,
            {
                "original_cost_id": original.id,
                "channel_id": correction.channel_id,
                "category": correction.category.value,
                "provider": correction.provider,
                "amount": str(correction.amount),
                "currency": correction.currency,
                "content_item_id": correction.content_item_id,
                "ref": correction.ref,
            },
        )
        return CostLedgerEntry(correction, True)

    def get(self, cost_id: str) -> CostRecord | None:
        connection = self._database.connect()
        try:
            return CostRecordRepository(connection).get(cost_id)
        finally:
            connection.close()

    def find_by_ref(self, category: CostCategory, ref: str) -> CostRecord | None:
        connection = self._database.connect()
        try:
            return CostRecordRepository(connection).find_by_ref(category, ref)
        finally:
            connection.close()

    def list_for_channel(
        self,
        channel_id: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        category: CostCategory | None = None,
        content_item_id: str | None = None,
    ) -> list[CostRecord]:
        if start is not None and end is not None and start >= end:
            raise CostLedgerError(
                CostLedgerErrorCode.INVALID_RANGE,
                "start must be earlier than end",
            )
        connection = self._database.connect()
        try:
            if ChannelRepository(connection).get(channel_id) is None:
                raise CostLedgerError(
                    CostLedgerErrorCode.CHANNEL_NOT_FOUND,
                    f"channel {channel_id} does not exist",
                )
            records = CostRecordRepository(connection).list_by_channel(
                channel_id, start=start, end=end
            )
        finally:
            connection.close()
        return [
            rec
            for rec in records
            if (category is None or rec.category is category)
            and (content_item_id is None or rec.content_item_id == content_item_id)
        ]

    def list_for_content_item(
        self,
        content_item_id: str,
        *,
        category: CostCategory | None = None,
    ) -> list[CostRecord]:
        connection = self._database.connect()
        try:
            item = ContentItemRepository(connection).get(content_item_id)
            if item is None:
                raise CostLedgerError(
                    CostLedgerErrorCode.ITEM_NOT_FOUND,
                    f"content item {content_item_id} does not exist",
                )
            records = CostRecordRepository(connection).list_by_channel(item.channel_id)
        finally:
            connection.close()
        return [
            rec
            for rec in records
            if rec.content_item_id == content_item_id
            and (category is None or rec.category is category)
        ]

    def summarize(
        self,
        channel_id: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        content_item_id: str | None = None,
    ) -> CostLedgerSummary:
        """Aggregate tracked contribution costs for ``channel_id`` (and optional item/window)."""
        records = self.list_for_channel(
            channel_id,
            start=start,
            end=end,
            content_item_id=content_item_id,
        )
        currencies = {rec.currency for rec in records}
        if len(currencies) > 1:
            raise CostLedgerError(
                CostLedgerErrorCode.CURRENCY_MISMATCH,
                f"cost records span multiple currencies: {sorted(currencies)}",
            )
        currency = next(iter(currencies), None)
        if currency is None:
            connection = self._database.connect()
            try:
                strategy = StrategyProfileRepository(connection).get_by_channel(
                    channel_id
                )
            finally:
                connection.close()
            if strategy and strategy.budget:
                currency = strategy.budget.currency

        total = Decimal("0")
        by_category: dict[str, Decimal] = {}
        by_provider: dict[str, Decimal] = {}
        for rec in records:
            total += rec.amount
            by_category[rec.category.value] = (
                by_category.get(rec.category.value, Decimal("0")) + rec.amount
            )
            by_provider[rec.provider] = (
                by_provider.get(rec.provider, Decimal("0")) + rec.amount
            )
        return CostLedgerSummary(
            channel_id=channel_id,
            content_item_id=content_item_id,
            start=start,
            end=end,
            currency=currency,
            total_amount=total,
            by_category=by_category,
            by_provider=by_provider,
            records=tuple(records),
        )

    @staticmethod
    def _validate_channel_and_item(
        connection: sqlite3.Connection,
        *,
        channel_id: str,
        content_item_id: str | None,
        currency: str,
    ) -> None:
        channel = ChannelRepository(connection).get(channel_id)
        if channel is None:
            raise CostLedgerError(
                CostLedgerErrorCode.CHANNEL_NOT_FOUND,
                f"channel {channel_id} does not exist",
            )
        if content_item_id is not None:
            item = ContentItemRepository(connection).get(content_item_id)
            if item is None:
                raise CostLedgerError(
                    CostLedgerErrorCode.ITEM_NOT_FOUND,
                    f"content item {content_item_id} does not exist",
                )
            if item.channel_id != channel_id:
                raise CostLedgerError(
                    CostLedgerErrorCode.CHANNEL_MISMATCH,
                    f"content item {content_item_id} belongs to channel {item.channel_id}, not {channel_id}",
                )
        strategy = StrategyProfileRepository(connection).get_by_channel(channel_id)
        budget = strategy.budget if strategy else None
        if budget is not None and budget.currency != currency:
            raise CostLedgerError(
                CostLedgerErrorCode.CURRENCY_MISMATCH,
                f"cost currency {currency} differs from channel budget currency {budget.currency}",
            )
