"""Script Versioner (Prompt Pack v8, prompt #073), context C5.

``ScriptVersioner`` stores the revision of a script version: the diff metadata
of the version against its parent (``content/script_revision.py``, method
``script-diff-v1``, no AI model). The rules were approved by the user on
2026-10-04:

- A revision is made on demand (``record(script_id, actor=...)``), only for a
  version after the first, and only against its parent. An unknown script is
  ``ScriptNotFoundError`` (404); version 1, a version with no parent, a parent
  that does not exist or that belongs to another content item is
  ``ScriptInputError`` (422). Any actor may record a revision: it is derived
  data, and nothing in it is a decision.
- The diff is computed outside any write transaction; the revision and its
  entries are stored in one (``script_revisions`` and
  ``script_revision_sections``, migration 0022).
- One revision per script version, never recomputed: a second call returns the
  stored revision before anything is read or computed, without writing or
  auditing. When a concurrent call stored a revision first, the unique
  ``script_id`` refuses this one, and the stored revision is read and returned.
- ``script.revision_recorded`` is audited after commit with ids and counts only,
  never any script text.
- A revision is not related to approvals (they bind artifacts, not scripts: a
  new script version invalidates nothing until it is rendered into new
  artifacts) nor to the #068 to #072 runs of a version. There is no restore of
  an earlier version yet.
- ``history(content_item_id)`` reads, and writes nothing: the versions of the
  script of a content item in order, each with its revision summary when one
  is stored.
"""

import sqlite3
from collections.abc import Callable
from datetime import datetime

from ai_youtube_agent.content.claim_extractor import ScriptNotFoundError
from ai_youtube_agent.content.hook_generator import ContentItemNotFoundError
from ai_youtube_agent.content.script_generation import ScriptInputError
from ai_youtube_agent.content.script_revision import (
    ScriptHistoryEntry,
    ScriptRevision,
    diff_scripts,
)
from ai_youtube_agent.core.audit import Actor, AuditLog, AuditResult, EntityRef
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import (
    ContentItemRepository,
    ScriptRepository,
    ScriptRevisionRepository,
)

Clock = Callable[[], datetime]


class ScriptVersioner:
    def __init__(
        self, database: Database, audit: AuditLog, *, clock: Clock | None = None
    ) -> None:
        self._database = database
        self._audit = audit
        self._clock = clock

    def record(self, script_id: str, *, actor: Actor) -> ScriptRevision:
        with self._database.transaction() as connection:
            scripts = ScriptRepository(connection)
            script = scripts.get(script_id)
            if script is None:
                raise ScriptNotFoundError(f"script {script_id} does not exist")
            stored = ScriptRevisionRepository(connection).get_by_script(script_id)
            if stored is not None:
                return stored
            if script.version < 2 or script.parent_id is None:
                raise ScriptInputError(
                    f"script {script_id} is version {script.version}: "
                    "only a version after the first has a revision"
                )
            parent = scripts.get(script.parent_id)
            if parent is None or parent.content_item_id != script.content_item_id:
                raise ScriptInputError(
                    f"the parent of script {script_id} is not a version of "
                    "the same content item"
                )
        revision = ScriptRevision.create(
            script,
            parent,
            diff_scripts(parent, script),
            requested_by=actor,
            clock=self._clock,
        )
        try:
            with self._database.transaction() as connection:
                ScriptRevisionRepository(connection).add(revision)
        except sqlite3.IntegrityError:
            # Expected: a concurrent call for this script committed first. Any
            # other integrity error is hidden only if such a revision exists;
            # else it is raised again below.
            with self._database.transaction() as connection:
                stored = ScriptRevisionRepository(connection).get_by_script(script_id)
            if stored is None:
                raise
            return stored
        self._audit.record(
            "script.revision_recorded",
            actor,
            EntityRef("script", script.id),
            AuditResult.SUCCESS,
            {
                "script_revision_id": revision.id,
                "parent_script_id": parent.id,
                "content_item_id": script.content_item_id,
                "version": script.version,
                "method": revision.method,
                **revision.counts(),
            },
        )
        return revision

    def history(self, content_item_id: str) -> list[ScriptHistoryEntry]:
        with self._database.transaction() as connection:
            if ContentItemRepository(connection).get(content_item_id) is None:
                raise ContentItemNotFoundError(
                    f"content item {content_item_id} does not exist"
                )
            scripts = ScriptRepository(connection).list_by_content_item(content_item_id)
            revisions = {
                revision.script_id: revision
                for revision in ScriptRevisionRepository(
                    connection
                ).list_by_content_item(content_item_id)
            }
        return [
            ScriptHistoryEntry.of(script, revisions.get(script.id))
            for script in scripts
        ]
