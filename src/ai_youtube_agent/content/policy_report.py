"""Policy Report (Prompt Pack v8, prompt G-083), context C6 Rights & Policy.

``PolicyReporter`` builds a machine-readable and dashboard-friendly policy report
of one content item. It is a reporting layer over the ``PolicyChecker`` of G-080:
it evaluates, normalises and gates nothing by itself.

- One report per content item and exact ``RuleSet``, at any item status. An
  unknown rule set is ``PolicyRuleSetNotFoundError`` (checked before the database
  or the clock is read), an unknown item is ``ContentItemNotFoundError``, a
  channel without a strategy profile is ``StrategyNotFoundError`` (the report
  fails closed: it never assumes there are no banned phrases) and a wrong
  argument type is a ``TypeError`` that never holds the value.
- The report is computed on demand and never stored: no table, no migration, no
  HTTP route and no audit event. It changes nothing.
- The item, its strategy profile and the clock are read inside one transaction. A
  clock that returns a naive or non-UTC time is a fault of the application (a
  plain ``ValueError``).
- ``PolicyReport.to_dict`` is JSON-safe with a fixed key order, catalog order for
  every list and an ISO datetime. The schema is named by ``REPORT_SCHEMA_VERSION``.
- ``outcomes`` holds one entry per rule (passes included), ``findings`` the failed
  rules and ``blocking_findings`` the blocking ones. A report whose status,
  ``blocks`` or ``blocking_findings`` disagree with its outcomes cannot be built;
  neither can a report whose ``findings`` are not exactly the findings of its
  non-pass outcomes or whose outcome ``blocking`` flag disagrees with its status.
- The report never holds the title, the description, the tags or the banned
  phrases. A rule message passes through as the rule wrote it.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ai_youtube_agent.content.hook_generator import ContentItemNotFoundError
from ai_youtube_agent.content.policy import PolicyFinding
from ai_youtube_agent.content.policy_check import (
    PolicyChecker,
    PolicyInput,
    PolicyRuleSetCatalog,
    PolicyStatus,
    RuleOutcome,
    default_catalog,
    worst,
)
from ai_youtube_agent.content.policy_rule import RuleSet
from ai_youtube_agent.content.strategy_settings import StrategyNotFoundError
from ai_youtube_agent.content.text_prompts import banned_phrases
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import StrategyProfileRepository
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository

REPORT_SCHEMA_VERSION = "policy-report-v1"
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class RuleOutcomeReport:
    rule_id: str
    version: int
    status: str
    code: str
    message: str
    field: str | None
    blocking: bool

    @classmethod
    def from_outcome(cls, outcome: RuleOutcome) -> "RuleOutcomeReport":
        return cls(
            rule_id=outcome.rule_id,
            version=outcome.version,
            status=outcome.status.value,
            code=outcome.code,
            message=outcome.message,
            field=outcome.field,
            blocking=outcome.status is PolicyStatus.BLOCK,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "version": self.version,
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "field": self.field,
            "blocking": self.blocking,
        }


@dataclass(frozen=True)
class FindingReport:
    rule_id: str
    rule_version: int
    blocking: bool
    message: str

    @classmethod
    def from_finding(cls, finding: PolicyFinding) -> "FindingReport":
        return cls(
            rule_id=finding.rule_id,
            rule_version=finding.rule_version,
            blocking=finding.blocking,
            message=finding.message,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "blocking": self.blocking,
            "message": self.message,
        }


@dataclass(frozen=True)
class PolicyReport:
    content_item_id: str
    channel_id: str
    generated_at: datetime
    rule_set: RuleSet
    status: PolicyStatus
    blocks: bool
    outcomes: tuple[RuleOutcomeReport, ...]
    findings: tuple[FindingReport, ...]
    blocking_findings: tuple[FindingReport, ...]
    schema_version: str = REPORT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.status != worst(PolicyStatus(o.status) for o in self.outcomes):
            raise ValueError("status must be the worst outcome status")
        if self.blocks is not (self.status is PolicyStatus.BLOCK):
            raise ValueError("blocks must be true exactly when the status is block")
        if self.blocking_findings != tuple(f for f in self.findings if f.blocking):
            raise ValueError("blocking_findings must be the blocking findings")
        if any(o.blocking is not (o.status == "block") for o in self.outcomes):
            raise ValueError("an outcome is blocking exactly when its status is block")
        derived = tuple(
            FindingReport(o.rule_id, o.version, o.status == "block", o.message)
            for o in self.outcomes
            if o.status != PolicyStatus.PASS.value
        )
        if self.findings != derived:
            raise ValueError("findings must be the findings of the non-pass outcomes")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "content_item_id": self.content_item_id,
            "channel_id": self.channel_id,
            "generated_at": self.generated_at.isoformat(),
            "rule_set": {
                "id": self.rule_set.id,
                "version": self.rule_set.version,
                "stored_version": self.rule_set.stored_version,
            },
            "status": self.status.value,
            "blocks": self.blocks,
            "outcomes": [o.to_dict() for o in self.outcomes],
            "findings": [f.to_dict() for f in self.findings],
            "blocking_findings": [f.to_dict() for f in self.blocking_findings],
        }


class PolicyReporter:
    def __init__(
        self,
        database: Database,
        *,
        clock: Clock | None = None,
        catalog: PolicyRuleSetCatalog | None = None,
    ) -> None:
        self._database = database
        self._clock = clock
        self._catalog = catalog if catalog is not None else default_catalog()
        self._checker = PolicyChecker(self._catalog)

    def report(
        self,
        content_item_id: str,
        *,
        rule_set: RuleSet,
        description: str | None = None,
        tags: tuple[str, ...] | list[str] = (),
    ) -> PolicyReport:
        if not isinstance(content_item_id, str):
            raise TypeError("content_item_id must be text")
        if not isinstance(rule_set, RuleSet):
            raise TypeError("rule_set must be a RuleSet")
        if description is not None and not isinstance(description, str):
            raise TypeError("description must be text or None")
        if not isinstance(tags, tuple | list):
            raise TypeError("tags must be a tuple or list of text values")
        if not all(isinstance(tag, str) for tag in tags):
            raise TypeError("tags must be a tuple or list of text values")
        self._catalog.rules_for(rule_set)  # an unknown set fails before any read
        with self._database.transaction() as connection:
            now = self._now()
            item = ContentItemRepository(connection).get(content_item_id)
            if item is None:
                raise ContentItemNotFoundError(
                    f"content item {content_item_id} does not exist"
                )
            profile = StrategyProfileRepository(connection).get_by_channel(
                item.channel_id
            )
            if profile is None:
                raise StrategyNotFoundError(
                    f"channel {item.channel_id} has no strategy"
                )
            policy_input = PolicyInput(
                item.id,
                item.channel_id,
                item.title,
                description or "",
                tuple(tags),
                banned_phrases(profile),
            )
            result = self._checker.check(policy_input, rule_set=rule_set)
        findings = tuple(FindingReport.from_finding(f) for f in result.to_findings())
        return PolicyReport(
            content_item_id=item.id,
            channel_id=item.channel_id,
            generated_at=now,
            rule_set=result.rule_set,
            status=result.status,
            blocks=result.status is PolicyStatus.BLOCK,
            outcomes=tuple(RuleOutcomeReport.from_outcome(o) for o in result.outcomes),
            findings=findings,
            blocking_findings=tuple(f for f in findings if f.blocking),
        )

    def _now(self) -> datetime:
        """The time of the report, read inside the transaction. A clock that
        returns a naive or non-UTC time is a fault of the application."""
        now = self._clock() if self._clock else datetime.now(UTC)
        if not isinstance(now, datetime) or now.utcoffset() != timedelta(0):
            raise ValueError("the clock must return a timezone-aware UTC time")
        return now
