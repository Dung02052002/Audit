"""Repositories of the paid AI API policy and quotes (BUDGET-01)."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from ai_youtube_agent.content.paid_api_budget import (
    PaidApiMode,
    PaidApiPolicy,
    PaidQuoteStatus,
    PaidRequestQuote,
    PaidTaskType,
)
from ai_youtube_agent.core.db.codec import format_decimal, parse_decimal
from ai_youtube_agent.core.db.database import ConcurrencyError
from ai_youtube_agent.core.db.repositories.base import (
    Repository,
    actor_columns,
    actor_from,
    dt,
    from_json,
    parse_dt,
    to_json,
)

POLICY_ID = 1


class PaidApiPolicyRepository(Repository):
    """The single policy row (id 1); no row means the default policy."""

    table = "paid_api_policy"

    def get(self) -> PaidApiPolicy | None:
        row = self._one("SELECT * FROM paid_api_policy WHERE id = ?", (POLICY_ID,))
        if row is None:
            return None
        return PaidApiPolicy(
            mode=PaidApiMode(row["mode"]),
            version=row["version"],
            updated_by=actor_from(row, "updated_by"),
            updated_at=parse_dt(row["updated_at"]),
        )

    def upsert(self, policy: PaidApiPolicy, *, expected_version: int) -> None:
        """Insert the row at version 0, else update it if it is still at
        ``expected_version``; ``ConcurrencyError`` otherwise."""
        values = {
            "mode": policy.mode.value,
            "version": policy.version,
            **actor_columns("updated_by", policy.updated_by),
            "updated_at": dt(policy.updated_at),
        }
        if expected_version == 0:
            try:
                self._insert(self.table, {"id": POLICY_ID, **values})
            except sqlite3.IntegrityError as error:
                if self.get() is None:
                    raise
                raise ConcurrencyError(
                    "paid_api_policy was saved since it was read"
                ) from error
            return
        self._update(
            POLICY_ID, values, guard_column="version", guard_value=expected_version
        )


class PaidApiQuoteRepository(Repository):
    """Quotes are added once and change only through conditional transitions."""

    table = "paid_api_quotes"

    def add(self, quote: PaidRequestQuote) -> None:
        self._insert(
            self.table,
            {
                "id": quote.id,
                "status": quote.status.value,
                "task_type": quote.task_type.value,
                "provider": quote.provider,
                "model": quote.model,
                "request_hash": quote.request_hash,
                "unit": quote.unit,
                "unit_count": format_decimal(quote.unit_count),
                "duration_ms": quote.duration_ms,
                "scene_id": quote.scene_id,
                "unit_price": _money(quote.unit_price),
                "estimated_amount": _money(quote.estimated_amount),
                "currency": quote.currency,
                "price_source": quote.price_source,
                "conditions_json": to_json(list(quote.conditions)),
                "unknowns_json": to_json(list(quote.unknowns)),
                "blocked_reason": quote.blocked_reason,
                "content_item_id": quote.content_item_id,
                "job_id": quote.job_id,
                "created_at": dt(quote.created_at),
                "expires_at": dt(quote.expires_at),
                **actor_columns("decided_by", quote.decided_by),
                "decided_at": dt(quote.decided_at),
                "consumed_at": dt(quote.consumed_at),
            },
        )

    def get(self, quote_id: str) -> PaidRequestQuote | None:
        row = self._one("SELECT * FROM paid_api_quotes WHERE id = ?", (quote_id,))
        return _quote(row) if row else None

    def find_open(self, request_hash: str) -> PaidRequestQuote | None:
        """The pending or approved quote of ``request_hash`` (at most one)."""
        row = self._one(
            "SELECT * FROM paid_api_quotes WHERE request_hash = ? "
            "AND status IN ('pending', 'approved')",
            (request_hash,),
        )
        return _quote(row) if row else None

    def list(
        self,
        *,
        status: PaidQuoteStatus | None = None,
        content_item_id: str | None = None,
        job_id: str | None = None,
        limit: int = 50,
    ) -> list[PaidRequestQuote]:
        """Newest first."""
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if content_item_id is not None:
            clauses.append("content_item_id = ?")
            params.append(content_item_id)
        if job_id is not None:
            clauses.append("job_id = ?")
            params.append(job_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._all(
            f"SELECT * FROM paid_api_quotes{where} "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (*params, limit),
        )
        return [_quote(row) for row in rows]

    def transition(
        self,
        quote_id: str,
        from_status: PaidQuoteStatus,
        to_status: PaidQuoteStatus,
        **columns: Any,
    ) -> bool:
        """Move a quote from ``from_status`` to ``to_status``; ``False`` when it
        is no longer in ``from_status``."""
        values = {"status": to_status.value, **columns}
        assignments = ", ".join(f"{column} = ?" for column in values)
        cursor = self.connection.execute(
            f"UPDATE paid_api_quotes SET {assignments} WHERE id = ? AND status = ?",
            (*values.values(), quote_id, from_status.value),
        )
        return cursor.rowcount == 1

    def consume(self, quote_id: str, now: datetime) -> PaidRequestQuote | None:
        """Consume quote ``quote_id`` atomically while it is approved and
        unexpired; ``None`` when it no longer is."""
        moment = dt(now)
        cursor = self.connection.execute(
            "UPDATE paid_api_quotes SET status = 'consumed', consumed_at = ? "
            "WHERE id = ? AND status = 'approved' AND expires_at > ?",
            (moment, quote_id, moment),
        )
        if cursor.rowcount != 1:
            return None
        return self.get(quote_id)


def _money(value: Any) -> str | None:
    return format_decimal(value) if value is not None else None


def _decimal(text: str | None) -> Any:
    return parse_decimal(text) if text is not None else None


def _quote(row: sqlite3.Row) -> PaidRequestQuote:
    return PaidRequestQuote(
        id=row["id"],
        status=PaidQuoteStatus(row["status"]),
        task_type=PaidTaskType(row["task_type"]),
        provider=row["provider"],
        model=row["model"],
        request_hash=row["request_hash"],
        unit=row["unit"],
        unit_count=parse_decimal(row["unit_count"]),
        duration_ms=row["duration_ms"],
        scene_id=row["scene_id"],
        unit_price=_decimal(row["unit_price"]),
        estimated_amount=_decimal(row["estimated_amount"]),
        currency=row["currency"],
        price_source=row["price_source"],
        conditions=tuple(from_json(row["conditions_json"])),
        unknowns=tuple(from_json(row["unknowns_json"])),
        blocked_reason=row["blocked_reason"],
        content_item_id=row["content_item_id"],
        job_id=row["job_id"],
        created_at=parse_dt(row["created_at"]),
        expires_at=parse_dt(row["expires_at"]),
        decided_by=actor_from(row, "decided_by"),
        decided_at=parse_dt(row["decided_at"]),
        consumed_at=parse_dt(row["consumed_at"]),
    )
