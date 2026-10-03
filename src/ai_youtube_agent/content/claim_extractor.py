"""Claim Extractor (Prompt Pack v8, prompt #068), context C5 Script & Fact Check.

``ClaimExtractor`` finds the factual claims of one stored script version by
the deterministic rules of ``content/claim_extraction.py``, with no AI model.
The rules were approved by the user on 2026-10-03:

- It runs on demand only (``extract(script_id, actor=...)``), for any stored
  version. Each version gets its own extraction; claims are not linked to the
  research report's claims (that is evidence matching, #069).
- The run and its claims (``Claim`` with ``section_index``, ``kind`` and
  ``extraction_id``, text verbatim) are stored in one transaction
  (``claim_extractions``, migration 0017), also when no claim was found.
- One run per script version: a second call returns the stored run without
  writing or auditing. When a concurrent call stored a run first, the unique
  ``script_id`` refuses this one, and the stored run is read and returned.
- ``claims.extracted`` is audited after commit with scalar counts only.
- An unknown script raises ``ScriptNotFoundError`` (404).
"""

import sqlite3
from collections.abc import Callable
from datetime import datetime
from http import HTTPStatus

from ai_youtube_agent.content.claim_extraction import (
    ClaimExtraction,
    extract_claims,
)
from ai_youtube_agent.core.audit import Actor, AuditLog, AuditResult, EntityRef
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import (
    ClaimExtractionRepository,
    ScriptRepository,
)
from ai_youtube_agent.core.errors import DomainError

Clock = Callable[[], datetime]


class ScriptNotFoundError(DomainError):
    default_code = "domain.script_not_found"
    default_user_message = "This script does not exist."
    default_http_status = HTTPStatus.NOT_FOUND


class ClaimExtractor:
    def __init__(
        self, database: Database, audit: AuditLog, *, clock: Clock | None = None
    ) -> None:
        self._database = database
        self._audit = audit
        self._clock = clock

    def extract(self, script_id: str, *, actor: Actor) -> ClaimExtraction:
        with self._database.transaction() as connection:
            script = ScriptRepository(connection).get(script_id)
            if script is None:
                raise ScriptNotFoundError(f"script {script_id} does not exist")
            stored = ClaimExtractionRepository(connection).get_by_script(script_id)
        if stored is not None:
            return stored
        extraction = ClaimExtraction.create(
            script, extract_claims(script), requested_by=actor, clock=self._clock
        )
        try:
            with self._database.transaction() as connection:
                ClaimExtractionRepository(connection).add(extraction)
        except sqlite3.IntegrityError:
            # Expected: a concurrent run for this script committed first. Any
            # other integrity error is hidden only if such a run exists; else
            # it is raised again below.
            with self._database.transaction() as connection:
                stored = ClaimExtractionRepository(connection).get_by_script(script_id)
            if stored is None:
                raise
            return stored
        self._audit.record(
            "claims.extracted",
            actor,
            EntityRef("script", script.id),
            AuditResult.SUCCESS,
            {
                "claim_extraction_id": extraction.id,
                "content_item_id": script.content_item_id,
                "version": script.version,
                "method": extraction.method,
                **extraction.counts(),
            },
        )
        return extraction
