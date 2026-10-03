"""Evidence Matcher (Prompt Pack v8, prompt #069), context C5 Script & Fact Check.

``EvidenceMatcher`` links the claims of one stored claim extraction run to
the evidence of the script version's research report, by the deterministic
rules of ``content/evidence_matching.py``, with no AI model. The rules were
approved by the user on 2026-10-03:

- It runs on demand only (``match(script_id, actor=...)``), for a script
  version whose claims were extracted (#068); only the claims of that
  extraction run are matched.
- The pool is the research report in ``Script.research_report_id`` only. A
  script version without one still gets a run, with no report id, no link
  and every claim unmatched. A report id whose report cannot be read raises
  ``ResearchReportNotFoundError`` (404).
- The run and its links (``Evidence`` rows with ``match_id``,
  ``research_claim_id``, ``score`` and ``numbers``; ``source_ref`` stays a
  ``Source`` id) are stored in one transaction (``evidence_matches``,
  migration 0018), also when nothing matched.
- One run per extraction run: a second call returns the stored run without
  writing or auditing. When a concurrent call stored a run first, the unique
  ``extraction_id`` refuses this one, and the stored run is read and
  returned.
- ``evidence.matched`` is audited after commit with scalar counts only.
- An unknown script raises ``ScriptNotFoundError`` (404); a script version
  without a claim extraction raises ``ClaimsNotExtractedError`` (409).
- Differing numbers are recorded on the link (``numbers`` = ``differ``), with
  no verdict; the Fact Check Result (#070) owns verdicts.
"""

import sqlite3
from collections.abc import Callable
from datetime import datetime
from http import HTTPStatus

from ai_youtube_agent.content.claim_extractor import ScriptNotFoundError
from ai_youtube_agent.content.evidence_matching import EvidenceMatch, match_claims
from ai_youtube_agent.content.hook_generator import ResearchReportNotFoundError
from ai_youtube_agent.core.audit import Actor, AuditLog, AuditResult, EntityRef
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import (
    ClaimExtractionRepository,
    EvidenceMatchRepository,
    ScriptRepository,
)
from ai_youtube_agent.core.db.repositories.research import ResearchReportRepository
from ai_youtube_agent.core.errors import DomainError

Clock = Callable[[], datetime]


class ClaimsNotExtractedError(DomainError):
    default_code = "domain.claims_not_extracted"
    default_user_message = "Extract the claims of this script version first."
    default_http_status = HTTPStatus.CONFLICT


class EvidenceMatcher:
    def __init__(
        self, database: Database, audit: AuditLog, *, clock: Clock | None = None
    ) -> None:
        self._database = database
        self._audit = audit
        self._clock = clock

    def match(self, script_id: str, *, actor: Actor) -> EvidenceMatch:
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
            stored = EvidenceMatchRepository(connection).get_by_extraction(
                extraction.id
            )
            if stored is None and script.research_report_id is not None:
                report = ResearchReportRepository(connection).get(
                    script.research_report_id
                )
                if report is None:
                    raise ResearchReportNotFoundError(
                        f"research report {script.research_report_id} does not exist"
                    )
        if stored is not None:
            return stored
        match = EvidenceMatch.create(
            extraction,
            script.research_report_id,
            match_claims(extraction.claims, report),
            requested_by=actor,
            clock=self._clock,
        )
        try:
            with self._database.transaction() as connection:
                EvidenceMatchRepository(connection).add(match)
        except sqlite3.IntegrityError:
            # Expected: a concurrent run for this extraction committed first.
            # Any other integrity error is hidden only if such a run exists;
            # else it is raised again below.
            with self._database.transaction() as connection:
                stored = EvidenceMatchRepository(connection).get_by_extraction(
                    extraction.id
                )
            if stored is None:
                raise
            return stored
        self._audit.record(
            "evidence.matched",
            actor,
            EntityRef("script", script.id),
            AuditResult.SUCCESS,
            {
                "evidence_match_id": match.id,
                "claim_extraction_id": extraction.id,
                "content_item_id": script.content_item_id,
                "version": script.version,
                "research_report_id": match.research_report_id,
                "method": match.method,
                **match.counts(),
            },
        )
        return match
