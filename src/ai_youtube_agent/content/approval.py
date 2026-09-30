"""Approval entity (Prompt Pack v8, prompt #021), context C11 Review.

An ``ApprovalRequest`` asks a person to approve one exact set of artifact
versions of a content item:

- ``id``: a stable id for this request.
- ``content_item_id``: the ``ContentItem.id`` to approve.
- ``artifacts``: one ``ArtifactBinding`` per artifact, a snapshot of its id,
  kind, version and sha256 taken from the ``Artifact`` record. Each kind
  appears once. The checksum lets #035 detect a changed artifact without
  trusting ids alone.
- ``status``: one of ``ApprovalStatus``. A new request starts ``PENDING``.
- ``requested_by``: the ``Actor`` who asked, usually the pipeline (system).
- ``qc_result_id``: the optional ``QCResult`` shown with the preview (#134).
- ``created_at``: timezone-aware UTC.

The request is frozen. Approve, reject and request changes come with
#139–#141 and expiry with #142. ``invalidate`` (#035) marks a pending or
approved request as invalidated once ``stale_kinds`` shows it no longer covers
the current artifact versions. The approval gate (#034) blocks a publish
without a valid approval (R-08).
"""

import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from ai_youtube_agent.content.qc import QCResult
from ai_youtube_agent.core.artifact import SHA256_PATTERN, Artifact, ArtifactKind
from ai_youtube_agent.core.audit import Actor
from ai_youtube_agent.core.errors import DomainError

Clock = Callable[[], datetime]


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"
    INVALIDATED = "invalidated"
    EXPIRED = "expired"


# The statuses a changed artifact can invalidate (#035, user decision): a
# request still waiting for review and an approval that is still in force.
INVALIDATABLE = frozenset({ApprovalStatus.PENDING, ApprovalStatus.APPROVED})


class ApprovalStateError(DomainError):
    default_code = "domain.approval_state"
    default_user_message = "This approval cannot do that in its current state."


@dataclass(frozen=True)
class ArtifactBinding:
    artifact_id: str
    kind: ArtifactKind
    version: int
    sha256: str

    def __post_init__(self) -> None:
        if not self.artifact_id.strip():
            raise ValueError("artifact_id must not be empty")
        if not isinstance(self.kind, ArtifactKind):
            raise TypeError("kind must be an ArtifactKind")
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version < 1
        ):
            raise ValueError("version must be a whole number of 1 or more")
        if not SHA256_PATTERN.match(self.sha256):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")

    @classmethod
    def of(cls, artifact: Artifact) -> "ArtifactBinding":
        return cls(artifact.id, artifact.kind, artifact.version, artifact.sha256)

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind.value,
            "version": self.version,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class ApprovalRequest:
    id: str
    content_item_id: str
    artifacts: tuple[ArtifactBinding, ...]
    status: ApprovalStatus
    requested_by: Actor
    qc_result_id: str | None
    created_at: datetime

    def __post_init__(self) -> None:
        for name in ("id", "content_item_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if not isinstance(self.artifacts, tuple) or not self.artifacts:
            raise ValueError("artifacts must be a non-empty tuple")
        if any(not isinstance(item, ArtifactBinding) for item in self.artifacts):
            raise TypeError("artifacts must be ArtifactBinding values")
        ids = [binding.artifact_id for binding in self.artifacts]
        if len(set(ids)) != len(ids):
            raise ValueError("artifacts must not repeat")
        kinds = [binding.kind for binding in self.artifacts]
        if len(set(kinds)) != len(kinds):
            raise ValueError("each artifact kind may appear only once")
        if not isinstance(self.status, ApprovalStatus):
            raise TypeError("status must be an ApprovalStatus")
        if not isinstance(self.requested_by, Actor):
            raise TypeError("requested_by must be an Actor")
        if self.qc_result_id is not None and not self.qc_result_id.strip():
            raise ValueError("qc_result_id must not be empty")
        if self.created_at.utcoffset() != timedelta(0):
            raise ValueError("created_at must be timezone-aware UTC")

    @classmethod
    def create(
        cls,
        content_item_id: str,
        artifacts: Iterable[Artifact],
        *,
        requested_by: Actor,
        qc_result: QCResult | None = None,
        clock: Clock | None = None,
    ) -> "ApprovalRequest":
        artifacts = tuple(artifacts)
        for artifact in artifacts:
            if artifact.content_item_id != content_item_id:
                raise ValueError(
                    f"artifact {artifact.id} belongs to another content item"
                )
        if qc_result is not None and qc_result.content_item_id != content_item_id:
            raise ValueError("the QC result belongs to another content item")
        return cls(
            id=uuid.uuid4().hex,
            content_item_id=content_item_id,
            artifacts=tuple(ArtifactBinding.of(artifact) for artifact in artifacts),
            status=ApprovalStatus.PENDING,
            requested_by=requested_by,
            qc_result_id=qc_result.id if qc_result else None,
            created_at=clock() if clock else datetime.now(UTC),
        )

    def invalidate(self) -> "ApprovalRequest":
        """Return this request marked invalidated (#035)."""
        if self.status not in INVALIDATABLE:
            raise ApprovalStateError(
                f"approval {self.id} is {self.status.value} and cannot be invalidated"
            )
        return replace(self, status=ApprovalStatus.INVALIDATED)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content_item_id": self.content_item_id,
            "artifacts": [binding.as_dict() for binding in self.artifacts],
            "status": self.status.value,
            "requested_by": {
                "kind": self.requested_by.kind.value,
                "id": self.requested_by.id,
            },
            "qc_result_id": self.qc_result_id,
            "created_at": self.created_at.isoformat(),
        }


def stale_kinds(
    approval: ApprovalRequest, artifacts: Sequence[Artifact]
) -> list[ArtifactKind]:
    """Kinds whose latest version is not exactly what the approval bound.

    The latest version of each kind among ``artifacts`` must be bound with the
    same id, version and sha256. A kind the item has but the approval does not
    bind, and a bound kind the item no longer has, are both stale. An empty
    list means the approval covers the current versions (#034, #035).
    """
    latest: dict[ArtifactKind, ArtifactBinding] = {}
    for artifact in artifacts:
        current = latest.get(artifact.kind)
        if current is None or artifact.version > current.version:
            latest[artifact.kind] = ArtifactBinding.of(artifact)
    bound = {binding.kind: binding for binding in approval.artifacts}
    return [
        kind
        for kind in ArtifactKind
        if (kind in latest or kind in bound) and latest.get(kind) != bound.get(kind)
    ]
