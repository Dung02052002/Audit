"""Repositories for metric snapshots, revenue and cost records (C13, C14).

All three are immutable records: add only.
"""

import sqlite3
from datetime import datetime

from ai_youtube_agent.content.analytics import MetricScope, MetricSnapshot
from ai_youtube_agent.content.cost import CostCategory, CostRecord
from ai_youtube_agent.content.revenue import RevenueRecord, RevenueStage
from ai_youtube_agent.core.db.codec import format_decimal, parse_decimal
from ai_youtube_agent.core.db.repositories.base import Repository, dt, parse_dt


class MetricSnapshotRepository(Repository):
    table = "metric_snapshots"

    def add(self, snapshot: MetricSnapshot) -> None:
        self._insert(
            "metric_snapshots",
            {
                "id": snapshot.id,
                "scope": snapshot.scope.value,
                "subject_id": snapshot.subject_id,
                "source": snapshot.source,
                "period_start": dt(snapshot.period_start),
                "period_end": dt(snapshot.period_end),
                "retrieved_at": dt(snapshot.retrieved_at),
            },
        )
        for name, value in snapshot.metrics.items():
            self._insert(
                "metric_values",
                {
                    "snapshot_id": snapshot.id,
                    "name": name,
                    "value": format_decimal(value),
                },
            )

    def get(self, snapshot_id: str) -> MetricSnapshot | None:
        row = self._one("SELECT * FROM metric_snapshots WHERE id = ?", (snapshot_id,))
        return self._snapshot(row) if row else None

    def list_by_subject(
        self, scope: MetricScope, subject_id: str
    ) -> list[MetricSnapshot]:
        rows = self._all(
            "SELECT * FROM metric_snapshots WHERE scope = ? AND subject_id = ? "
            "ORDER BY period_start, retrieved_at, id",
            (scope.value, subject_id),
        )
        return [self._snapshot(row) for row in rows]

    def _snapshot(self, row: sqlite3.Row) -> MetricSnapshot:
        values = self._all(
            "SELECT name, value FROM metric_values WHERE snapshot_id = ? "
            "ORDER BY rowid",
            (row["id"],),
        )
        return MetricSnapshot(
            id=row["id"],
            scope=MetricScope(row["scope"]),
            subject_id=row["subject_id"],
            source=row["source"],
            period_start=parse_dt(row["period_start"]),
            period_end=parse_dt(row["period_end"]),
            retrieved_at=parse_dt(row["retrieved_at"]),
            metrics={v["name"]: parse_decimal(v["value"]) for v in values},
        )


class RevenueRecordRepository(Repository):
    table = "revenue_records"

    def add(self, record: RevenueRecord) -> None:
        self._insert(
            self.table,
            {
                "id": record.id,
                "stage": record.stage.value,
                "scope": record.scope.value,
                "subject_id": record.subject_id,
                "revenue_type": record.revenue_type,
                "source": record.source,
                "amount": format_decimal(record.amount),
                "currency": record.currency,
                "period_start": dt(record.period_start),
                "period_end": dt(record.period_end),
                "retrieved_at": dt(record.retrieved_at),
            },
        )

    def get(self, record_id: str) -> RevenueRecord | None:
        row = self._one("SELECT * FROM revenue_records WHERE id = ?", (record_id,))
        return _revenue(row) if row else None

    def list_by_subject(
        self, scope: MetricScope, subject_id: str
    ) -> list[RevenueRecord]:
        rows = self._all(
            "SELECT * FROM revenue_records WHERE scope = ? AND subject_id = ? "
            "ORDER BY period_start, stage, retrieved_at, id",
            (scope.value, subject_id),
        )
        return [_revenue(row) for row in rows]


class CostRecordRepository(Repository):
    table = "cost_records"

    def add(self, record: CostRecord) -> None:
        self._insert(
            self.table,
            {
                "id": record.id,
                "channel_id": record.channel_id,
                "category": record.category.value,
                "provider": record.provider,
                "amount": format_decimal(record.amount),
                "currency": record.currency,
                "incurred_at": dt(record.incurred_at),
                "content_item_id": record.content_item_id,
                "ref": record.ref,
            },
        )

    def get(self, record_id: str) -> CostRecord | None:
        row = self._one("SELECT * FROM cost_records WHERE id = ?", (record_id,))
        return _cost(row) if row else None

    def list_by_channel(
        self,
        channel_id: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[CostRecord]:
        """Costs of a channel, optionally within the half-open range [start, end)."""
        sql = "SELECT * FROM cost_records WHERE channel_id = ?"
        params: list = [channel_id]
        if start is not None:
            sql += " AND incurred_at >= ?"
            params.append(dt(start))
        if end is not None:
            sql += " AND incurred_at < ?"
            params.append(dt(end))
        rows = self._all(sql + " ORDER BY incurred_at, id", params)
        return [_cost(row) for row in rows]


def _revenue(row: sqlite3.Row) -> RevenueRecord:
    return RevenueRecord(
        id=row["id"],
        stage=RevenueStage(row["stage"]),
        scope=MetricScope(row["scope"]),
        subject_id=row["subject_id"],
        revenue_type=row["revenue_type"],
        source=row["source"],
        amount=parse_decimal(row["amount"]),
        currency=row["currency"],
        period_start=parse_dt(row["period_start"]),
        period_end=parse_dt(row["period_end"]),
        retrieved_at=parse_dt(row["retrieved_at"]),
    )


def _cost(row: sqlite3.Row) -> CostRecord:
    return CostRecord(
        id=row["id"],
        channel_id=row["channel_id"],
        category=CostCategory(row["category"]),
        provider=row["provider"],
        amount=parse_decimal(row["amount"]),
        currency=row["currency"],
        incurred_at=parse_dt(row["incurred_at"]),
        content_item_id=row["content_item_id"],
        ref=row["ref"],
    )
