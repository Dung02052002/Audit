"""Repository for research sources (C4, #055).

Sources are immutable records: add only. One source per normalised URL;
``add_or_get`` returns the stored source when the page is already known.
"""

import sqlite3
from collections.abc import Sequence

from ai_youtube_agent.content.source import EvidenceNote, Source
from ai_youtube_agent.core.db.repositories.base import (
    Repository,
    dt,
    from_json,
    parse_dt,
    to_json,
)


class SourceRepository(Repository):
    table = "sources"

    def add(self, source: Source) -> None:
        self._insert(
            self.table,
            {
                "id": source.id,
                "url": source.url,
                "final_url": source.final_url,
                "normalized_url": source.normalized_url,
                "title": source.title,
                "provider": source.provider,
                "published_at": dt(source.published_at),
                "retrieved_at": dt(source.retrieved_at),
                "evidence_notes_json": to_json(
                    [note.as_dict() for note in source.evidence_notes]
                ),
            },
        )

    def add_or_get(self, source: Source) -> Source:
        """Store ``source``, or return the source already stored for its URL."""
        existing = self.get_by_normalized_url(source.normalized_url)
        if existing is not None:
            return existing
        self.add(source)
        return source

    def get(self, source_id: str) -> Source | None:
        row = self._one("SELECT * FROM sources WHERE id = ?", (source_id,))
        return _source(row) if row else None

    def get_by_normalized_url(self, normalized_url: str) -> Source | None:
        row = self._one(
            "SELECT * FROM sources WHERE normalized_url = ?", (normalized_url,)
        )
        return _source(row) if row else None

    def list_by_ids(self, source_ids: Sequence[str]) -> list[Source]:
        """The sources with these ids, in the order asked; unknown ids are skipped."""
        if not source_ids:
            return []
        marks = ", ".join("?" for _ in source_ids)
        rows = self._all(
            f"SELECT * FROM sources WHERE id IN ({marks})", tuple(source_ids)
        )
        by_id = {row["id"]: _source(row) for row in rows}
        return [by_id[i] for i in dict.fromkeys(source_ids) if i in by_id]


def _source(row: sqlite3.Row) -> Source:
    return Source(
        id=row["id"],
        url=row["url"],
        final_url=row["final_url"],
        normalized_url=row["normalized_url"],
        title=row["title"],
        provider=row["provider"],
        published_at=parse_dt(row["published_at"]),
        retrieved_at=parse_dt(row["retrieved_at"]),
        evidence_notes=tuple(
            EvidenceNote(item["note"], item["quote"])
            for item in from_json(row["evidence_notes_json"])
        ),
    )
