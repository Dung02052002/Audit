"""Aggregated QC Report & Gates (Prompt Pack v8, prompt K-129), context C10 Quality
(Test & QC).

Aggregates the 9 technical/artifact checks (#117–#125) and the 3 compliance QC
checks (#126–#128) into a structured ``PASS`` / ``WARN`` / ``FAIL`` ``QCReport``
backed by an immutable ``QCResult`` stored in ``QCResultRepository``:
- Video & artifact checks (#117–#125):
  - ``video.container``
  - ``video.resolution``
  - ``video.fps``
  - ``video.duration``
  - ``video.audio``
  - ``video.subtitles``
  - ``video.black_frames``
  - ``video.integrity``
  - ``video.metadata``
- Compliance QC dependencies (#126–#128):
  - ``qc.rights``
  - ``qc.policy``
  - ``qc.ai_disclosure``
- Also provides ``TestGate`` (``GateName.TEST``) and ``QCGate`` (``GateName.QC``)
  implementing ``PipelineGate`` (#033).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from ai_youtube_agent.content.hook_generator import ContentItemNotFoundError
from ai_youtube_agent.content.qc import QCCheck, QCResult, QCStatus
from ai_youtube_agent.content.qc_disclosure import (
    DISCLOSURE_QC_CHECK_NAME,
    DisclosureQCChecker,
    DisclosureQCEvaluation,
)
from ai_youtube_agent.content.qc_harness import (
    QCHarnessError,
    QCHarnessErrorCode,
    VideoArtifactTestHarness,
    all_video_checks,
)
from ai_youtube_agent.content.qc_policy import (
    POLICY_QC_CHECK_NAME,
    PolicyQCChecker,
    PolicyQCEvaluation,
)
from ai_youtube_agent.content.qc_rights import (
    RIGHTS_QC_CHECK_NAME,
    RightsQCChecker,
    RightsQCEvaluation,
)
from ai_youtube_agent.content.qc_video_checks import (
    AUDIO_CHECK_NAME,
    BLACK_FRAME_CHECK_NAME,
    CONTAINER_CHECK_NAME,
    DURATION_CHECK_NAME,
    FPS_CHECK_NAME,
    INTEGRITY_CHECK_NAME,
    METADATA_CHECK_NAME,
    RESOLUTION_CHECK_NAME,
    SUBTITLE_CHECK_NAME,
)
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import ArtifactStorage
from ai_youtube_agent.core.audit import (
    Actor,
    AuditLog,
    AuditResult,
    EntityRef,
)
from ai_youtube_agent.core.config import Settings
from ai_youtube_agent.core.content_item import ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.review import QCResultRepository
from ai_youtube_agent.core.gates import (
    GateContext,
    GateName,
    GateReason,
    GateResult,
)

QC_REPORT_SCHEMA_VERSION: Final[str] = "qc-report-v1"
QC_REPORT_AUDIT_ACTION: Final[str] = "qc.report_generated"

VIDEO_CHECK_NAMES: Final[tuple[str, ...]] = (
    CONTAINER_CHECK_NAME,
    RESOLUTION_CHECK_NAME,
    FPS_CHECK_NAME,
    DURATION_CHECK_NAME,
    AUDIO_CHECK_NAME,
    SUBTITLE_CHECK_NAME,
    BLACK_FRAME_CHECK_NAME,
    INTEGRITY_CHECK_NAME,
    METADATA_CHECK_NAME,
)
COMPLIANCE_CHECK_NAMES: Final[tuple[str, ...]] = (
    RIGHTS_QC_CHECK_NAME,
    POLICY_QC_CHECK_NAME,
    DISCLOSURE_QC_CHECK_NAME,
)
ALL_QC_CHECK_NAMES: Final[tuple[str, ...]] = VIDEO_CHECK_NAMES + COMPLIANCE_CHECK_NAMES

_TRACKED_ARTIFACT_KINDS: Final[tuple[ArtifactKind, ...]] = (
    ArtifactKind.VIDEO,
    ArtifactKind.AUDIO,
    ArtifactKind.SUBTITLES,
    ArtifactKind.THUMBNAIL,
    ArtifactKind.METADATA,
)


@dataclass(frozen=True)
class QCReport:
    """Structured PASS/WARN/FAIL QC report aggregating video and compliance checks."""

    qc_result: QCResult
    content_item_id: str
    channel_id: str
    content_type: ContentType
    status: QCStatus
    blocks: bool
    artifact_ids: tuple[str, ...]
    checks: tuple[QCCheck, ...]
    passed_checks: tuple[QCCheck, ...]
    warning_checks: tuple[QCCheck, ...]
    failed_checks: tuple[QCCheck, ...]
    rights_evaluation: RightsQCEvaluation
    policy_evaluation: PolicyQCEvaluation
    disclosure_evaluation: DisclosureQCEvaluation
    generated_at: datetime
    schema_version: str = QC_REPORT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.qc_result, QCResult):
            raise TypeError("qc_result must be a QCResult")
        if self.content_item_id != self.qc_result.content_item_id:
            raise ValueError("content_item_id must match qc_result.content_item_id")
        if self.status is not self.qc_result.status:
            raise ValueError("status must match qc_result.status")
        if self.blocks is not (self.status is QCStatus.FAIL):
            raise ValueError("blocks must be True exactly when status is FAIL")
        if self.checks != self.qc_result.checks:
            raise ValueError("checks must match qc_result.checks")
        if self.artifact_ids != self.qc_result.artifact_ids:
            raise ValueError("artifact_ids must match qc_result.artifact_ids")
        if self.passed_checks != tuple(
            c for c in self.checks if c.status is QCStatus.PASS
        ):
            raise ValueError("passed_checks must contain all PASS checks in order")
        if self.warning_checks != tuple(
            c for c in self.checks if c.status is QCStatus.WARN
        ):
            raise ValueError("warning_checks must contain all WARN checks in order")
        if self.failed_checks != tuple(
            c for c in self.checks if c.status is QCStatus.FAIL
        ):
            raise ValueError("failed_checks must contain all FAIL checks in order")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "qc_result_id": self.qc_result.id,
            "content_item_id": self.content_item_id,
            "channel_id": self.channel_id,
            "content_type": self.content_type.value,
            "status": self.status.value,
            "blocks": self.blocks,
            "artifact_ids": list(self.artifact_ids),
            "summary": {
                "total": len(self.checks),
                "passed": len(self.passed_checks),
                "warnings": len(self.warning_checks),
                "failed": len(self.failed_checks),
            },
            "checks": [c.as_dict() for c in self.checks],
            "passed_checks": [c.as_dict() for c in self.passed_checks],
            "warning_checks": [c.as_dict() for c in self.warning_checks],
            "failed_checks": [c.as_dict() for c in self.failed_checks],
            "compliance": {
                "rights": self.rights_evaluation.to_dict(),
                "policy": self.policy_evaluation.to_dict(),
                "ai_disclosure": self.disclosure_evaluation.to_dict(),
            },
            "generated_at": self.generated_at.isoformat(),
        }


class QCResultSource(Protocol):
    def list_by_content_item(self, content_item_id: str) -> Sequence[QCResult]: ...


class ArtifactListSource(Protocol):
    def list_by_content_item(self, content_item_id: str) -> Sequence[Artifact]: ...


def _latest_tracked_artifact_ids(artifacts: Sequence[Artifact]) -> tuple[str, ...]:
    latest_by_kind: dict[ArtifactKind, Artifact] = {}
    for artifact in artifacts:
        if artifact.kind in _TRACKED_ARTIFACT_KINDS:
            prev = latest_by_kind.get(artifact.kind)
            if prev is None or artifact.version >= prev.version:
                latest_by_kind[artifact.kind] = artifact
    return tuple(
        latest_by_kind[kind].id
        for kind in _TRACKED_ARTIFACT_KINDS
        if kind in latest_by_kind
    )


class TestGate:
    """Pipeline gate for ``GateName.TEST`` verifying video/artifact test checks."""

    __test__ = False
    name = GateName.TEST

    def __init__(
        self,
        qc_results: QCResultSource,
        artifacts: ArtifactListSource,
    ) -> None:
        self._qc_results = qc_results
        self._artifacts = artifacts

    def evaluate(self, context: GateContext) -> GateResult:
        if context.target_status not in (
            ContentStatus.PREVIEW_READY,
            ContentStatus.PUBLISHING,
        ):
            return GateResult.passed(self.name, context.at)
        results = self._qc_results.list_by_content_item(context.item.id)
        if not results:
            return GateResult.blocked(
                self.name,
                context.at,
                GateReason("test.missing", "No video artifact test result exists."),
            )
        newest = max(results, key=lambda r: (r.created_at, r.id))
        current_ids = set(
            _latest_tracked_artifact_ids(
                self._artifacts.list_by_content_item(context.item.id)
            )
        )
        if not current_ids or not current_ids.issubset(set(newest.artifact_ids)):
            return GateResult.blocked(
                self.name,
                context.at,
                GateReason(
                    "test.stale",
                    "The latest test result does not cover current artifacts.",
                ),
            )
        video_checks = [c for c in newest.checks if c.name.startswith("video.")]
        if not video_checks:
            return GateResult.blocked(
                self.name,
                context.at,
                GateReason("test.missing", "No video checks recorded in QC result."),
            )
        failed = [c for c in video_checks if c.status is QCStatus.FAIL]
        if failed:
            return GateResult.blocked(
                self.name,
                context.at,
                *(
                    GateReason(
                        "test.failed",
                        f"Video check {c.name} failed: {c.detail or 'failed'}.",
                    )
                    for c in failed
                ),
            )
        return GateResult.passed(self.name, context.at)


class QCGate:
    """Pipeline gate for ``GateName.QC`` verifying overall QC report pass/warn."""

    name = GateName.QC

    def __init__(
        self,
        qc_results: QCResultSource,
        artifacts: ArtifactListSource,
    ) -> None:
        self._qc_results = qc_results
        self._artifacts = artifacts

    def evaluate(self, context: GateContext) -> GateResult:
        if context.target_status not in (
            ContentStatus.PREVIEW_READY,
            ContentStatus.PUBLISHING,
        ):
            return GateResult.passed(self.name, context.at)
        results = self._qc_results.list_by_content_item(context.item.id)
        if not results:
            return GateResult.blocked(
                self.name,
                context.at,
                GateReason("qc.missing", "No QC result exists for this content item."),
            )
        newest = max(results, key=lambda r: (r.created_at, r.id))
        current_ids = set(
            _latest_tracked_artifact_ids(
                self._artifacts.list_by_content_item(context.item.id)
            )
        )
        if not current_ids or not current_ids.issubset(set(newest.artifact_ids)):
            return GateResult.blocked(
                self.name,
                context.at,
                GateReason(
                    "qc.stale",
                    "The latest QC result does not cover current artifacts.",
                ),
            )
        if newest.status is QCStatus.FAIL:
            failed = [c for c in newest.checks if c.status is QCStatus.FAIL]
            return GateResult.blocked(
                self.name,
                context.at,
                *(
                    GateReason(
                        "qc.failed",
                        f"QC check {c.name} failed: {c.detail or 'failed'}.",
                    )
                    for c in failed
                ),
            )
        return GateResult.passed(self.name, context.at)


class QCReportBuilder:
    """Runs all 9 video/artifact checks and 3 compliance QC checks to build a
    ``QCReport``.
    """

    def __init__(
        self,
        database: Database,
        storage: ArtifactStorage,
        audit: AuditLog,
        settings: Settings,
        *,
        harness: VideoArtifactTestHarness | None = None,
        rights_checker: RightsQCChecker | None = None,
        policy_checker: PolicyQCChecker | None = None,
        disclosure_checker: DisclosureQCChecker | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._audit = audit
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(UTC))
        self._harness = (
            harness
            if harness is not None
            else VideoArtifactTestHarness(
                database,
                storage,
                audit,
                checks=all_video_checks(),
                clock=self._clock,
            )
        )
        for name, fn in all_video_checks().items():
            if name not in self._harness.registered_checks:
                self._harness.register_check(name, fn)
        self._rights_checker = (
            rights_checker
            if rights_checker is not None
            else RightsQCChecker(database, settings, clock=self._clock)
        )
        self._policy_checker = (
            policy_checker
            if policy_checker is not None
            else PolicyQCChecker(database, storage=storage, clock=self._clock)
        )
        self._disclosure_checker = (
            disclosure_checker
            if disclosure_checker is not None
            else DisclosureQCChecker(database, storage=storage)
        )

    def generate(
        self,
        content_item_id: str,
        *,
        actor: Actor,
        persist: bool = True,
        actual_width: int | None = None,
        actual_height: int | None = None,
        actual_fps: float | None = None,
        expected_duration_ms: int | None = None,
        frame_luma_samples: Sequence[float] | None = None,
        frame_hashes: Sequence[str] | None = None,
    ) -> QCReport:
        if not isinstance(content_item_id, str) or not content_item_id.strip():
            raise ValueError("content_item_id must be a non-empty string")

        with self._database.transaction() as connection:
            item = ContentItemRepository(connection).get(content_item_id)
            if item is None:
                raise ContentItemNotFoundError(
                    f"content item {content_item_id} does not exist"
                )
            all_item_artifacts = ArtifactRepository(connection).list_by_content_item(
                item.id
            )

        tracked_artifacts = self._collect_latest_artifacts(item.id, all_item_artifacts)

        _item, _bound_artifacts, context = self._harness.build_inspection_context(
            item.id,
            actual_width=actual_width,
            actual_height=actual_height,
            actual_fps=actual_fps,
            expected_duration_ms=expected_duration_ms,
            frame_luma_samples=frame_luma_samples,
            frame_hashes=frame_hashes,
        )
        video_checks = self._harness.run_checks(context, check_names=VIDEO_CHECK_NAMES)

        rights_eval = self._rights_checker.evaluate(item.id)
        policy_eval = self._policy_checker.evaluate(item.id)
        disclosure_eval = self._disclosure_checker.evaluate(
            item.id, require_metadata=True
        )

        all_checks: tuple[QCCheck, ...] = (
            *video_checks,
            rights_eval.check,
            policy_eval.check,
            disclosure_eval.check,
        )
        now = self._clock()
        qc_result = QCResult.create(
            content_item_id=item.id,
            artifacts=tracked_artifacts,
            checks=all_checks,
            clock=lambda: now,
        )

        if persist:
            with self._database.transaction() as connection:
                QCResultRepository(connection).add(qc_result)
            self._audit.record(
                action=QC_REPORT_AUDIT_ACTION,
                actor=actor,
                entity=EntityRef("qc_result", qc_result.id),
                result=(
                    AuditResult.FAILURE
                    if qc_result.status is QCStatus.FAIL
                    else AuditResult.SUCCESS
                ),
                metadata={
                    "content_item_id": item.id,
                    "channel_id": item.channel_id,
                    "status": qc_result.status.value,
                    "artifact_count": len(qc_result.artifact_ids),
                    "total_checks": len(all_checks),
                    "pass_count": sum(
                        1 for c in all_checks if c.status is QCStatus.PASS
                    ),
                    "warn_count": sum(
                        1 for c in all_checks if c.status is QCStatus.WARN
                    ),
                    "fail_count": sum(
                        1 for c in all_checks if c.status is QCStatus.FAIL
                    ),
                    "failed_checks": ",".join(
                        c.name for c in all_checks if c.status is QCStatus.FAIL
                    ),
                    "warning_checks": ",".join(
                        c.name for c in all_checks if c.status is QCStatus.WARN
                    ),
                },
            )

        return QCReport(
            qc_result=qc_result,
            content_item_id=item.id,
            channel_id=item.channel_id,
            content_type=item.content_type,
            status=qc_result.status,
            blocks=qc_result.status is QCStatus.FAIL,
            artifact_ids=qc_result.artifact_ids,
            checks=qc_result.checks,
            passed_checks=tuple(
                c for c in qc_result.checks if c.status is QCStatus.PASS
            ),
            warning_checks=tuple(
                c for c in qc_result.checks if c.status is QCStatus.WARN
            ),
            failed_checks=tuple(
                c for c in qc_result.checks if c.status is QCStatus.FAIL
            ),
            rights_evaluation=rights_eval,
            policy_evaluation=policy_eval,
            disclosure_evaluation=disclosure_eval,
            generated_at=now,
        )

    @staticmethod
    def _collect_latest_artifacts(
        content_item_id: str, artifacts: Sequence[Artifact]
    ) -> tuple[Artifact, ...]:
        latest_by_kind: dict[ArtifactKind, Artifact] = {}
        for artifact in artifacts:
            if artifact.kind in _TRACKED_ARTIFACT_KINDS:
                prev = latest_by_kind.get(artifact.kind)
                if prev is None or artifact.version >= prev.version:
                    latest_by_kind[artifact.kind] = artifact
        if ArtifactKind.VIDEO not in latest_by_kind:
            raise QCHarnessError(
                f"Content item {content_item_id!r} has no rendered video artifact",
                code=QCHarnessErrorCode.VIDEO_MISSING,
            )
        return tuple(
            latest_by_kind[kind]
            for kind in _TRACKED_ARTIFACT_KINDS
            if kind in latest_by_kind
        )
