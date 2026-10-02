"""Research Report generator (Prompt Pack v8, prompt #060), context C4.

``ResearchReportGenerator`` assembles the ``ResearchReport`` of a finished
request from stored data (see ``content/research_report.py`` for the rules)
and stores it once; later calls return the stored report.
"""

import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from ai_youtube_agent.content.research_report import (
    MIN_SOURCES,
    STALE_DAYS,
    ClaimEvidence,
    ReportFailure,
    ReportSource,
    ReportTopic,
    ResearchClaim,
    ResearchReport,
    Uncertainty,
    claim_id,
)
from ai_youtube_agent.content.research_request import (
    ResearchRequest,
    ResearchRequestStateError,
    ResearchStatus,
)
from ai_youtube_agent.content.similarity import SHORT_TEXT_THRESHOLD, word_jaccard
from ai_youtube_agent.content.source import Source
from ai_youtube_agent.content.source_collector import ResearchRequestNotFoundError
from ai_youtube_agent.content.source_dedup import SourceDeduplicator
from ai_youtube_agent.content.topic import Topic
from ai_youtube_agent.content.topic_extractor import phrases
from ai_youtube_agent.content.topic_scorer import TopicScorer
from ai_youtube_agent.content.topic_scoring import TopicScoring
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.research import (
    ResearchReportRepository,
    ResearchRequestRepository,
    SourceRepository,
    TopicRepository,
)

Clock = Callable[[], datetime]


def _host(url: str) -> str:
    return urlsplit(url).netloc


def build_claims(
    request_id: str, sources: Sequence[Source], topics: Sequence[Topic]
) -> tuple[ResearchClaim, ...]:
    """Claims from the evidence notes of ``sources`` (kept, in collection order)."""
    groups: list[tuple[str, list[ClaimEvidence]]] = []
    for source in sources:
        for note in source.evidence_notes:
            text = note.quote or note.note
            evidence = ClaimEvidence(source.id, note.note, note.quote)
            group = next(
                (g for g in groups if word_jaccard(g[0], text) >= SHORT_TEXT_THRESHOLD),
                None,
            )
            if group is None:
                groups.append((text, [evidence]))
            elif all(e.source_id != source.id for e in group[1]):
                group[1].append(evidence)
    hosts = {source.id: _host(source.normalized_url) for source in sources}
    claims = []
    for text, evidence in groups:
        found = set().union(*(phrases(f"{e.quote or ''} {e.note}") for e in evidence))
        topic_ids = tuple(t.id for t in topics if any(p in found for p in t.keyphrases))
        level, reasons = _claim_uncertainty(evidence, hosts)
        claims.append(
            ResearchClaim(
                claim_id(request_id, text),
                text,
                tuple(evidence),
                topic_ids,
                level,
                reasons,
            )
        )
    return tuple(claims)


def _claim_uncertainty(
    evidence: list[ClaimEvidence], hosts: dict[str, str]
) -> tuple[Uncertainty, tuple[str, ...]]:
    count = len(evidence)
    distinct_hosts = len({hosts[e.source_id] for e in evidence})
    quoted = any(e.quote for e in evidence)
    reasons = (
        f"{count} source(s) on {distinct_hosts} host(s)",
        "has a verbatim quote" if quoted else "no verbatim quote, only a note",
    )
    if distinct_hosts >= MIN_SOURCES and quoted:
        return Uncertainty.LOW, reasons
    if count >= 2 or quoted:
        return Uncertainty.MEDIUM, reasons
    return Uncertainty.HIGH, reasons


def report_uncertainty(
    request: ResearchRequest,
    sources: Sequence[Source],
    claims: Sequence[ResearchClaim],
    now: datetime,
) -> tuple[Uncertainty, tuple[str, ...]]:
    reasons = []
    if request.status is ResearchStatus.FAILED:
        reasons.append("the research request failed")
    elif request.status is ResearchStatus.PARTIAL:
        reasons.append(f"{len(request.failures)} search or fetch step(s) failed")
    if len(sources) < MIN_SOURCES:
        reasons.append(f"only {len(sources)} source(s) kept, fewer than {MIN_SOURCES}")
    stale = [
        s
        for s in sources
        if s.published_at and now - s.published_at > timedelta(days=STALE_DAYS)
    ]
    if stale:
        reasons.append(f"{len(stale)} source(s) older than {STALE_DAYS} days")
    if not claims:
        reasons.append("no claims: the sources have no evidence notes")
    if request.status is ResearchStatus.FAILED or not sources or not claims:
        return Uncertainty.HIGH, tuple(reasons)
    if reasons:
        return Uncertainty.MEDIUM, tuple(reasons)
    return Uncertainty.LOW, ()


def build_report(
    request: ResearchRequest,
    sources: Sequence[Source],
    topics: Sequence[Topic],
    scoring: TopicScoring,
    now: datetime,
) -> ResearchReport:
    by_topic = {topic.id: topic for topic in topics}
    claims = build_claims(request.id, sources, topics)
    level, reasons = report_uncertainty(request, sources, claims, now)
    return ResearchReport(
        id=uuid.uuid4().hex,
        request_id=request.id,
        channel_id=request.channel_id,
        queries=request.queries,
        language=request.language,
        market=request.market,
        request_status=request.status.value,
        topics=tuple(
            ReportTopic(
                s.topic_id,
                s.label,
                by_topic[s.topic_id].keyphrases,
                s.score,
                s.relevance,
                s.novelty,
                s.support,
                by_topic[s.topic_id].source_ids,
                s.reasons,
            )
            for s in scoring.scores
        ),
        claims=claims,
        sources=tuple(
            ReportSource(
                s.id,
                s.title,
                s.normalized_url,
                _host(s.normalized_url),
                s.published_at,
                s.retrieved_at,
            )
            for s in sources
        ),
        failures=tuple(
            ReportFailure(f.operation.value, f.target, f.code, f.attempts)
            for f in request.failures
        ),
        uncertainty=level,
        uncertainty_reasons=reasons,
        generated_at=now,
    )


class ResearchReportGenerator:
    def __init__(
        self,
        database: Database,
        deduplicator: SourceDeduplicator,
        scorer: TopicScorer,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._deduplicator = deduplicator
        self._scorer = scorer
        self._clock = clock

    def generate(self, request_id: str) -> ResearchReport:
        with self._database.transaction() as connection:
            request = ResearchRequestRepository(connection).get(request_id)
            if request is None:
                raise ResearchRequestNotFoundError(
                    f"research request {request_id} does not exist"
                )
            if not request.is_finished:
                raise ResearchRequestStateError(
                    f"research request {request_id} is {request.status.value}, "
                    "not finished"
                )
            stored = ResearchReportRepository(connection).get_by_request(request_id)
        if stored is not None:
            return stored
        kept = self._deduplicator.deduplicate(request_id).kept
        scoring = self._scorer.score(request_id)
        with self._database.transaction() as connection:
            sources = SourceRepository(connection).list_by_ids(kept)
            extraction = TopicRepository(connection).get(request_id)
            topics = extraction.topics if extraction else ()
            now = self._clock() if self._clock else datetime.now(UTC)
            report = build_report(request, sources, topics, scoring, now)
            ResearchReportRepository(connection).add(report)
        return report
