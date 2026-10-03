"""Repositories for research sources, requests, topics, scores and reports (C4).

Sources are immutable records: add only. One source per normalised URL;
``add_or_get`` returns the stored source when the page is already known.
Research requests change through optimistic ``update`` calls; the links to
collected sources are added as the request saves its progress (#062) and
never removed.
"""

import sqlite3
from collections.abc import Sequence
from datetime import datetime

from ai_youtube_agent.content.research_report import ResearchReport
from ai_youtube_agent.content.research_request import (
    CollectedSource,
    CollectionFailure,
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
from ai_youtube_agent.content.topic import (
    EvidenceField,
    Topic,
    TopicEvidence,
    TopicExtraction,
)
from ai_youtube_agent.content.topic_scoring import TopicScore, TopicScoring
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

    def has_results(self, request_id: str) -> bool:
        """Whether deduplication, topics, scores or a report are stored for it."""
        row = self._one(
            "SELECT EXISTS (SELECT 1 FROM source_deduplications WHERE request_id = ?)"
            " OR EXISTS (SELECT 1 FROM topic_extractions WHERE request_id = ?)"
            " OR EXISTS (SELECT 1 FROM topic_scorings WHERE request_id = ?)"
            " OR EXISTS (SELECT 1 FROM research_reports WHERE request_id = ?)"
            " AS found",
            (request_id,) * 4,
        )
        return bool(row["found"])

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
                CollectionFailure.from_dict(item)
                for item in from_json(row["failures_json"])
            ),
            queries_done=row["queries_done"],
            lease_expires_at=parse_dt(row["lease_expires_at"]),
            retries=row["retries"],
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
        "queries_done": request.queries_done,
        "lease_expires_at": dt(request.lease_expires_at),
        "retries": request.retries,
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


class TopicRepository(Repository):
    """Topic extractions (#058): written once per request, never changed."""

    table = "research_topics"

    def add(self, extraction: TopicExtraction) -> None:
        self._insert(
            "topic_extractions",
            {
                "request_id": extraction.request_id,
                "extracted_at": dt(extraction.extracted_at),
            },
        )
        for topic in extraction.topics:
            self._insert(
                self.table,
                {
                    "id": topic.id,
                    "request_id": topic.request_id,
                    "rank": topic.rank,
                    "label": topic.label,
                    "keyphrases_json": to_json(list(topic.keyphrases)),
                },
            )
            for position, item in enumerate(topic.evidence, start=1):
                self._insert(
                    "topic_evidence",
                    {
                        "topic_id": topic.id,
                        "source_id": item.source_id,
                        "position": position,
                        "field": item.field.value,
                        "text": item.text,
                    },
                )

    def get(self, request_id: str) -> TopicExtraction | None:
        row = self._one(
            "SELECT extracted_at FROM topic_extractions WHERE request_id = ?",
            (request_id,),
        )
        if row is None:
            return None
        topics = tuple(
            self._topic(topic)
            for topic in self._all(
                "SELECT * FROM research_topics WHERE request_id = ? ORDER BY rank",
                (request_id,),
            )
        )
        return TopicExtraction(request_id, topics, parse_dt(row["extracted_at"]))

    def _topic(self, row: sqlite3.Row) -> Topic:
        evidence = self._all(
            "SELECT * FROM topic_evidence WHERE topic_id = ? ORDER BY position",
            (row["id"],),
        )
        return Topic(
            id=row["id"],
            request_id=row["request_id"],
            rank=row["rank"],
            label=row["label"],
            keyphrases=tuple(from_json(row["keyphrases_json"])),
            evidence=tuple(
                TopicEvidence(e["source_id"], EvidenceField(e["field"]), e["text"])
                for e in evidence
            ),
        )


