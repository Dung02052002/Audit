"""Originality Checker (Prompt Pack v8, prompt #071), context C5.

``OriginalityChecker`` looks for excessive reuse and repetitive structure in one
stored script version, against the prior content of its channel, by the
deterministic rules of ``content/originality.py``, with no AI model. The rules
were approved by the user on 2026-10-03:

- It runs on demand only (``check(script_id, actor=...)``), for any stored
  script version. It needs nothing else: no claim extraction, evidence matching
  or fact check run (#068 to #070).
- Prior content is the latest version of every other content item of the same
  channel, among the scripts created strictly before this one, the newest 50
  (``MAX_PRIORS``), whatever the status of the item
  (``ScriptRepository.list_prior_latest``). A script is compared with content
  that already existed when it was made, so checking an older script never
  flags it for what came after.
- The rules run outside any write transaction; the run and its findings are
  stored in one transaction (``originality_checks`` and
  ``originality_findings``, migration 0020).
- One run per script version, never recomputed: a second call returns the
  stored run without reading the priors again, writing or auditing. When a
  concurrent call stored a run first, the unique ``script_id`` refuses this
  one, and the stored run is read and returned.
- ``originality.checked`` is audited after commit with ids and counts only,
  never any script text.
- An unknown script raises ``ScriptNotFoundError`` (404).
- The result is a record only: no gate reads it yet, it is never changed, and
  nothing overrides it.
"""

import sqlite3
from collections.abc import Callable
from datetime import datetime

from ai_youtube_agent.content.claim_extractor import ScriptNotFoundError
from ai_youtube_agent.content.originality import (
    MAX_PRIORS,
    OriginalityCheck,
    check_originality,
)
from ai_youtube_agent.core.audit import Actor, AuditLog, AuditResult, EntityRef
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import (
    ContentItemRepository,
    OriginalityRepository,
    ScriptRepository,
)

Clock = Callable[[], datetime]


class OriginalityChecker:
    def __init__(
        self, database: Database, audit: AuditLog, *, clock: Clock | None = None
    ) -> None:
        self._database = database
        self._audit = audit
        self._clock = clock

    def check(self, script_id: str, *, actor: Actor) -> OriginalityCheck:
        with self._database.transaction() as connection:
            scripts = ScriptRepository(connection)
            script = scripts.get(script_id)
            if script is None:
                raise ScriptNotFoundError(f"script {script_id} does not exist")
            stored = OriginalityRepository(connection).get_by_script(script_id)
            if stored is not None:
                return stored
            item = ContentItemRepository(connection).get(script.content_item_id)
            priors = scripts.list_prior_latest(
                item.channel_id, item.id, script.created_at, MAX_PRIORS
            )
        check = OriginalityCheck.create(
            script,
            check_originality(script, priors),
            requested_by=actor,
            clock=self._clock,
        )
        try:
            with self._database.transaction() as connection:
                OriginalityRepository(connection).add(check)
        except sqlite3.IntegrityError:
            # Expected: a concurrent run for this script committed first. Any
            # other integrity error is hidden only if such a run exists; else
            # it is raised again below.
            with self._database.transaction() as connection:
                stored = OriginalityRepository(connection).get_by_script(script_id)
            if stored is None:
                raise
            return stored
        self._audit.record(
            "originality.checked",
            actor,
            EntityRef("script", script.id),
            AuditResult.SUCCESS,
            {
                "originality_check_id": check.id,
                "content_item_id": script.content_item_id,
                "version": script.version,
                "method": check.method,
                **check.counts(),
            },
        )
        return check
