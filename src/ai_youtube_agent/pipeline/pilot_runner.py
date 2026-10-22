"""Shorts Pilot Runner & Technical Report Service.

(Prompt Pack v8, prompt T-236), context C16/C12.

Executes or verifies a controlled Shorts pilot run with mandatory explicit human
approval (``ActorKind.USER``, R-08) and produces a structured ``ShortsPilotReport``
suitable for programmatic gating (T-237 ``LongFormUnlockGate``) and Markdown export.
Never performs unapproved or external live network uploads.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from ai_youtube_agent.content.approval import (
    ApprovalNotAllowedError,
    ApprovalRequest,
    ApprovalStatus,
)
from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.qc import QCResult, QCStatus
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    EntityRef,
)
from ai_youtube_agent.core.content_item import ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.publish import PublishJobRepository
from ai_youtube_agent.core.db.repositories.review import (
    ApprovalRequestRepository,
    QCResultRepository,
)
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.pipeline.publish import PublishJob, PublishStatus
from ai_youtube_agent.pipeline.shorts_publisher import ShortsPublisher
from ai_youtube_agent.providers.youtube_upload import YouTubePrivacyStatus

Clock = Callable[[], datetime]

PILOT_COMPLETED_ACTION: Final[str] = "pilot.shorts_completed"
PILOT_BLOCKED_ACTION: Final[str] = "pilot.shorts_blocked"
PILOT_ACTOR: Final[Actor] = Actor(ActorKind.SYSTEM, "shorts_pilot_runner")


class PilotRunnerError(DomainError):
    """Raised when a Shorts pilot run violates preconditions or safety invariants."""

    default_code = "domain.pilot_runner_error"
    default_user_message = "The Shorts pilot run could not be completed."


@dataclass(frozen=True)
class PilotStageCheck:
    """Verification result for one stage of the Shorts pilot."""

    stage: str
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ShortsPilotReport:
    """Structured technical report for a controlled Shorts pilot run (T-236)."""

    pilot_id: str
    channel_id: str
    content_item_id: str
    content_type: ContentType
    passed: bool
    approval_request_id: str | None
    approved_by_user_id: str | None
    video_artifact_id: str | None
    video_sha256: str | None
    qc_result_id: str | None
    qc_status: str | None
    publish_job_id: str | None
    youtube_video_id: str | None
    privacy_status: str | None
    stage_checks: tuple[PilotStageCheck, ...]
    blockers: tuple[str, ...]
    generated_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "pilot_id": self.pilot_id,
            "channel_id": self.channel_id,
            "content_item_id": self.content_item_id,
            "content_type": self.content_type.value,
            "passed": self.passed,
            "approval_request_id": self.approval_request_id,
            "approved_by_user_id": self.approved_by_user_id,
            "video_artifact_id": self.video_artifact_id,
            "video_sha256": self.video_sha256,
            "qc_result_id": self.qc_result_id,
            "qc_status": self.qc_status,
            "publish_job_id": self.publish_job_id,
            "youtube_video_id": self.youtube_video_id,
            "privacy_status": self.privacy_status,
            "stage_checks": [c.as_dict() for c in self.stage_checks],
            "blockers": list(self.blockers),
            "generated_at": self.generated_at.isoformat(),
        }

    def to_markdown(self) -> str:
        verdict = "PASS" if self.passed else "BLOCKED"
        lines = [
            f"# T-236 Shorts Pilot Technical Report (`{self.pilot_id}`)",
            "",
            f"- **Verdict:** `{verdict}`",
            f"- **Channel ID:** `{self.channel_id}`",
            f"- **Content Item ID:** `{self.content_item_id}`",
            f"- **Content Type:** `{self.content_type.value}`",
            f"- **Approval Request ID:** `{self.approval_request_id or 'none'}`",
            f"- **Approved By (Human User):** `{self.approved_by_user_id or 'none'}`",
            f"- **Video Artifact ID:** `{self.video_artifact_id or 'none'}`",
            f"- **Video SHA-256:** `{self.video_sha256 or 'none'}`",
            f"- **QC Status:** `{self.qc_status or 'none'}`",
            f"- **Publish Job ID:** `{self.publish_job_id or 'none'}`",
            f"- **YouTube Video ID (Controlled):** `{self.youtube_video_id or 'none'}`",
            f"- **Privacy Status:** `{self.privacy_status or 'none'}`",
            f"- **Generated At:** `{self.generated_at.isoformat()}`",
            "",
            "## Stage Verification Checks",
            "",
            "| Stage | Status | Detail |",
            "|---|---|---|",
        ]
        for check in self.stage_checks:
            status_str = "PASS" if check.passed else "FAIL"
            lines.append(f"| `{check.stage}` | `{status_str}` | {check.detail} |")
        if self.blockers:
            lines.extend(["", "## Blockers", ""])
            for blocker in self.blockers:
                lines.append(f"- {blocker}")
        return "\n".join(lines) + "\n"


class ShortsPilotRunner:
    """Runs or evaluates a controlled Shorts pilot requiring explicit user approval."""

    def __init__(
        self,
        database: Database,
        audit_log: AuditLog,
        *,
        shorts_publisher: ShortsPublisher | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._audit = audit_log
        self._publisher = shorts_publisher
        self._clock = clock

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)

    def run_pilot(
        self,
        content_item_id: str,
        *,
        reviewer: Actor,
        privacy_status: YouTubePrivacyStatus = YouTubePrivacyStatus.PRIVATE,
    ) -> ShortsPilotReport:
        """Executes controlled publishing for an approved Shorts pilot item and builds
        its report.

        Requires ``reviewer`` to be an explicit human user (``ActorKind.USER``, R-08).
        """
        if not isinstance(reviewer, Actor) or reviewer.kind is not ActorKind.USER:
            raise ApprovalNotAllowedError(
                "A Shorts pilot requires explicit human user approval (ActorKind.USER)."
            )

        with self._database.connection() as conn:
            item = ContentItemRepository(conn).get(content_item_id)
            if item is None:
                raise PilotRunnerError(f"Content item {content_item_id} was not found")
            if item.content_type is not ContentType.SHORTS:
                raise PilotRunnerError(
                    f"Shorts pilot requires ContentType.SHORTS, got "
                    f"{item.content_type.value}"
                )

        if item.status is ContentStatus.APPROVED and self._publisher is not None:
            self._publisher.publish(
                content_item_id,
                actor=PILOT_ACTOR,
                privacy_status=privacy_status,
            )

        return self.evaluate_pilot(
            content_item_id, reviewer=reviewer, privacy_status=privacy_status
        )

    def evaluate_pilot(
        self,
        content_item_id: str,
        *,
        reviewer: Actor | None = None,
        privacy_status: YouTubePrivacyStatus | None = None,
    ) -> ShortsPilotReport:
        """Inspects persisted state for ``content_item_id`` and generates a
        ``ShortsPilotReport``.

        An ``ApprovalRequest`` does not store who decided it (``approve`` only
        accepts a human ``ActorKind.USER``, so ``APPROVED`` already means a human
        approved). ``reviewer`` is the human who authorised the pilot run; it is
        reported as the approving user when the request is approved. A
        ``PublishResult`` does not store the privacy status, so the caller that
        published passes it as ``privacy_status``.
        """
        now = self._now()
        pilot_id = f"pilot-{uuid.uuid5(uuid.NAMESPACE_OID, content_item_id).hex[:12]}"

        with self._database.connection() as conn:
            item = ContentItemRepository(conn).get(content_item_id)
            if item is None:
                raise PilotRunnerError(f"Content item {content_item_id} was not found")
            channel = ChannelRepository(conn).get(item.channel_id)
            strategy = StrategyProfileRepository(conn).get_by_channel(item.channel_id)
            artifacts = ArtifactRepository(conn).list_by_content_item(content_item_id)
            qc_results = QCResultRepository(conn).list_by_content_item(content_item_id)
            approvals = ApprovalRequestRepository(conn).list_by_content_item(
                content_item_id
            )
            publish_jobs = PublishJobRepository(conn).list_by_content_item(
                content_item_id
            )

        checks: list[PilotStageCheck] = []
        blockers: list[str] = []

        # 1. Channel & Strategy check
        channel_ok = (
            channel is not None
            and channel.status is ChannelStatus.ACTIVE
            and strategy is not None
        )
        checks.append(
            PilotStageCheck(
                "channel_and_strategy",
                channel_ok,
                "Active channel and strategy profile present"
                if channel_ok
                else "Channel is missing/inactive or lacks a strategy profile",
            )
        )
        if not channel_ok:
            blockers.append("pilot.channel_or_strategy_invalid")

        # 2. Content type check
        type_ok = item.content_type is ContentType.SHORTS
        checks.append(
            PilotStageCheck(
                "content_type_shorts",
                type_ok,
                f"Content type is {item.content_type.value}",
            )
        )
        if not type_ok:
            blockers.append("pilot.not_shorts")

        # 3. Video & Metadata artifacts check
        video_arts = [a for a in artifacts if a.kind is ArtifactKind.VIDEO]
        meta_arts = [a for a in artifacts if a.kind is ArtifactKind.METADATA]
        latest_video: Artifact | None = (
            max(video_arts, key=lambda a: a.version) if video_arts else None
        )
        latest_meta: Artifact | None = (
            max(meta_arts, key=lambda a: a.version) if meta_arts else None
        )
        artifacts_ok = latest_video is not None and latest_meta is not None
        checks.append(
            PilotStageCheck(
                "artifacts_stored",
                artifacts_ok,
                (
                    f"Video v{latest_video.version} and "
                    f"Metadata v{latest_meta.version} stored"
                )
                if latest_video and latest_meta
                else "Missing VIDEO or METADATA artifact",
            )
        )
        if not artifacts_ok:
            blockers.append("pilot.artifacts_missing")

        # 4. QC result check
        latest_qc: QCResult | None = qc_results[-1] if qc_results else None
        qc_ok = (
            latest_qc is not None
            and latest_qc.status in (QCStatus.PASS, QCStatus.WARN)
            and latest_video is not None
            and latest_video.id in latest_qc.artifact_ids
        )
        checks.append(
            PilotStageCheck(
                "qc_verified",
                qc_ok,
                (
                    f"QC {latest_qc.id} status={latest_qc.status.value} "
                    f"covers video {latest_video.id}"
                )
                if qc_ok and latest_qc and latest_video
                else "Missing or failing QC result for latest video artifact",
            )
        )
        if not qc_ok:
            blockers.append("pilot.qc_not_passed")

        # 5. Explicit human approval check (R-08)
        latest_approval: ApprovalRequest | None = approvals[-1] if approvals else None
        approval_ok = (
            latest_approval is not None
            and latest_approval.status is ApprovalStatus.APPROVED
            and latest_video is not None
            and latest_approval.binding_for(ArtifactKind.VIDEO) is not None
            and latest_approval.binding_for(ArtifactKind.VIDEO).artifact_id  # type: ignore[union-attr]
            == latest_video.id
        )
        approver_id = (
            reviewer.id
            if approval_ok and reviewer is not None and reviewer.kind is ActorKind.USER
            else None
        )
        checks.append(
            PilotStageCheck(
                "human_approval",
                approval_ok,
                f"Human-approved request {latest_approval.id}"
                + (f" (pilot reviewer '{approver_id}')" if approver_id else "")
                if approval_ok and latest_approval
                else (
                    "No valid human (ActorKind.USER) approval bound to latest "
                    "video artifact"
                ),
            )
        )
        if not approval_ok:
            blockers.append("pilot.human_approval_missing")

        # 6. Controlled publish result check
        succeeded_jobs = [
            j
            for j in publish_jobs
            if j.status is PublishStatus.SUCCEEDED and j.result is not None
        ]
        latest_job: PublishJob | None = succeeded_jobs[-1] if succeeded_jobs else None
        publish_ok = (
            item.status is ContentStatus.PUBLISHED
            and latest_job is not None
            and latest_job.result is not None
            and bool(latest_job.result.youtube_video_id)
        )
        checks.append(
            PilotStageCheck(
                "controlled_publish",
                publish_ok,
                (
                    f"Published via job {latest_job.id} -> YouTube ID "
                    f"{latest_job.result.youtube_video_id}"
                )
                if publish_ok and latest_job and latest_job.result
                else "Item is not PUBLISHED or lacks a succeeded PublishJob receipt",
            )
        )
        if not publish_ok:
            blockers.append("pilot.publish_incomplete")

        passed = len(blockers) == 0
        report = ShortsPilotReport(
            pilot_id=pilot_id,
            channel_id=item.channel_id,
            content_item_id=item.id,
            content_type=item.content_type,
            passed=passed,
            approval_request_id=latest_approval.id if latest_approval else None,
            approved_by_user_id=approver_id,
            video_artifact_id=latest_video.id if latest_video else None,
            video_sha256=latest_video.sha256 if latest_video else None,
            qc_result_id=latest_qc.id if latest_qc else None,
            qc_status=latest_qc.status.value if latest_qc else None,
            publish_job_id=latest_job.id if latest_job else None,
            youtube_video_id=(
                latest_job.result.youtube_video_id
                if latest_job and latest_job.result
                else None
            ),
            privacy_status=(
                privacy_status.value
                if privacy_status is not None and latest_job and latest_job.result
                else None
            ),
            stage_checks=tuple(checks),
            blockers=tuple(blockers),
            generated_at=now,
        )

        self._audit.record(
            action=PILOT_COMPLETED_ACTION if passed else PILOT_BLOCKED_ACTION,
            actor=PILOT_ACTOR,
            entity=EntityRef("content_item", item.id),
            result=AuditResult.SUCCESS if passed else AuditResult.DENIED,
            metadata={
                "pilot_id": pilot_id,
                "channel_id": item.channel_id,
                "passed": passed,
                "blockers": ",".join(blockers),
                "youtube_video_id": report.youtube_video_id or "",
            },
        )
        return report
