"""Repository of the AI disclosure decision history of content items (G-081)."""

import sqlite3

from ai_youtube_agent.content.disclosure import (
    DisclosureDecision,
    DisclosureRecord,
    DisclosureSources,
    RationaleEntry,
)
from ai_youtube_agent.core.db.repositories.base import (
    Repository,
    actor_columns,
    actor_from,
    dt,
    from_json,
    parse_dt,
    to_json,
)


class DisclosureRepository(Repository):
    """The history is append only: add and read, no update and no delete."""

    table = "disclosure_decisions"

    def add(self, record: DisclosureRecord) -> None:
        self._insert(
            self.table,
            {
                "id": record.id,
                "content_item_id": record.content_item_id,
                "channel_id": record.channel_id,
                "rule_set_id": record.rule_set_id,
                "rule_set_version": record.rule_set_version,
                "decision": record.decision.value,
                "rationale_json": to_json([e.as_dict() for e in record.rationale]),
                "sources_json": to_json(record.sources.as_dict()),
                **actor_columns("decided_by", record.decided_by),
                "created_at": dt(record.created_at),
            },
        )

    def latest(self, content_item_id: str) -> DisclosureRecord | None:
        row = self._one(
            "SELECT * FROM disclosure_decisions WHERE content_item_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (content_item_id,),
        )
        return _record(row) if row else None

    def list_by_content_item(self, content_item_id: str) -> list[DisclosureRecord]:
        rows = self._all(
            "SELECT * FROM disclosure_decisions WHERE content_item_id = ? "
            "ORDER BY created_at, rowid",
            (content_item_id,),
        )
        return [_record(row) for row in rows]


def _record(row: sqlite3.Row) -> DisclosureRecord:
    sources = from_json(row["sources_json"])
    return DisclosureRecord(
        id=row["id"],
        content_item_id=row["content_item_id"],
        channel_id=row["channel_id"],
        rule_set_id=row["rule_set_id"],
        rule_set_version=row["rule_set_version"],
        decision=DisclosureDecision(row["decision"]),
        rationale=tuple(
            RationaleEntry(**entry) for entry in from_json(row["rationale_json"])
        ),
        sources=DisclosureSources(tuple(sources["facts"]), tuple(sources["asset_ids"])),
        decided_by=actor_from(row, "decided_by"),
        created_at=parse_dt(row["created_at"]),
    )
