"""Emergency stop state (Prompt Pack v8, prompt #040), context C16 Orchestration.

The kill switch gate (#040) needs to read whether the emergency stop is active
before the kill switch itself (#213) exists. As the user approved on
2026-10-01, this module holds only that read shape, with no table or toggle:

- ``EmergencyStop``: ``active``, and while active the ``activated_by`` actor,
  the ``activated_at`` time (UTC) and an optional ``reason`` the activator
  gave. An inactive stop has none of these.

The value is frozen. Who may turn the switch on and off, and where it is
stored, is #213.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ai_youtube_agent.core.audit import Actor

Clock = Callable[[], datetime]


@dataclass(frozen=True)
class EmergencyStop:
    active: bool
    activated_by: Actor | None = None
    activated_at: datetime | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.active, bool):
            raise TypeError("active must be a bool")
        if not self.active:
            if any(
                v is not None
                for v in (self.activated_by, self.activated_at, self.reason)
            ):
                raise ValueError("an inactive stop has no activator, time or reason")
            return
        if not isinstance(self.activated_by, Actor):
            raise TypeError("an active stop needs activated_by")
        if self.activated_at is None:
            raise ValueError("an active stop needs activated_at")
        if self.activated_at.utcoffset() != timedelta(0):
            raise ValueError("activated_at must be timezone-aware UTC")
        if self.reason is not None and not self.reason.strip():
            raise ValueError("reason must not be empty")

    @classmethod
    def inactive(cls) -> "EmergencyStop":
        return cls(active=False)

    @classmethod
    def activated(
        cls,
        by: Actor,
        *,
        reason: str | None = None,
        clock: Clock | None = None,
    ) -> "EmergencyStop":
        return cls(
            active=True,
            activated_by=by,
            activated_at=clock() if clock else datetime.now(UTC),
            reason=reason.strip() if reason is not None else None,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "activated_by": (
                {"kind": self.activated_by.kind.value, "id": self.activated_by.id}
                if self.activated_by
                else None
            ),
            "activated_at": (
                self.activated_at.isoformat() if self.activated_at else None
            ),
            "reason": self.reason,
        }
