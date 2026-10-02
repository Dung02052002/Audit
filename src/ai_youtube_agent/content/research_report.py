"""Research report (Prompt Pack v8, prompt #060), context C4 Research.

A ``ResearchReport`` is the structured result of one finished research
request. The rules were approved by the user on 2026-10-03:

- It is assembled from stored data only, with no language model and no new
  sentences: the request (queries, language, market, status, failures), the
  sources kept by deduplication, the scored topics and the claims.
- A ``ResearchClaim`` is an evidence note of a kept source: its verbatim quote
  when it has one, otherwise the note. Claims with near-duplicate text (word
  Jaccard >= 0.8) across sources are merged and their evidence combined. A
  claim lists the topics whose key phrases occur in it.
- Each claim has an ``Uncertainty``: low with at least 3 sources on different
  hosts and a verbatim quote; medium with 2 or more sources, or 1 source with
  a quote; high for 1 source with only a note. The report has an overall
  level with reasons: a partial or failed request, fewer than 3 kept sources,
  sources older than 365 days, or no claims.
- Research claims are their own type, with stable ids. The script stage
  (#065-#074) may later make B-017 ``Claim``/``Evidence`` that point to the
  same source ids; B-017 is not changed.
- The report is stored once per request as JSON with ``schema_version``;
  ``to_markdown`` renders a readable copy on demand.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

REPORT_SCHEMA_VERSION = 1
STALE_DAYS = 365
MIN_SOURCES = 3


class Uncertainty(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


def claim_id(request_id: str, text: str) -> str:
    """A stable id for a claim of a request."""
    digest = hashlib.sha256(f"{request_id}\n{text}".encode()).hexdigest()
    return f"clm_{digest[:24]}"


@dataclass(frozen=True)
class ClaimEvidence:
    source_id: str
    note: str
    quote: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"source_id": self.source_id, "note": self.note, "quote": self.quote}


@dataclass(frozen=True)
class ResearchClaim:
    id: str
    text: str
    evidence: tuple[ClaimEvidence, ...]
    topic_ids: tuple[str, ...]
    uncertainty: Uncertainty
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("claim text must not be empty")
        if not self.evidence:
            raise ValueError("a claim needs evidence")
        sources = [e.source_id for e in self.evidence]
        if len(set(sources)) != len(sources):
            raise ValueError("one evidence item per source")

    @property
    def source_ids(self) -> tuple[str, ...]:
        return tuple(e.source_id for e in self.evidence)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "evidence": [e.as_dict() for e in self.evidence],
            "topic_ids": list(self.topic_ids),
            "uncertainty": self.uncertainty.value,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class ReportSource:
    source_id: str
    title: str
    url: str
    host: str
    published_at: datetime | None
    retrieved_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "title": self.title,
            "url": self.url,
            "host": self.host,
            "published_at": _iso(self.published_at),
            "retrieved_at": _iso(self.retrieved_at),
        }


@dataclass(frozen=True)
class ReportTopic:
    topic_id: str
    label: str
    keyphrases: tuple[str, ...]
    score: float
    relevance: float
    novelty: float
    support: float
    source_ids: tuple[str, ...]
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "topic_id": self.topic_id,
            "label": self.label,
            "keyphrases": list(self.keyphrases),
            "score": self.score,
            "relevance": self.relevance,
            "novelty": self.novelty,
            "support": self.support,
            "source_ids": list(self.source_ids),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class ReportFailure:
    operation: str
    target: str
    code: str
    attempts: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "target": self.target,
            "code": self.code,
            "attempts": self.attempts,
        }


@dataclass(frozen=True)
class ResearchReport:
    id: str
    request_id: str
    channel_id: str
    queries: tuple[str, ...]
    language: str | None
    market: str | None
    request_status: str
    topics: tuple[ReportTopic, ...]
    claims: tuple[ResearchClaim, ...]
    sources: tuple[ReportSource, ...]
    failures: tuple[ReportFailure, ...]
    uncertainty: Uncertainty
    uncertainty_reasons: tuple[str, ...]
    generated_at: datetime
    schema_version: int = REPORT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        known = {s.source_id for s in self.sources}
        for claim in self.claims:
            if not set(claim.source_ids) <= known:
                raise ValueError("every claim source must be a report source")
        topic_ids = {t.topic_id for t in self.topics}
        for claim in self.claims:
            if not set(claim.topic_ids) <= topic_ids:
                raise ValueError("every claim topic must be a report topic")
        if self.schema_version != REPORT_SCHEMA_VERSION:
            raise ValueError(f"unknown report schema version {self.schema_version}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "request_id": self.request_id,
            "channel_id": self.channel_id,
            "queries": list(self.queries),
            "language": self.language,
            "market": self.market,
            "request_status": self.request_status,
            "topics": [t.as_dict() for t in self.topics],
            "claims": [c.as_dict() for c in self.claims],
            "sources": [s.as_dict() for s in self.sources],
            "failures": [f.as_dict() for f in self.failures],
            "uncertainty": self.uncertainty.value,
            "uncertainty_reasons": list(self.uncertainty_reasons),
            "generated_at": _iso(self.generated_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResearchReport":
        return cls(
            id=data["id"],
            request_id=data["request_id"],
            channel_id=data["channel_id"],
            queries=tuple(data["queries"]),
            language=data["language"],
            market=data["market"],
            request_status=data["request_status"],
            topics=tuple(
                ReportTopic(
                    t["topic_id"],
                    t["label"],
                    tuple(t["keyphrases"]),
                    t["score"],
                    t["relevance"],
                    t["novelty"],
                    t["support"],
                    tuple(t["source_ids"]),
                    tuple(t["reasons"]),
                )
                for t in data["topics"]
            ),
            claims=tuple(
                ResearchClaim(
                    c["id"],
                    c["text"],
                    tuple(
                        ClaimEvidence(e["source_id"], e["note"], e["quote"])
                        for e in c["evidence"]
                    ),
                    tuple(c["topic_ids"]),
                    Uncertainty(c["uncertainty"]),
                    tuple(c["reasons"]),
                )
                for c in data["claims"]
            ),
            sources=tuple(
                ReportSource(
                    s["source_id"],
                    s["title"],
                    s["url"],
                    s["host"],
                    _parse(s["published_at"]),
                    _parse(s["retrieved_at"]),
                )
                for s in data["sources"]
            ),
            failures=tuple(
                ReportFailure(f["operation"], f["target"], f["code"], f["attempts"])
                for f in data["failures"]
            ),
            uncertainty=Uncertainty(data["uncertainty"]),
            uncertainty_reasons=tuple(data["uncertainty_reasons"]),
            generated_at=_parse(data["generated_at"]),
            schema_version=data["schema_version"],
        )

    def to_markdown(self) -> str:
        number = {s.source_id: i for i, s in enumerate(self.sources, start=1)}
        labels = {t.topic_id: t.label for t in self.topics}
        lines = [
            "# Research report",
            "",
            f"- Request: {self.request_id}",
            f"- Queries: {', '.join(self.queries)}",
            f"- Language: {self.language or 'not set'}; "
            f"market: {self.market or 'not set'}",
            f"- Request status: {self.request_status}",
            f"- Generated: {_iso(self.generated_at)}",
            "",
            f"## Uncertainty: {self.uncertainty.value}",
            "",
        ]
        lines += [f"- {reason}" for reason in self.uncertainty_reasons] or [
            "- No concerns found."
        ]
        lines += ["", "## Topics", ""]
        if self.topics:
            lines += [
                "| Topic | Score | Relevance | Novelty | Support | Sources |",
                "|---|---|---|---|---|---|",
            ]
            for topic in self.topics:
                refs = " ".join(f"[{number[i]}]" for i in topic.source_ids)
                lines.append(
                    f"| {_cell(topic.label)} | {topic.score} | {topic.relevance} | "
                    f"{topic.novelty} | {topic.support} | {refs} |"
                )
        else:
            lines.append("No topics.")
        lines += ["", "## Claims", ""]
        if not self.claims:
            lines.append("No claims.")
        for claim in self.claims:
            refs = " ".join(f"[{number[i]}]" for i in claim.source_ids)
            topics = ", ".join(labels[t] for t in claim.topic_ids)
            lines.append(
                f'- "{claim.text}" {refs} (uncertainty: {claim.uncertainty.value}'
                + (f"; topics: {topics}" if topics else "")
                + ")"
            )
        lines += ["", "## Sources", ""]
        if not self.sources:
            lines.append("No sources.")
        for source in self.sources:
            published = (
                f", published {source.published_at.date().isoformat()}"
                if source.published_at
                else ""
            )
            lines.append(
                f"{number[source.source_id]}. {source.title} - {source.url}{published}"
            )
        if self.failures:
            lines += ["", "## Failures", ""]
            lines += [
                f"- {f.operation} {f.target}: {f.code} after {f.attempts} attempt(s)"
                for f in self.failures
            ]
        return "\n".join(lines) + "\n"


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def _parse(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def _cell(text: str) -> str:
    return text.replace("|", "\\|")
