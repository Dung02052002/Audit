"""Helpers shared by the repositories."""

import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.db.codec import format_datetime, parse_datetime
from ai_youtube_agent.core.db.database import ConcurrencyError, RecordNotFoundError


class Repository:
    table: str

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def _insert(self, table: str, values: dict[str, Any]) -> None:
        columns = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        self.connection.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({marks})",
            tuple(values.values()),
        )

    def _one(self, sql: str, params: Sequence[Any]) -> sqlite3.Row | None:
        cursor = self.connection.execute(sql, params)
        cursor.row_factory = sqlite3.Row
        return cursor.fetchone()

    def _all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        cursor = self.connection.execute(sql, params)
        cursor.row_factory = sqlite3.Row
        return cursor.fetchall()

    def _update(
        self,
        entity_id: str,
        values: dict[str, Any],
        *,
        guard_column: str,
        guard_value: Any,
        table: str | None = None,
    ) -> None:
        table = table or self.table
        assignments = ", ".join(f"{column} = ?" for column in values)
        cursor = self.connection.execute(
            f"UPDATE {table} SET {assignments} WHERE id = ? AND {guard_column} = ?",
            (*values.values(), entity_id, guard_value),
        )
        if cursor.rowcount == 1:
            return
        exists = self.connection.execute(
            f"SELECT 1 FROM {table} WHERE id = ?", (entity_id,)
        ).fetchone()
        if exists is None:
            raise RecordNotFoundError(f"{table} {entity_id} does not exist")
        raise ConcurrencyError(
            f"{table} {entity_id} changed since it was read "
            f"({guard_column} is no longer {guard_value!r})"
        )


def dt(moment: datetime | None) -> str | None:
    return format_datetime(moment) if moment is not None else None


def parse_dt(text: str | None) -> datetime | None:
    return parse_datetime(text) if text is not None else None


def actor_columns(role: str, actor: Actor | None) -> dict[str, str | None]:
    return {
        f"{role}_kind": actor.kind.value if actor else None,
        f"{role}_id": actor.id if actor else None,
    }


def actor_from(row: sqlite3.Row, role: str) -> Actor | None:
    kind = row[f"{role}_kind"]
    return Actor(ActorKind(kind), row[f"{role}_id"]) if kind is not None else None


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def from_json(text: str) -> Any:
    return json.loads(text)