class TopicScoreRepository(Repository):
    """Topic scores (#059): written once per request, never changed."""

    table = "topic_scores"

    def add(self, scoring: TopicScoring) -> None:
        self._insert(
            "topic_scorings",
            {
                "request_id": scoring.request_id,
                "scored_at": dt(scoring.scored_at),
                "strategy_version": scoring.strategy_version,
                "missing_inputs_json": to_json(list(scoring.missing_inputs)),
            },
        )
        for position, score in enumerate(scoring.scores, start=1):
            self._insert(
                self.table,
                {
                    "topic_id": score.topic_id,
                    "request_id": scoring.request_id,
                    "position": position,
                    "relevance": score.relevance,
                    "novelty": score.novelty,
                    "support": score.support,
                    "score": score.score,
                    "topic_words_json": to_json(list(score.topic_words)),
                    "matched_words_json": to_json(list(score.matched_words)),
                    "seen_in_json": to_json(list(score.seen_in)),
                    "support_count": score.support_count,
                    "kept_sources": score.kept_sources,
                },
            )

    def get(self, request_id: str) -> TopicScoring | None:
        row = self._one(
            "SELECT * FROM topic_scorings WHERE request_id = ?", (request_id,)
        )
        if row is None:
            return None
        rows = self._all(
            "SELECT s.*, t.label FROM topic_scores s "
            "JOIN research_topics t ON t.id = s.topic_id "
            "WHERE s.request_id = ? ORDER BY s.position",
            (request_id,),
        )
        return TopicScoring(
            request_id=request_id,
            scores=tuple(
                TopicScore(
                    topic_id=r["topic_id"],
                    label=r["label"],
                    relevance=r["relevance"],
                    novelty=r["novelty"],
                    support=r["support"],
                    score=r["score"],
                    topic_words=tuple(from_json(r["topic_words_json"])),
                    matched_words=tuple(from_json(r["matched_words_json"])),
                    seen_in=tuple(from_json(r["seen_in_json"])),
                    support_count=r["support_count"],
                    kept_sources=r["kept_sources"],
                )
                for r in rows
            ),
            strategy_version=row["strategy_version"],
            missing_inputs=tuple(from_json(row["missing_inputs_json"])),
            scored_at=parse_dt(row["scored_at"]),
        )


class ResearchReportRepository(Repository):
    """Research reports (#060): one per request, written once, never changed."""

    table = "research_reports"

    def add(self, report: ResearchReport) -> None:
        self._insert(
            self.table,
            {
                "id": report.id,
                "request_id": report.request_id,
                "channel_id": report.channel_id,
                "schema_version": report.schema_version,
                "uncertainty": report.uncertainty.value,
                "report_json": to_json(report.as_dict()),
                "generated_at": dt(report.generated_at),
            },
        )

    def get(self, report_id: str) -> ResearchReport | None:
        row = self._one("SELECT * FROM research_reports WHERE id = ?", (report_id,))
        return ResearchReport.from_dict(from_json(row["report_json"])) if row else None

    def get_by_request(self, request_id: str) -> ResearchReport | None:
        row = self._one(
            "SELECT * FROM research_reports WHERE request_id = ?", (request_id,)
        )
        return ResearchReport.from_dict(from_json(row["report_json"])) if row else None


class ResearchCacheRepository(Repository):
    """Cached provider answers (#061): upserted per key, never deleted."""

    table = "research_cache"

    def get(self, kind: str, key: str) -> tuple[dict, datetime] | None:
        row = self._one(
            "SELECT payload_json, cached_at FROM research_cache "
            "WHERE kind = ? AND key = ?",
            (kind, key),
        )
        if row is None:
            return None
        return from_json(row["payload_json"]), parse_dt(row["cached_at"])

    def put(
        self, kind: str, key: str, provider: str, payload: dict, cached_at: datetime
    ) -> None:
        self.connection.execute(
            "INSERT INTO research_cache (kind, key, provider, payload_json, cached_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (kind, key) DO UPDATE SET "
            "provider = excluded.provider, payload_json = excluded.payload_json, "
            "cached_at = excluded.cached_at",
            (kind, key, provider, to_json(payload), dt(cached_at)),
        )
