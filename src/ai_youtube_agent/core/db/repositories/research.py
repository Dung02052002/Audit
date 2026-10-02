"""Repositories for research sources and requests (C4, #055, #056).

Sources are immutable records: add only. One source per normalised URL;
``add_or_get`` returns the stored source when the page is already known.
Research requests change through optimistic ``update`` calls; the links to
collected sources are added when the request finishes and never removed.
"""

import sqlite3
from collections.abc import Sequence
from datetime import datetime

from ai_youtube_agent.content.research_request import (
    CollectedSource,
    CollectionFailure,
    CollectionOperation,
    ResearchLimits,
    ResearchRequest,
    ResearchStatus,
)
from ai_youtube_agent.content.source import (
    DuplicateReason,
    EvidenceNote,
    Source,
    SourceDuplicate,
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
                "content_fingerprint": source.content_fingerprint,
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
        content_fingerprint=row["content_fingerprint"],
    )


class ResearchRequestRepository(Repository):
    table = "research_requests"

    def add(self, request: ResearchRequest) -> None:
        self._insert(self.table, {"id": request.id, **_request_row(request)})
        self._add_links(request)

    def get(self, request_id: str) -> ResearchRequest | None:
        row = self._one("SELECT * FROM research_requests WHERE id = ?", (request_id,))
        return self._request(row) if row else None

    def list_by_channel(self, channel_id: str) -> list[ResearchRequest]:
        rows = self._all(
            "SELECT * FROM research_requests WHERE channel_id = ? "
            "ORDER BY created_at, id",
            (channel_id,),
        )
        return [self._request(row) for row in rows]

    def update(
        self, request: ResearchRequest, *, expected_updated_at: datetime
    ) -> None:
        self._update(
            request.id,
            _request_row(request),
            guard_column="updated_at",
            guard_value=dt(expected_updated_at),
        )
        self._add_links(request)

    def _add_links(self, request: ResearchRequest) -> None:
        stored = {
            row["source_id"]
            for row in self._all(
                "SELECT source_id FROM research_request_sources WHERE request_id = ?",
                (request.id,),
            )
        }
        for position, item in enumerate(request.collected, start=1):
            if item.source_id in stored:
                continue
            self._insert(
                "research_request_sources",
                {
                    "request_id": request.id,
                    "source_id": item.source_id,
                    "position": position,
                    "query": item.query,
                    "rank": item.rank,
                },
            )

    def _request(self, row: sqlite3.Row) -> ResearchRequest:
        links = self._all(
            "SELECT source_id, query, rank FROM research_request_sources "
            "WHERE request_id = ? ORDER BY position",
            (row["id"],),
        )
        return ResearchRequest(
            id=row["id"],
            channel_id=row["channel_id"],
            queries=tuple(from_json(row["queries_json"])),
            language=row["language"],
            market=row["market"],
            limits=ResearchLimits(
                row["max_sources"], row["max_results_per_query"], row["max_per_domain"]
            ),
            status=ResearchStatus(row["status"]),
            requested_by=actor_from(row, "requested_by"),
            created_at=parse_dt(row["created_at"]),
            updated_at=parse_dt(row["updated_at"]),
            started_at=parse_dt(row["started_at"]),
            finished_at=parse_dt(row["finished_at"]),
            collected=tuple(
                CollectedSource(link["source_id"], link["query"], link["rank"])
                for link in links
            ),
            failures=tuple(
                CollectionFailure(
                    CollectionOperation(item["operation"]),
                    item["target"],
                    item["code"],
                    item["attempts"],
                )
                for item in from_json(row["failures_json"])
            ),
        )


def _request_row(request: ResearchRequest) -> dict:
    return {
        "channel_id": request.channel_id,
        "queries_json": to_json(list(request.queries)),
        "language": request.language,
        "market": request.market,
        "max_sources": request.limits.max_sources,
        "max_results_per_query": request.limits.max_results_per_query,
        "max_per_domain": request.limits.max_per_domain,
        "status": request.status.value,
        **actor_columns("requested_by", request.requested_by),
        "created_at": dt(request.created_at),
        "updated_at": dt(request.updated_at),
        "started_at": dt(request.started_at),
        "finished_at": dt(request.finished_at),
        "failures_json": to_json([f.as_dict() for f in request.failures]),
    }


class SourceDuplicateRepository(Repository):
    """Deduplication results (#057): written once per request, never changed."""

    table = "source_duplicates"

    def add(
        self,
        request_id: str,
        duplicates: Sequence[SourceDuplicate],
        deduplicated_at: datetime,
    ) -> None:
        self._insert(
            "source_deduplications",
            {"request_id": request_id, "deduplicated_at": dt(deduplicated_at)},
        )
        for duplicate in duplicates:
            self._insert(
                self.table,
                {
                    "request_id": request_id,
                    "source_id": duplicate.source_id,
                    "duplicate_of": duplicate.duplicate_of,
                    "reason": duplicate.reason.value,
                    "similarity": duplicate.similarity,
                },
            )

    def get(
        self, request_id: str
    ) -> tuple[datetime, tuple[SourceDuplicate, ...]] | None:
        """When the request was deduplicated and its duplicates, or None."""
        row = self._one(
            "SELECT deduplicated_at FROM source_deduplications WHERE request_id = ?",
            (request_id,),
        )
        if row is None:
            return None
        rows = self._all(
            "SELECT * FROM source_duplicates WHERE request_id = ? ORDER BY rowid",
            (request_id,),
        )
        return parse_dt(row["deduplicated_at"]), tuple(
            SourceDuplicate(
                r["source_id"],
                r["duplicate_of"],
                DuplicateReason(r["reason"]),
                r["similarity"],
            )
            for r in rows
        )
