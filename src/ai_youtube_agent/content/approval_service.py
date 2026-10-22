"""Approval Request, Approve, Reject, Request Changes & Expiry service.

(Prompt Pack v8, prompts #137, #139–#143), context C11 Review.

``ApprovalService`` coordinates the lifecycle of human review requests:

- ``request_approval`` (#137): snapshots the exact latest ``Artifact`` of every
  kind on the content item into an ``ApprovalRequest``, verifies QC has passed
  (``PASS`` or ``WARN``) and covers the latest ``VIDEO`` artifact, invalidates
  any prior active requests, transitions the ``ContentItem`` to
  ``AWAITING_APPROVAL``, records ``approval.requested``, and optionally sends
  the approval email (#138).
- ``approve`` (#139): requires an explicit human reviewer (``ActorKind.USER``),
  verifies the request has not expired (#142) and its bound artifacts are still
  current via ``stale_kinds`` and match any ``expected_video_artifact_id`` /
  ``expected_video_version`` supplied by the reviewer, marks the request
  ``APPROVED``, transitions the ``ContentItem`` to ``APPROVED``, and records
  ``approval.approved`` (#143).
- ``reject`` (#140): requires an explicit human reviewer (``ActorKind.USER``)
  and a non-empty ``reason``, marks the request ``REJECTED``, transitions the
  ``ContentItem`` to ``REJECTED``, and records ``approval.rejected`` (#143).
- ``request_changes`` (#141): requires an explicit human reviewer
  (``ActorKind.USER``) and a non-empty ``reason``, marks the request
  ``CHANGES_REQUESTED``, transitions the ``ContentItem`` back to ``GENERATING``
  for the preview edit loop (#136), and records ``approval.changes_requested``
  (#143).
- ``expire_stale`` and ``revalidate`` (#142): enforces configurable approval
  expiry TTL and revalidates an expired request against current artifacts and QC
  to issue a fresh ``PENDING`` request, recording ``approval.expired`` and
  ``approval.revalidated`` (#143).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ai_youtube_agent.content.approval import (
    DEFAULT_APPROVAL_TTL,
    EXPIRABLE,
    INVALIDATABLE,
    ApprovalConflictError,
    ApprovalExpiredError,
    ApprovalNotAllowedError,
    ApprovalNotFoundError,
    ApprovalReasonRequiredError,
    ApprovalRequest,
    ApprovalStateError,
    ApprovalStatus,
    stale_kinds,
)
from ai_youtube_agent.content.approval_audit import (
    APPROVAL_APPROVED,
    APPROVAL_CHANGES_REQUESTED,
    APPROVAL_ENTITY_TYPE,
    APPROVAL_EXPIRED,
    APPROVAL_REJECTED,
    APPROVAL_REQUESTED,
    APPROVAL_REVALIDATED,
)
from ai_youtube_agent.content.email_approval import ApprovalEmailService
from ai_youtube_agent.content.qc import QCResult, QCStatus
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    EntityRef,
)
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.review import (
    ApprovalRequestRepository,
    QCResultRepository,
)

Clock = Callable[[], datetime]
DEFAULT_REQUESTER = Actor(ActorKind.SYSTEM, "approval_service")


@dataclass(frozen=True)
class ApprovalDecisionResult:
    approval: ApprovalRequest
    content_item: ContentItem
    decision: str
    decided_by: Actor
    reason: str | None = None
    target_kinds: tuple[ArtifactKind, ...] = ()
    email_receipt: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "approval": self.approval.as_dict(),
            "content_item": self.content_item.as_dict(),
            "decision": self.decision,
            "decided_by": {
                "kind": self.decided_by.kind.value,
                "id": self.decided_by.id,
            },
            "reason": self.reason,
        }
        if self.target_kinds:
            payload["target_kinds"] = [k.value for k in self.target_kinds]
        if self.email_receipt is not None:
            payload["email_receipt"] = self.email_receipt
        return payload


def _latest_artifacts_by_kind(artifacts: list[Artifact]) -> list[Artifact]:
    by_kind: dict[ArtifactKind, Artifact] = {}
    for artifact in artifacts:
        current = by_kind.get(artifact.kind)
        if current is None or artifact.version > current.version:
            by_kind[artifact.kind] = artifact
    return [by_kind[kind] for kind in ArtifactKind if kind in by_kind]


class ApprovalService:
    def __init__(
        self,
        database: Database,
        audit_log: AuditLog,
        *,
        email_service: ApprovalEmailService | None = None,
        ttl: timedelta = DEFAULT_APPROVAL_TTL,
        clock: Clock | None = None,
    ) -> None:
        if not isinstance(ttl, timedelta) or ttl <= timedelta(0):
            raise ValueError("ttl must be a positive timedelta")
        self._database = database
        self._audit = audit_log
        self._email_service = email_service
        self._ttl = ttl
        self._clock = clock

    @property
    def ttl(self) -> timedelta:
        return self._ttl

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)

    def get(self, approval_request_id: str) -> ApprovalRequest:
        with self._database.transaction() as conn:
            request = ApprovalRequestRepository(conn).get(approval_request_id)
            if request is None:
                raise ApprovalNotFoundError(
                    f"approval request {approval_request_id} was not found"
                )
            return request

    def list_by_content_item(self, content_item_id: str) -> list[ApprovalRequest]:
        with self._database.transaction() as conn:
            item = ContentItemRepository(conn).get(content_item_id)
            if item is None:
                raise ApprovalNotFoundError(
                    f"content item {content_item_id} was not found"
                )
            return ApprovalRequestRepository(conn).list_by_content_item(content_item_id)

    def request_approval(
        self,
        content_item_id: str,
        *,
        requested_by: Actor = DEFAULT_REQUESTER,
        expected_video_version: int | None = None,
        expected_video_artifact_id: str | None = None,
        recipient_email: str | None = None,
        base_url: str | None = None,
    ) -> ApprovalDecisionResult:
        with self._database.transaction() as conn:
            items_repo = ContentItemRepository(conn)
            artifacts_repo = ArtifactRepository(conn)
            qc_repo = QCResultRepository(conn)
            approvals_repo = ApprovalRequestRepository(conn)

            item = items_repo.get(content_item_id)
            if item is None:
                raise ApprovalNotFoundError(
                    f"content item {content_item_id} was not found"
                )

            all_artifacts = artifacts_repo.list_by_content_item(content_item_id)
            latest_artifacts = _latest_artifacts_by_kind(all_artifacts)
            video_artifact = next(
                (a for a in latest_artifacts if a.kind is ArtifactKind.VIDEO), None
            )
            if video_artifact is None:
                raise ApprovalStateError(
                    f"content item {content_item_id} has no video artifact to approve"
                )

            if (
                expected_video_version is not None
                and video_artifact.version != expected_video_version
            ):
                raise ApprovalConflictError(
                    f"video artifact version is {video_artifact.version}, "
                    f"expected {expected_video_version}"
                )
            if (
                expected_video_artifact_id is not None
                and video_artifact.id != expected_video_artifact_id
            ):
                raise ApprovalConflictError(
                    f"video artifact id is {video_artifact.id}, "
                    f"expected {expected_video_artifact_id}"
                )

            qc_results = qc_repo.list_by_content_item(content_item_id)
            latest_qc: QCResult | None = qc_results[-1] if qc_results else None
            if latest_qc is None:
                raise ApprovalStateError(
                    f"content item {content_item_id} has no QC result; "
                    "run QC before requesting approval"
                )
            if latest_qc.status is QCStatus.FAIL:
                raise ApprovalStateError(
                    f"content item {content_item_id} failed QC ({latest_qc.id}) "
                    "and cannot request approval"
                )
            if video_artifact.id not in latest_qc.artifact_ids:
                raise ApprovalStateError(
                    f"latest QC result {latest_qc.id} does not cover "
                    f"current video artifact {video_artifact.id} "
                    f"(v{video_artifact.version})"
                )

            # Invalidate any previously pending or approved requests for this item
            for existing in approvals_repo.list_by_content_item(content_item_id):
                if existing.status in INVALIDATABLE:
                    approvals_repo.update(
                        existing.invalidate(), expected_status=existing.status
                    )

            # Advance ContentItem to AWAITING_APPROVAL
            if item.status is ContentStatus.TESTING:
                prev_updated = item.updated_at
                item = item.with_status(ContentStatus.PREVIEW_READY, clock=self._clock)
                items_repo.update(item, expected_updated_at=prev_updated)
            if item.status is ContentStatus.PREVIEW_READY:
                prev_updated = item.updated_at
                item = item.with_status(
                    ContentStatus.AWAITING_APPROVAL, clock=self._clock
                )
                items_repo.update(item, expected_updated_at=prev_updated)
            elif item.status is not ContentStatus.AWAITING_APPROVAL:
                item.with_status(ContentStatus.AWAITING_APPROVAL, clock=self._clock)

            request = ApprovalRequest.create(
                content_item_id,
                latest_artifacts,
                requested_by=requested_by,
                qc_result=latest_qc,
                clock=self._clock,
            )
            approvals_repo.add(request)

        self._audit.record(
            APPROVAL_REQUESTED,
            requested_by,
            EntityRef(APPROVAL_ENTITY_TYPE, request.id),
            AuditResult.SUCCESS,
            metadata={
                "content_item_id": content_item_id,
                "video_artifact_id": video_artifact.id,
                "video_artifact_version": video_artifact.version,
                "video_artifact_sha256": video_artifact.sha256,
                "qc_result_id": latest_qc.id,
                "bound_artifact_count": len(request.artifacts),
            },
        )

        email_receipt: dict[str, Any] | None = None
        if recipient_email is not None:
            if self._email_service is None:
                raise ApprovalStateError(
                    "email_service is not configured to send approval emails"
                )
            email_receipt = self._email_service.send_approval_email(
                request.id,
                recipient_email,
                base_url=base_url,
                actor=requested_by,
            )

        return ApprovalDecisionResult(
            approval=request,
            content_item=item,
            decision="requested",
            decided_by=requested_by,
            email_receipt=email_receipt,
        )

    def _expire_if_past_ttl(
        self,
        approvals_repo: ApprovalRequestRepository,
        request: ApprovalRequest,
        *,
        now: datetime,
        action: str,
        actor: Actor,
    ) -> None:
        if request.status is ApprovalStatus.PENDING and request.is_expired(
            now, self._ttl
        ):
            expired = request.expire()
            approvals_repo.update(expired, expected_status=ApprovalStatus.PENDING)
            self._audit.record(
                APPROVAL_EXPIRED,
                DEFAULT_REQUESTER,
                EntityRef(APPROVAL_ENTITY_TYPE, request.id),
                AuditResult.SUCCESS,
                metadata={
                    "content_item_id": request.content_item_id,
                    "previous_status": request.status.value,
                    "expires_at": request.expires_at(self._ttl).isoformat(),
                },
            )
            self._audit.record(
                action,
                actor,
                EntityRef(APPROVAL_ENTITY_TYPE, request.id),
                AuditResult.DENIED,
                metadata={
                    "content_item_id": request.content_item_id,
                    "status": ApprovalStatus.EXPIRED.value,
                    "reason": "expired",
                },
            )
            raise ApprovalExpiredError(
                f"approval {request.id} expired at "
                f"{request.expires_at(self._ttl).isoformat()} and must be revalidated"
            )

    def approve(
        self,
        approval_request_id: str,
        *,
        actor: Actor,
        expected_video_artifact_id: str | None = None,
        expected_video_version: int | None = None,
        note: str | None = None,
    ) -> ApprovalDecisionResult:
        now = self._now()
        expired_during_call = False
        expired_at_iso = ""
        content_item_id_for_audit = ""
        with self._database.transaction() as conn:
            approvals_repo = ApprovalRequestRepository(conn)
            items_repo = ContentItemRepository(conn)
            artifacts_repo = ArtifactRepository(conn)

            request = approvals_repo.get(approval_request_id)
            if request is None:
                raise ApprovalNotFoundError(
                    f"approval request {approval_request_id} was not found"
                )
            content_item_id_for_audit = request.content_item_id
            item = items_repo.get(request.content_item_id)
            if item is None:
                raise ApprovalNotFoundError(
                    f"content item {request.content_item_id} was not found"
                )
            artifacts = artifacts_repo.list_by_content_item(request.content_item_id)

            if request.status is ApprovalStatus.PENDING and request.is_expired(
                now, self._ttl
            ):
                expired = request.expire()
                approvals_repo.update(expired, expected_status=ApprovalStatus.PENDING)
                expired_during_call = True
                expired_at_iso = request.expires_at(self._ttl).isoformat()

            if not expired_during_call:
                video_binding = request.binding_for(ArtifactKind.VIDEO)
                if video_binding is None:
                    raise ApprovalStateError(
                        f"approval {request.id} does not bind a video artifact"
                    )
                if (
                    expected_video_artifact_id is not None
                    and video_binding.artifact_id != expected_video_artifact_id
                ):
                    raise ApprovalConflictError(
                        f"approval {request.id} binds video artifact "
                        f"{video_binding.artifact_id}, not {expected_video_artifact_id}"
                    )
                if (
                    expected_video_version is not None
                    and video_binding.version != expected_video_version
                ):
                    raise ApprovalConflictError(
                        f"approval {request.id} binds video version "
                        f"{video_binding.version}, not {expected_video_version}"
                    )

                stale = stale_kinds(request, artifacts)
                if stale:
                    if request.status in INVALIDATABLE:
                        invalidated = request.invalidate()
                        approvals_repo.update(
                            invalidated, expected_status=request.status
                        )
                    kinds_str = ", ".join(k.value for k in stale)
                    self._audit.record(
                        APPROVAL_APPROVED,
                        actor,
                        EntityRef(APPROVAL_ENTITY_TYPE, request.id),
                        AuditResult.DENIED,
                        metadata={
                            "content_item_id": request.content_item_id,
                            "stale_kinds": kinds_str,
                        },
                    )
                    raise ApprovalStateError(
                        f"approval {request.id} is stale for artifact kinds: "
                        f"{kinds_str}"
                    )

                try:
                    approved = request.approve(actor)
                except (ApprovalNotAllowedError, ApprovalStateError):
                    self._audit.record(
                        APPROVAL_APPROVED,
                        actor,
                        EntityRef(APPROVAL_ENTITY_TYPE, request.id),
                        AuditResult.DENIED,
                        metadata={
                            "content_item_id": request.content_item_id,
                            "status": request.status.value,
                        },
                    )
                    raise

                approvals_repo.update(approved, expected_status=ApprovalStatus.PENDING)

                if item.status is ContentStatus.PREVIEW_READY:
                    prev_updated = item.updated_at
                    item = item.with_status(
                        ContentStatus.AWAITING_APPROVAL, clock=self._clock
                    )
                    items_repo.update(item, expected_updated_at=prev_updated)
                prev_updated = item.updated_at
                item = item.with_status(ContentStatus.APPROVED, clock=self._clock)
                items_repo.update(item, expected_updated_at=prev_updated)

        if expired_during_call:
            self._audit.record(
                APPROVAL_EXPIRED,
                DEFAULT_REQUESTER,
                EntityRef(APPROVAL_ENTITY_TYPE, approval_request_id),
                AuditResult.SUCCESS,
                metadata={
                    "content_item_id": content_item_id_for_audit,
                    "previous_status": ApprovalStatus.PENDING.value,
                    "expires_at": expired_at_iso,
                },
            )
            self._audit.record(
                APPROVAL_APPROVED,
                actor,
                EntityRef(APPROVAL_ENTITY_TYPE, approval_request_id),
                AuditResult.DENIED,
                metadata={
                    "content_item_id": content_item_id_for_audit,
                    "status": ApprovalStatus.EXPIRED.value,
                    "reason": "expired",
                },
            )
            raise ApprovalExpiredError(
                f"approval {approval_request_id} expired at {expired_at_iso} "
                "and must be revalidated"
            )

        cleaned_note = note.strip() if isinstance(note, str) and note.strip() else None
        self._audit.record(
            APPROVAL_APPROVED,
            actor,
            EntityRef(APPROVAL_ENTITY_TYPE, approved.id),
            AuditResult.SUCCESS,
            metadata={
                "content_item_id": approved.content_item_id,
                "video_artifact_id": video_binding.artifact_id,
                "video_artifact_version": video_binding.version,
                "video_artifact_sha256": video_binding.sha256,
                "note": cleaned_note,
            },
        )
        return ApprovalDecisionResult(
            approval=approved,
            content_item=item,
            decision="approved",
            decided_by=actor,
            reason=cleaned_note,
        )

    def reject(
        self,
        approval_request_id: str,
        *,
        actor: Actor,
        reason: str,
    ) -> ApprovalDecisionResult:
        with self._database.transaction() as conn:
            approvals_repo = ApprovalRequestRepository(conn)
            items_repo = ContentItemRepository(conn)

            request = approvals_repo.get(approval_request_id)
            if request is None:
                raise ApprovalNotFoundError(
                    f"approval request {approval_request_id} was not found"
                )
            item = items_repo.get(request.content_item_id)
            if item is None:
                raise ApprovalNotFoundError(
                    f"content item {request.content_item_id} was not found"
                )

            try:
                rejected = request.reject(actor, reason)
            except (
                ApprovalNotAllowedError,
                ApprovalReasonRequiredError,
                ApprovalStateError,
            ):
                self._audit.record(
                    APPROVAL_REJECTED,
                    actor,
                    EntityRef(APPROVAL_ENTITY_TYPE, request.id),
                    AuditResult.DENIED,
                    metadata={
                        "content_item_id": request.content_item_id,
                        "status": request.status.value,
                    },
                )
                raise

            approvals_repo.update(rejected, expected_status=ApprovalStatus.PENDING)

            if item.status is ContentStatus.PREVIEW_READY:
                prev_updated = item.updated_at
                item = item.with_status(
                    ContentStatus.AWAITING_APPROVAL, clock=self._clock
                )
                items_repo.update(item, expected_updated_at=prev_updated)
            prev_updated = item.updated_at
            item = item.with_status(ContentStatus.REJECTED, clock=self._clock)
            items_repo.update(item, expected_updated_at=prev_updated)

        cleaned_reason = reason.strip()
        video_binding = rejected.binding_for(ArtifactKind.VIDEO)
        self._audit.record(
            APPROVAL_REJECTED,
            actor,
            EntityRef(APPROVAL_ENTITY_TYPE, rejected.id),
            AuditResult.SUCCESS,
            metadata={
                "content_item_id": rejected.content_item_id,
                "video_artifact_id": (
                    video_binding.artifact_id if video_binding else None
                ),
                "video_artifact_version": (
                    video_binding.version if video_binding else None
                ),
                "reason": cleaned_reason,
            },
        )
        return ApprovalDecisionResult(
            approval=rejected,
            content_item=item,
            decision="rejected",
            decided_by=actor,
            reason=cleaned_reason,
        )

    def request_changes(
        self,
        approval_request_id: str,
        *,
        actor: Actor,
        reason: str,
        target_kinds: Sequence[ArtifactKind] | None = None,
    ) -> ApprovalDecisionResult:
        """Record a human change request (#141), moving the item back to GENERATING."""
        kinds_tuple: tuple[ArtifactKind, ...] = ()
        if target_kinds is not None:
            seen: list[ArtifactKind] = []
            for kind in target_kinds:
                if not isinstance(kind, ArtifactKind):
                    raise TypeError("target_kinds must contain ArtifactKind values")
                if kind not in seen:
                    seen.append(kind)
            kinds_tuple = tuple(seen)

        with self._database.transaction() as conn:
            approvals_repo = ApprovalRequestRepository(conn)
            items_repo = ContentItemRepository(conn)

            request = approvals_repo.get(approval_request_id)
            if request is None:
                raise ApprovalNotFoundError(
                    f"approval request {approval_request_id} was not found"
                )
            item = items_repo.get(request.content_item_id)
            if item is None:
                raise ApprovalNotFoundError(
                    f"content item {request.content_item_id} was not found"
                )

            try:
                updated = request.request_changes(actor, reason)
            except (
                ApprovalNotAllowedError,
                ApprovalReasonRequiredError,
                ApprovalStateError,
            ):
                self._audit.record(
                    APPROVAL_CHANGES_REQUESTED,
                    actor,
                    EntityRef(APPROVAL_ENTITY_TYPE, request.id),
                    AuditResult.DENIED,
                    metadata={
                        "content_item_id": request.content_item_id,
                        "status": request.status.value,
                    },
                )
                raise

            approvals_repo.update(updated, expected_status=ApprovalStatus.PENDING)

            if item.status is ContentStatus.PREVIEW_READY:
                prev_updated = item.updated_at
                item = item.with_status(
                    ContentStatus.AWAITING_APPROVAL, clock=self._clock
                )
                items_repo.update(item, expected_updated_at=prev_updated)
            if item.status is not ContentStatus.GENERATING:
                prev_updated = item.updated_at
                item = item.with_status(ContentStatus.GENERATING, clock=self._clock)
                items_repo.update(item, expected_updated_at=prev_updated)

        cleaned_reason = reason.strip()
        video_binding = updated.binding_for(ArtifactKind.VIDEO)
        self._audit.record(
            APPROVAL_CHANGES_REQUESTED,
            actor,
            EntityRef(APPROVAL_ENTITY_TYPE, updated.id),
            AuditResult.SUCCESS,
            metadata={
                "content_item_id": updated.content_item_id,
                "video_artifact_id": (
                    video_binding.artifact_id if video_binding else None
                ),
                "video_artifact_version": (
                    video_binding.version if video_binding else None
                ),
                "reason": cleaned_reason,
                "target_kinds": (
                    ",".join(k.value for k in kinds_tuple) if kinds_tuple else None
                ),
            },
        )
        return ApprovalDecisionResult(
            approval=updated,
            content_item=item,
            decision="changes_requested",
            decided_by=actor,
            reason=cleaned_reason,
            target_kinds=kinds_tuple,
        )

    def expire_stale(
        self,
        content_item_id: str,
        *,
        actor: Actor = DEFAULT_REQUESTER,
        at: datetime | None = None,
    ) -> list[ApprovalRequest]:
        """Expire any PENDING or APPROVED request past ``self.ttl`` at ``at`` (#142)."""
        now = at if at is not None else self._now()
        expired_list: list[tuple[ApprovalRequest, ApprovalStatus]] = []
        with self._database.transaction() as conn:
            items_repo = ContentItemRepository(conn)
            approvals_repo = ApprovalRequestRepository(conn)
            item = items_repo.get(content_item_id)
            if item is None:
                raise ApprovalNotFoundError(
                    f"content item {content_item_id} was not found"
                )
            for req in approvals_repo.list_by_content_item(content_item_id):
                if req.status in EXPIRABLE and req.is_expired(now, self._ttl):
                    expired_req = req.expire()
                    approvals_repo.update(expired_req, expected_status=req.status)
                    expired_list.append((expired_req, req.status))

        for expired_req, prev_status in expired_list:
            self._audit.record(
                APPROVAL_EXPIRED,
                actor,
                EntityRef(APPROVAL_ENTITY_TYPE, expired_req.id),
                AuditResult.SUCCESS,
                metadata={
                    "content_item_id": content_item_id,
                    "previous_status": prev_status.value,
                    "expires_at": expired_req.expires_at(self._ttl).isoformat(),
                },
            )
        return [pair[0] for pair in expired_list]

    def revalidate(
        self,
        approval_request_id: str,
        *,
        requested_by: Actor = DEFAULT_REQUESTER,
        recipient_email: str | None = None,
        base_url: str | None = None,
    ) -> ApprovalDecisionResult:
        """Revalidate an expired or active request against current artifacts and QC.

        (#142)
        """
        with self._database.transaction() as conn:
            approvals_repo = ApprovalRequestRepository(conn)
            items_repo = ContentItemRepository(conn)
            artifacts_repo = ArtifactRepository(conn)
            qc_repo = QCResultRepository(conn)

            prior = approvals_repo.get(approval_request_id)
            if prior is None:
                raise ApprovalNotFoundError(
                    f"approval request {approval_request_id} was not found"
                )
            content_item_id = prior.content_item_id
            item = items_repo.get(content_item_id)
            if item is None:
                raise ApprovalNotFoundError(
                    f"content item {content_item_id} was not found"
                )

            all_artifacts = artifacts_repo.list_by_content_item(content_item_id)
            stale = stale_kinds(prior, all_artifacts)
            if stale:
                kinds_str = ", ".join(k.value for k in stale)
                raise ApprovalStateError(
                    f"cannot revalidate approval {prior.id}; "
                    f"artifacts changed for: {kinds_str}"
                )

            latest_artifacts = _latest_artifacts_by_kind(all_artifacts)
            video_artifact = next(
                (a for a in latest_artifacts if a.kind is ArtifactKind.VIDEO), None
            )
            if video_artifact is None:
                raise ApprovalStateError(
                    f"content item {content_item_id} has no video artifact"
                )

            qc_results = qc_repo.list_by_content_item(content_item_id)
            latest_qc: QCResult | None = qc_results[-1] if qc_results else None
            if latest_qc is None or latest_qc.status is QCStatus.FAIL:
                raise ApprovalStateError(
                    f"content item {content_item_id} does not have a passing QC result"
                )
            if video_artifact.id not in latest_qc.artifact_ids:
                raise ApprovalStateError(
                    f"latest QC result {latest_qc.id} does not cover "
                    f"video {video_artifact.id}"
                )

            if prior.status in EXPIRABLE:
                approvals_repo.update(prior.expire(), expected_status=prior.status)

            for existing in approvals_repo.list_by_content_item(content_item_id):
                if existing.id != prior.id and existing.status in INVALIDATABLE:
                    approvals_repo.update(
                        existing.invalidate(), expected_status=existing.status
                    )

            # Move item to AWAITING_APPROVAL using legal transitions
            if item.status is ContentStatus.APPROVED:
                for step in (
                    ContentStatus.GENERATING,
                    ContentStatus.TESTING,
                    ContentStatus.PREVIEW_READY,
                    ContentStatus.AWAITING_APPROVAL,
                ):
                    prev_updated = item.updated_at
                    item = item.with_status(step, clock=self._clock)
                    items_repo.update(item, expected_updated_at=prev_updated)
            elif item.status is ContentStatus.TESTING:
                for step in (
                    ContentStatus.PREVIEW_READY,
                    ContentStatus.AWAITING_APPROVAL,
                ):
                    prev_updated = item.updated_at
                    item = item.with_status(step, clock=self._clock)
                    items_repo.update(item, expected_updated_at=prev_updated)
            elif item.status is ContentStatus.PREVIEW_READY:
                prev_updated = item.updated_at
                item = item.with_status(
                    ContentStatus.AWAITING_APPROVAL, clock=self._clock
                )
                items_repo.update(item, expected_updated_at=prev_updated)
            elif item.status is not ContentStatus.AWAITING_APPROVAL:
                item.with_status(ContentStatus.AWAITING_APPROVAL, clock=self._clock)

            new_request = ApprovalRequest.create(
                content_item_id,
                latest_artifacts,
                requested_by=requested_by,
                qc_result=latest_qc,
                clock=self._clock,
            )
            approvals_repo.add(new_request)

        self._audit.record(
            APPROVAL_REVALIDATED,
            requested_by,
            EntityRef(APPROVAL_ENTITY_TYPE, new_request.id),
            AuditResult.SUCCESS,
            metadata={
                "content_item_id": content_item_id,
                "previous_approval_request_id": prior.id,
                "video_artifact_id": video_artifact.id,
                "video_artifact_version": video_artifact.version,
                "video_artifact_sha256": video_artifact.sha256,
                "qc_result_id": latest_qc.id,
            },
        )

        email_receipt: dict[str, Any] | None = None
        if recipient_email is not None:
            if self._email_service is None:
                raise ApprovalStateError(
                    "email_service is not configured to send approval emails"
                )
            email_receipt = self._email_service.send_approval_email(
                new_request.id,
                recipient_email,
                base_url=base_url,
                actor=requested_by,
            )

        return ApprovalDecisionResult(
            approval=new_request,
            content_item=item,
            decision="revalidated",
            decided_by=requested_by,
            email_receipt=email_receipt,
        )
