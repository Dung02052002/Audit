"""AI Disclosure Decider (Prompt Pack v8, prompt G-081), context C6 Rights & Policy.

``DisclosureDecider`` decides whether a content item needs an AI disclosure and
stores the decision with its rationale and sources as an append-only history of
``DisclosureRecord`` rows. The design was approved by the user on 2026-10-04:

- Scope: a service (this module), the entity (``content/disclosure.py``), the
  rules (``content/disclosure_rule.py``), a migration (0026) and a repository
  (``core/db/repositories/disclosure.py``), registered in the bootstrap. No HTTP
  route. The decision is not wired into the policy check, the publish gate or
  any QC.
- ``decide`` takes the facts the caller declares (``DisclosureFacts``), the rule
  set to apply and the actor. The rules are deterministic, with no AI model, and
  run only through the G-080 ``PolicyChecker``. The generated image and video clip
  assets attached to the item are read inside the transaction.
- An unknown rule set is ``DisclosureRuleSetNotFoundError`` (404) before the
  database or the clock is touched. A missing item is ``ContentItemNotFoundError``
  (404). A wrong argument type is a ``TypeError`` that names the argument.
- The whole decision runs in one ``BEGIN IMMEDIATE`` transaction: the time (read
  from the clock inside it), the item, the attached assets, the newest decision
  and the insert. A clock that returns a naive or non-UTC time is a fault of the
  application (a plain ``ValueError``).
- The clock must not go backwards: a time earlier than the newest decision of the
  item is a plain ``ValueError`` that names only the item id, checked before the
  idempotent return. An equal time is allowed.
- A decision equal to the newest one (``DisclosureRecord.content_key``: the rule
  set version, the decision, the rationale and the sources) returns the newest
  record and writes and audits nothing, whoever asks. Any other decision, also
  one equal to an older row, appends a row (A, B, A gives three rows).
- ``disclosure.decided`` is audited after commit, only for a new row, with ids,
  the decision, the four facts and the number of generated visuals; never a title,
  a URL or an asset id.
- ``latest`` and ``history`` read and write nothing.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from ai_youtube_agent.content.disclosure import DisclosureRecord
from ai_youtube_agent.content.disclosure_rule import (
    DisclosureFacts,
    DisclosureRuleCatalog,
    default_disclosure_catalog,
    evaluate_disclosure,
)
from ai_youtube_agent.content.hook_generator import ContentItemNotFoundError
from ai_youtube_agent.content.policy_rule import RuleSet
from ai_youtube_agent.core.audit import Actor, AuditLog, AuditResult, EntityRef
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.asset import AssetRepository
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.disclosure import DisclosureRepository

Clock = Callable[[], datetime]


class DisclosureDecider:
    def __init__(
        self,
        database: Database,
        audit: AuditLog,
        *,
        clock: Clock | None = None,
        catalog: DisclosureRuleCatalog | None = None,
    ) -> None:
        self._database = database
        self._audit = audit
        self._clock = clock
        self._catalog = catalog if catalog is not None else default_disclosure_catalog()

    def decide(
        self,
        content_item_id: str,
        facts: DisclosureFacts,
        *,
        rule_set: RuleSet,
        actor: Actor,
    ) -> DisclosureRecord:
        if not isinstance(content_item_id, str):
            raise TypeError("content_item_id must be text")
        if not isinstance(facts, DisclosureFacts):
            raise TypeError("facts must be DisclosureFacts")
        if not isinstance(rule_set, RuleSet):
            raise TypeError("rule_set must be a RuleSet")
        if not isinstance(actor, Actor):
            raise TypeError("actor must be an Actor")
        # An unknown rule set is refused before the database or the clock is used.
        self._catalog.factory_for(rule_set)
        with self._database.transaction() as connection:
            now = self._now()
            item = ContentItemRepository(connection).get(content_item_id)
            if item is None:
                raise ContentItemNotFoundError(
                    f"content item {content_item_id} does not exist"
                )
            assets = AssetRepository(connection).list_by_content_item(item.id)
            evaluation = evaluate_disclosure(
                item.id,
                item.channel_id,
                facts,
                assets,
                rule_set=rule_set,
                catalog=self._catalog,
            )
            decision = DisclosureRecord.create(
                item.id,
                item.channel_id,
                evaluation,
                decided_by=actor,
                clock=lambda: now,
            )
            records = DisclosureRepository(connection)
            newest = records.latest(item.id)
            if newest is not None:
                if now < newest.created_at:
                    raise ValueError(
                        f"the clock is earlier than a stored disclosure decision "
                        f"of content item {item.id}"
                    )
                if newest.content_key() == decision.content_key():
                    return newest
            records.add(decision)
        self._audit.record(
            "disclosure.decided",
            actor,
            EntityRef("content_item", decision.content_item_id),
            AuditResult.SUCCESS,
            {
                "decision_id": decision.id,
                "channel_id": decision.channel_id,
                "rule_set_id": decision.rule_set_id,
                "rule_set_version": decision.rule_set_version,
                "decision": decision.decision.value,
                "realistic_person": facts.realistic_person,
                "realistic_event": facts.realistic_event,
                "synthetic_voice_of_real_person": (
                    facts.synthetic_voice_of_real_person
                ),
                "realistic_visual": facts.realistic_visual,
                "generated_visual_count": len(decision.sources.asset_ids),
            },
        )
        return decision

    def latest(self, content_item_id: str) -> DisclosureRecord | None:
        with self._database.transaction() as connection:
            _require_item(connection, content_item_id)
            return DisclosureRepository(connection).latest(content_item_id)

    def history(self, content_item_id: str) -> list[DisclosureRecord]:
        with self._database.transaction() as connection:
            _require_item(connection, content_item_id)
            return DisclosureRepository(connection).list_by_content_item(
                content_item_id
            )

    def _now(self) -> datetime:
        """The time of the decision, read inside the transaction, so that a later
        insert never carries an earlier time. A clock that returns a naive or
        non-UTC time is a fault of the application, not an input error."""
        now = self._clock() if self._clock else datetime.now(UTC)
        if not isinstance(now, datetime) or now.utcoffset() != timedelta(0):
            raise ValueError("the clock must return a timezone-aware UTC time")
        return now


def _require_item(connection, content_item_id: str) -> None:
    if ContentItemRepository(connection).get(content_item_id) is None:
        raise ContentItemNotFoundError(f"content item {content_item_id} does not exist")
