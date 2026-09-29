"""QC entity (Prompt Pack v8, prompt #020), context C10 Quality (Test & QC).

A ``QCResult`` is the structured outcome of one QC run:

- ``id``: a stable id for this run.
- ``content_item_id``: the ``ContentItem.id`` that was checked.
- ``artifact_ids``: the exact ``Artifact`` version records that were checked.
  A new artifact version therefore needs a new QC result.
- ``checks``: one or more ``QCCheck``, each with a unique dotted ``name`` such
  as ``video.fps``, a ``QCStatus`` of pass, warn or fail, and an optional
  ``detail`` message.
- ``created_at``: timezone-aware UTC.

``status`` is derived from the checks and never stored: fail if any check
fails, otherwise warn if any check warns, otherwise pass. A result is complete
when it is created and never changes; a rerun makes a new result. The concrete
checks come with #117–#128 and the aggregated QC report with #129.
"""

import re
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from ai_youtube_agent.core.artifact import Artifact

CHECK_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
Clock = Callable[[], datetime]


class QCStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class QCCheck:
    name: str
    status: QCStatus
    detail: str | None = None

    def __post_init__(self) -> None:
        if not CHECK_NAME_PATTERN.match(self.name):
            raise ValueError(
                f"check name {self.name!r} must be a lowercase dotted name "
                "such as 'video.fps'"
            )
        if not isinstance(self.status, QCStatus):
            raise TypeError("status must be a QCStatus")
        if self.detail is not None and not self.detail.strip():
            raise ValueError("check detail must not be empty")

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status.value, "detail": self.detail}


@dataclass(frozen=True)
class QCResult:
    id: str
    content_item_id: str
    artifact_ids: tuple[str, ...]
    checks: tuple[QCCheck, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        for name in ("id", "content_item_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if not isinstance(self.artifact_ids, tuple) or not self.artifact_ids:
            raise ValueError("artifact_ids must be a non-empty tuple")
        if any(not artifact_id.strip() for artifact_id in self.artifact_ids):
            raise ValueError("artifact ids must not be empty")
        if len(set(self.artifact_ids)) != len(self.artifact_ids):
            raise ValueError("artifact ids must not repeat")
        if not isinstance(self.checks, tuple) or not self.checks:
            raise ValueError("checks must be a non-empty tuple")
        if any(not isinstance(check, QCCheck) for check in self.checks):
            raise TypeError("checks must be QCCheck values")
        names = [check.name for check in self.checks]
        if len(set(names)) != len(names):
            raise ValueError("check names must not repeat")
        if self.created_at.utcoffset() != timedelta(0):
            raise ValueError("created_at must be timezone-aware UTC")

    @classmethod
    def create(
        cls,
        content_item_id: str,
        artifacts: Iterable[Artifact],
        checks: Iterable[QCCheck],
        *,
        clock: Clock | None = None,
    ) -> "QCResult":
        artifacts = tuple(artifacts)
        for artifact in artifacts:
            if artifact.content_item_id != content_item_id:
                raise ValueError(
                    f"artifact {artifact.id} belongs to another content item"
                )
        return cls(
            id=uuid.uuid4().hex,
            content_item_id=content_item_id,
            artifact_ids=tuple(artifact.id for artifact in artifacts),
            checks=tuple(checks),
            created_at=clock() if clock else datetime.now(UTC),
        )

    @property
    def status(self) -> QCStatus:
        statuses = {check.status for check in self.checks}
        for status in (QCStatus.FAIL, QCStatus.WARN):
            if status in statuses:
                return status
        return QCStatus.PASS

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content_item_id": self.content_item_id,
            "artifact_ids": list(self.artifact_ids),
            "status": self.status.value,
            "checks": [check.as_dict() for check in self.checks],
            "created_at": self.created_at.isoformat(),
        }
