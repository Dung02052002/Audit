"""Script Validator (Prompt Pack v8, prompt #072), context C5.

``ScriptValidator`` checks one stored script version against the length,
section, language and content-type rules of ``content/script_validation.py``
and the current strategy of its channel, with no AI model. The rules were
approved by the user on 2026-10-04:

- It runs on demand only (``validate(script_id, actor=...)``), for any stored
  script version, whatever the status of its content item and whether or not
  ``LONGFORM_ENABLED`` is on. It needs nothing else: no claim extraction,
  evidence matching, fact check or originality run (#068 to #071), and it does
  not read their results.
- It needs the channel's strategy, with a primary language and a format, as the
  script generators do: an unknown script raises ``ScriptNotFoundError`` (404),
  a channel without a strategy ``StrategyNotFoundError`` (404), a strategy
  without a language or a format ``ScriptInputError`` (422).
- The rules run outside any write transaction; the run and its findings are
  stored in one (``script_validations`` and ``script_validation_findings``,
  migration 0021).
- One run per script version, never recomputed: a second call returns the
  stored run without loading the strategy, writing or auditing, even when the
  strategy changed since. When a concurrent call stored a run first, the
  unique ``script_id`` refuses this one, and the stored run is read and
  returned.
- ``script.validated`` is audited after commit with ids and counts only, never
  any script text.
- The result is a record only: no gate reads it yet, it is never changed, and
  nothing overrides it.
"""

import sqlite3
from collections.abc import Callable
from datetime import datetime

from ai_youtube_agent.content.claim_extractor import ScriptNotFoundError
from ai_youtube_agent.content.hook_generator import ContentItemNotFoundError
from ai_youtube_agent.content.script_generation import ScriptInputError
from ai_youtube_agent.content.script_validation import (
    ScriptValidation,
    validate_script,
)
from ai_youtube_agent.content.strategy_settings import StrategyNotFoundError
from ai_youtube_agent.core.audit import Actor, AuditLog, AuditResult, EntityRef
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import StrategyProfileRepository
from ai_youtube_agent.core.db.repositories.content import (
    ContentItemRepository,
    ScriptRepository,
    ScriptValidationRepository,
)

Clock = Callable[[], datetime]


class ScriptValidator:
    def __init__(
        self, database: Database, audit: AuditLog, *, clock: Clock | None = None
    ) -> None:
        self._database = database
        self._audit = audit
        self._clock = clock

    def validate(self, script_id: str, *, actor: Actor) -> ScriptValidation:
        with self._database.transaction() as connection:
            script = ScriptRepository(connection).get(script_id)
            if script is None:
                raise ScriptNotFoundError(f"script {script_id} does not exist")
            stored = ScriptValidationRepository(connection).get_by_script(script_id)
            if stored is not None:
                return stored
            item = ContentItemRepository(connection).get(script.content_item_id)
            if item is None:
                raise ContentItemNotFoundError(
                    f"content item {script.content_item_id} does not exist"
                )
            strategy = StrategyProfileRepository(connection).get_by_channel(
                item.channel_id
            )
        if strategy is None:
            raise StrategyNotFoundError(f"channel {item.channel_id} has no strategy")
        if strategy.languages is None or strategy.format is None:
            raise ScriptInputError(
                f"the strategy of channel {item.channel_id} needs a language "
                "and a format"
            )
        validation = ScriptValidation.create(
            script,
            validate_script(script, item.content_type, strategy),
            requested_by=actor,
            clock=self._clock,
        )
        try:
            with self._database.transaction() as connection:
                ScriptValidationRepository(connection).add(validation)
        except sqlite3.IntegrityError:
            # Expected: a concurrent run for this script committed first. Any
            # other integrity error is hidden only if such a run exists; else
            # it is raised again below.
            with self._database.transaction() as connection:
                stored = ScriptValidationRepository(connection).get_by_script(script_id)
            if stored is None:
                raise
            return stored
        self._audit.record(
            "script.validated",
            actor,
            EntityRef("script", script.id),
            AuditResult.SUCCESS,
            {
                "script_validation_id": validation.id,
                "content_item_id": script.content_item_id,
                "version": script.version,
                "method": validation.method,
                "strategy_version": validation.strategy_version,
                **validation.counts(),
            },
        )
        return validation
