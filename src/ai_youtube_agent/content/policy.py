"""Policy check values (Prompt Pack v8, prompt #039), context C6 Rights & Policy.

The policy gate (#039) needs a shape for policy results before the rule
interface (#079), the check (#080) and the report (#083) exist. As the user
approved on 2026-10-01, this module holds only that read shape, with no table
or repository:

- ``PolicyFinding``: one failed rule. ``rule_id`` and ``rule_version`` name
  the rule, ``blocking`` is copied from the rule's configuration (a
  non-blocking finding is a warning), and ``message`` is safe to show users.
- ``PolicyCheck``: one run of the policy check over a content item at
  ``checked_at`` (UTC), with every finding of that run. A check with no
  findings passed.

Both are frozen. #080 creates checks and #083 stores and reports them.
"""

import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

Clock = Callable[[], datetime]


@dataclass(frozen=True)
class PolicyFinding:
    rule_id: str
    rule_version: int
    blocking: bool
    message: str

    def __post_init__(self) -> None:
        for name in ("rule_id", "message"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if (
            isinstance(self.rule_version, bool)
            or not isinstance(self.rule_version, int)
            or self.rule_version < 1
        ):
            raise ValueError("rule_version must be an integer >= 1")
        if not isinstance(self.blocking, bool):
            raise TypeError("blocking must be a bool")

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "blocking": self.blocking,
            "message": self.message,
        }


@dataclass(frozen=True)
class PolicyCheck:
    id: str
    content_item_id: str
    checked_at: datetime
    findings: tuple[PolicyFinding, ...]

    def __post_init__(self) -> None:
        for name in ("id", "content_item_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if self.checked_at.utcoffset() != timedelta(0):
            raise ValueError("checked_at must be timezone-aware UTC")
        if not isinstance(self.findings, tuple) or not all(
            isinstance(f, PolicyFinding) for f in self.findings
        ):
            raise TypeError("findings must be a tuple of PolicyFinding values")

    @classmethod
    def create(
        cls,
        content_item_id: str,
        findings: Iterable[PolicyFinding] = (),
        *,
        clock: Clock | None = None,
    ) -> "PolicyCheck":
        return cls(
            id=uuid.uuid4().hex,
            content_item_id=content_item_id,
            checked_at=clock() if clock else datetime.now(UTC),
            findings=tuple(findings),
        )

    @property
    def blocking_findings(self) -> tuple[PolicyFinding, ...]:
        return tuple(f for f in self.findings if f.blocking)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content_item_id": self.content_item_id,
            "checked_at": self.checked_at.isoformat(),
            "findings": [f.as_dict() for f in self.findings],
        }
