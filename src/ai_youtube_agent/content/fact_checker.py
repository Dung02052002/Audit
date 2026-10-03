"""Fact Checker (Prompt Pack v8, prompt #070), context C5 Script & Fact Check.

``FactChecker`` gives every claim of one stored claim extraction run a PASS,
WARN or FAIL result from the stored evidence matching run (#069), by the
deterministic rules of ``content/fact_check.py``, with no AI model. The rules
were approved by the user on 2026-10-03:

- It runs on demand only (``check(script_id, actor=...)``), for a script
  version whose claims were extracted (#068) and matched (#069).
- The research report is the one of the matching run
  (``EvidenceMatch.research_report_id``); a matching run without one gives
  every claim a WARN. A report id whose report cannot be read raises
  ``ResearchReportNotFoundError`` (404).
- The run and its results are stored in one transaction (``fact_checks`` and
  ``fact_check_results``, migration 0019).
- One run per matching run: a second call returns the stored run without
  reading the report, writing or auditing. When a concurrent call stored a
  run first, the unique ``match_id`` refuses this one, and the stored run is
  read and returned.
- ``facts.checked`` is audited after commit with scalar counts only.
- An unknown script raises ``ScriptNotFoundError`` (404); a script version
  without a claim extraction raises ``ClaimsNotExtractedError`` (409); one
  without an evidence matching run raises ``EvidenceNotMatchedError`` (409).
- The result is a record only: no gate reads it yet, it is never changed, and
  nothing overrides it.
"""

import sqlite3
from collections.abc import Callable
from datetime import datetime
from http import HTTPStatus

from ai_youtube_agent.content.claim_extractor import ScriptNotFoundError
from ai_youtube_agent.content.evidence_matcher import ClaimsNotExtractedError
from ai_youtube_agent.content.fact_check import FactCheck, check_claims
from ai_youtube_agent.content.hook_generator import ResearchReportNotFoundError
from ai_youtube_agent.core.audit import Actor, AuditLog, AuditResult, EntityRef
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import (
    ClaimExtractionRepository,
    EvidenceMatchRepository,
    FactCheckRepository,
    ScriptRepository,
)
from ai_youtube_agent.core.db.repositories.research import ResearchReportRepository
from ai_youtube_agent.core.errors import DomainError

Clock = Callable[[], datetime]


class EvidenceNotMatchedError(DomainError):
    default_code = "domain.evidence_not_matched"
    default_user_message = "Match the evidence of this script version first."
    default_http_status = HTTPStatus.CONFLICT


class FactChecker:
    def __init__(
        self, database: Database, audit: AuditLog, *, clock: Clock | None = None
    ) -> None:
        self._database = database
        self._audit = audit
        self._clock = clock

    def check(self, script_id: str, *, actor: Actor) -> FactCheck:
        report = None
        with self._database.transaction() as connection:
            script = ScriptRepository(connection).get(script_id)
            if script is None:
                raise ScriptNotFoundError(f"script {script_id} does not exist")
            extraction = ClaimExtractionRepository(connection).get_by_script(script_id)
            if extraction is None:
                raise ClaimsNotExtractedError(
                    f"script {script_id} has no claim extraction"
                )
            match = EvidenceMatchRepository(connection).get_by_extraction(extraction.id)
            if match is None:
                raise EvidenceNotMatchedError(
                    f"script {script_id} has no evidence matching run"
                )
            stored = FactCheckRepository(connection).get_by_match(match.id)
            if stored is None and match.research_report_id is not None:
                report = ResearchReportRepository(connection).get(
                    match.research_report_id
                )
                if report is None:
                    raise ResearchReportNotFoundError(
                        f"research report {match.research_report_id} does not exist"
                    )
        if stored is not None:
            return stored
        fact_check = FactCheck.create(
            extraction,
            match,
            check_claims(extraction.claims, match, report),
            requested_by=actor,
            clock=self._clock,
        )
        try:
            with self._database.transaction() as connection:
                FactCheckRepository(connection).add(fact_check)
        except sqlite3.IntegrityError:
            # Expected: a concurrent run for this matching run committed first.
            # Any other integrity error is hidden only if such a run exists;
            # else it is raised again below.
            with self._database.transaction() as connection:
                stored = FactCheckRepository(connection).get_by_match(match.id)
            if stored is None:
                raise
            return stored
        self._audit.record(
            "facts.checked",
            actor,
            EntityRef("script", script.id),
            AuditResult.SUCCESS,
            {
                "fact_check_id": fact_check.id,
                "evidence_match_id": match.id,
                "claim_extraction_id": extraction.id,
                "content_item_id": script.content_item_id,
                "version": script.version,
                "research_report_id": fact_check.research_report_id,
                "method": fact_check.method,
                **fact_check.counts(),
            },
        )
        return fact_check
