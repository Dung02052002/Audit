"""AI disclosure decision (Prompt Pack v8, prompt G-081), context C6 Rights & Policy.

A ``DisclosureRecord`` is one entry of the history of the decisions whether a
content item needs an AI disclosure, with the rationale and the sources the
decision rests on. The design was approved by the user on 2026-10-04:

- ``DisclosureDecision``: ``REQUIRED`` or ``NOT_REQUIRED``.
- ``RationaleEntry``: the outcome of one rule (rule id, rule version, stable
  code, static message, and whether the rule triggered). One entry per rule of the
  set, in rule order. A code or a message never holds a title, a URL or a text.
- ``DisclosureSources``: what the decision looked at, as ids and names only: the
  facts the caller declared true (names from ``FACT_NAMES``, in that order) and
  the ids of the attached generated visual assets (sorted, unique).
- ``DisclosureEvaluation``: the pure result of the rules (rule set, decision,
  rationale, sources); it is not stored.
- ``DisclosureRecord``: the stored entry with its ids, the rule set id and its
  stored version (``<id>-rules-v<n>``), the decision, the rationale, the sources,
  who decided and when (UTC). The record is frozen and the history is append
  only. ``content_key`` is what the history compares: the version, the decision,
  the rationale and the sources, never the id, the actor, the channel or the time.

This module holds the entity only: the rules are in ``disclosure_rule.py``.
"""

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from ai_youtube_agent.content.policy_rule import RuleSet
from ai_youtube_agent.core.audit import Actor

FACT_NAMES = (
    "realistic_person",
    "realistic_event",
    "synthetic_voice_of_real_person",
    "realistic_visual",
)
# Relies on the ids ``Asset.create`` makes (uuid hex).
ASSET_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,100}")
MAX_STORED_LENGTH = 50
"""The CHECK of ``disclosure_decisions.rule_set_id`` and ``rule_set_version``."""
Clock = Callable[[], datetime]


class DisclosureDecision(StrEnum):
    REQUIRED = "required"
    NOT_REQUIRED = "not_required"


def _require_text(owner: object, *names: str) -> None:
    for name in names:
        value = getattr(owner, name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must not be empty")


def _require_utc(name: str, value: object) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")


def _check_decision(decision: object, rationale: object) -> None:
    if not isinstance(decision, DisclosureDecision):
        raise TypeError("decision must be a DisclosureDecision")
    if (
        not isinstance(rationale, tuple)
        or not rationale
        or not all(isinstance(entry, RationaleEntry) for entry in rationale)
    ):
        raise ValueError("rationale needs at least one RationaleEntry")
    triggered = any(entry.triggered for entry in rationale)
    if decision is DisclosureDecision.REQUIRED and not triggered:
        raise ValueError("a required decision needs a triggered rationale entry")
    if decision is DisclosureDecision.NOT_REQUIRED and triggered:
        raise ValueError("a not_required decision has no triggered rationale entry")


@dataclass(frozen=True)
class RationaleEntry:
    rule_id: str
    version: int
    code: str
    message: str
    triggered: bool

    def __post_init__(self) -> None:
        _require_text(self, "rule_id", "code", "message")
        if self.code != self.code.strip():
            raise ValueError("code must not have outer whitespace")
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version < 1
        ):
            raise ValueError("version must be an integer >= 1")
        if not isinstance(self.triggered, bool):
            raise TypeError("triggered must be a bool")

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "version": self.version,
            "code": self.code,
            "message": self.message,
            "triggered": self.triggered,
        }


@dataclass(frozen=True)
class DisclosureSources:
    facts: tuple[str, ...]
    asset_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.facts, tuple) or not all(
            isinstance(fact, str) and fact in FACT_NAMES for fact in self.facts
        ):
            raise ValueError("facts must hold names of the known facts only")
        canonical = tuple(name for name in FACT_NAMES if name in self.facts)
        if self.facts != canonical:
            raise ValueError("facts must be unique and in the canonical order")
        if not isinstance(self.asset_ids, tuple) or not all(
            isinstance(asset_id, str) and ASSET_ID_PATTERN.fullmatch(asset_id)
            for asset_id in self.asset_ids
        ):
            raise ValueError("asset_ids must hold safe asset ids only")
        if self.asset_ids != tuple(sorted(set(self.asset_ids))):
            raise ValueError("asset_ids must be sorted and unique")

    def as_dict(self) -> dict[str, Any]:
        return {"asset_ids": list(self.asset_ids), "facts": list(self.facts)}


@dataclass(frozen=True)
class DisclosureEvaluation:
    rule_set: RuleSet
    decision: DisclosureDecision
    rationale: tuple[RationaleEntry, ...]
    sources: DisclosureSources

    def __post_init__(self) -> None:
        if not isinstance(self.rule_set, RuleSet):
            raise TypeError("rule_set must be a RuleSet")
        _check_decision(self.decision, self.rationale)
        if not isinstance(self.sources, DisclosureSources):
            raise TypeError("sources must be DisclosureSources")


@dataclass(frozen=True)
class DisclosureRecord:
    id: str
    content_item_id: str
    channel_id: str
    rule_set_id: str
    rule_set_version: str
    decision: DisclosureDecision
    rationale: tuple[RationaleEntry, ...]
    sources: DisclosureSources
    decided_by: Actor
    created_at: datetime

    def __post_init__(self) -> None:
        _require_text(
            self,
            "id",
            "content_item_id",
            "channel_id",
            "rule_set_id",
            "rule_set_version",
        )
        for name in ("rule_set_id", "rule_set_version"):
            if len(getattr(self, name)) > MAX_STORED_LENGTH:
                raise ValueError(
                    f"{name} must be at most {MAX_STORED_LENGTH} characters"
                )
        pattern = re.escape(self.rule_set_id) + r"-rules-v[1-9][0-9]*"
        if not re.fullmatch(pattern, self.rule_set_version):
            raise ValueError("rule_set_version must be <rule_set_id>-rules-v<n>")
        _check_decision(self.decision, self.rationale)
        if not isinstance(self.sources, DisclosureSources):
            raise TypeError("sources must be DisclosureSources")
        if not isinstance(self.decided_by, Actor):
            raise TypeError("decided_by must be an Actor")
        _require_utc("created_at", self.created_at)

    @classmethod
    def create(
        cls,
        content_item_id: str,
        channel_id: str,
        evaluation: DisclosureEvaluation,
        *,
        decided_by: Actor,
        clock: Clock | None = None,
    ) -> "DisclosureRecord":
        return cls(
            id=uuid.uuid4().hex,
            content_item_id=content_item_id,
            channel_id=channel_id,
            rule_set_id=evaluation.rule_set.id,
            rule_set_version=evaluation.rule_set.stored_version,
            decision=evaluation.decision,
            rationale=evaluation.rationale,
            sources=evaluation.sources,
            decided_by=decided_by,
            created_at=clock() if clock else datetime.now(UTC),
        )

    def content_key(self) -> tuple[Any, ...]:
        """What the decision says: equal keys are the same decision."""
        return (self.rule_set_version, self.decision, self.rationale, self.sources)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content_item_id": self.content_item_id,
            "channel_id": self.channel_id,
            "rule_set_id": self.rule_set_id,
            "rule_set_version": self.rule_set_version,
            "decision": self.decision.value,
            "rationale": [entry.as_dict() for entry in self.rationale],
            "sources": self.sources.as_dict(),
            "decided_by": {
                "kind": self.decided_by.kind.value,
                "id": self.decided_by.id,
            },
            "created_at": self.created_at.isoformat(),
        }
