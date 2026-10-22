"""Preview Reports service (Prompt Pack v8, prompt L-134), context C11 Review.

Exposes the QC report, Rights report, and Policy report together for human
review during preview:
- Loads the latest ``QCResult`` for the content item and checks whether its bound
  ``artifact_ids`` match the item's current artifact versions.
- Runs ``RightsReporter.report`` to surface asset rights assessments, provenance
  completeness, AI disclosure decision, and ``RightsGate`` verdict.
- Runs ``PolicyReporter.report`` (including publish metadata description and tags
  when available) at the configured policy rule set version.
- Summarises ``ready_for_approval`` and any ``blocking_reasons`` so the reviewer
  sees why an item can or cannot be approved.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ai_youtube_agent.content.policy_report import PolicyReport, PolicyReporter
from ai_youtube_agent.content.policy_rule import RuleSet
from ai_youtube_agent.content.preview_artifact import (
    PreviewError,
    PreviewErrorCode,
)
from ai_youtube_agent.content.qc import QCCheck, QCResult, QCStatus
from ai_youtube_agent.content.rights_report import RightsReport, RightsReporter
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import ArtifactStorage
from ai_youtube_agent.core.config import Settings
from ai_youtube_agent.core.content_item import ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.review import QCResultRepository


@dataclass(frozen=True)
class PreviewQCSummary:
    """Structured view of the latest ``QCResult`` shown during preview (#134)."""

    qc_result_id: str
    content_item_id: str
    status: QCStatus
    created_at: datetime
    artifact_ids: tuple[str, ...]
    is_current_for_artifacts: bool
    checks: tuple[QCCheck, ...]
    blocking_checks: tuple[QCCheck, ...]
    warn_checks: tuple[QCCheck, ...]
    pass_count: int
    warn_count: int
    fail_count: int

    @classmethod
    def from_qc_result(
        cls,
        result: QCResult,
        *,
        latest_artifacts: tuple[Artifact, ...],
    ) -> "PreviewQCSummary":
        latest_ids = {a.id for a in latest_artifacts}
        bound_ids = set(result.artifact_ids)
        video_arts = [a for a in latest_artifacts if a.kind is ArtifactKind.VIDEO]
        video_covered = bool(video_arts) and all(v.id in bound_ids for v in video_arts)
        is_current = video_covered and bound_ids.issubset(latest_ids)

        blocking = tuple(c for c in result.checks if c.status is QCStatus.FAIL)
        warned = tuple(c for c in result.checks if c.status is QCStatus.WARN)
        passed_count = sum(1 for c in result.checks if c.status is QCStatus.PASS)
        return cls(
            qc_result_id=result.id,
            content_item_id=result.content_item_id,
            status=result.status,
            created_at=result.created_at,
            artifact_ids=result.artifact_ids,
            is_current_for_artifacts=is_current,
            checks=result.checks,
            blocking_checks=blocking,
            warn_checks=warned,
            pass_count=passed_count,
            warn_count=len(warned),
            fail_count=len(blocking),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "qc_result_id": self.qc_result_id,
            "content_item_id": self.content_item_id,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "artifact_ids": list(self.artifact_ids),
            "is_current_for_artifacts": self.is_current_for_artifacts,
            "pass_count": self.pass_count,
            "warn_count": self.warn_count,
            "fail_count": self.fail_count,
            "checks": [c.as_dict() for c in self.checks],
            "blocking_checks": [c.as_dict() for c in self.blocking_checks],
            "warn_checks": [c.as_dict() for c in self.warn_checks],
        }


@dataclass(frozen=True)
class PreviewReportsBundle:
    """Combined QC, Rights, and Policy reports for human review (#134)."""

    content_item_id: str
    channel_id: str
    content_type: ContentType
    status: ContentStatus
    qc: PreviewQCSummary | None
    rights_report: RightsReport
    policy_report: PolicyReport
    ready_for_approval: bool
    blocking_reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "content_item_id": self.content_item_id,
            "channel_id": self.channel_id,
            "content_type": self.content_type.value,
            "status": self.status.value,
            "qc": self.qc.as_dict() if self.qc is not None else None,
            "rights_report": self.rights_report.to_dict(),
            "policy_report": self.policy_report.to_dict(),
            "ready_for_approval": self.ready_for_approval,
            "blocking_reasons": list(self.blocking_reasons),
        }


class PreviewReportService:
    """Aggregates QC, Rights, and Policy reports for preview (#134)."""

    def __init__(
        self,
        database: Database,
        storage: ArtifactStorage,
        settings: Settings,
        *,
        rights_reporter: RightsReporter | None = None,
        policy_reporter: PolicyReporter | None = None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._settings = settings
        self._rights_reporter = rights_reporter or RightsReporter(database, settings)
        self._policy_reporter = policy_reporter or PolicyReporter(database)

    def get_reports(self, content_item_id: str) -> PreviewReportsBundle:
        """Load the QC, Rights, and Policy reports for ``content_item_id``."""
        with self._database.transaction() as connection:
            item = ContentItemRepository(connection).get(content_item_id)
            if item is None:
                raise PreviewError(
                    f"content item {content_item_id} not found",
                    code=PreviewErrorCode.ITEM_NOT_FOUND,
                )
            artifacts_repo = ArtifactRepository(connection)
            latest_by_kind: list[Artifact] = []
            for kind in ArtifactKind:
                latest_art = artifacts_repo.latest(content_item_id, kind)
                if latest_art is not None:
                    latest_by_kind.append(latest_art)
            qc_results = QCResultRepository(connection).list_by_content_item(
                content_item_id
            )
            latest_qc = qc_results[-1] if qc_results else None
            meta_art = artifacts_repo.latest(content_item_id, ArtifactKind.METADATA)

        description: str | None = None
        tags: tuple[str, ...] = ()
        if meta_art is not None:
            try:
                raw_meta = self._storage.read(meta_art.uri)
                payload = json.loads(raw_meta.decode("utf-8"))
                if isinstance(payload, dict):
                    if isinstance(payload.get("description"), str):
                        description = payload["description"]
                    raw_tags = payload.get("tags")
                    if isinstance(raw_tags, list) and all(
                        isinstance(t, str) for t in raw_tags
                    ):
                        tags = tuple(raw_tags)
            except Exception:
                pass

        qc_summary = (
            PreviewQCSummary.from_qc_result(
                latest_qc, latest_artifacts=tuple(latest_by_kind)
            )
            if latest_qc is not None
            else None
        )

        rights_report = self._rights_reporter.report(content_item_id)
        policy_rule_set = RuleSet("policy", self._settings.policy_rule_set_version)
        policy_report = self._policy_reporter.report(
            content_item_id,
            rule_set=policy_rule_set,
            description=description,
            tags=tags,
        )

        reasons: list[str] = []
        if not any(a.kind is ArtifactKind.VIDEO for a in latest_by_kind):
            reasons.append("video.missing: No rendered video artifact exists.")
        if qc_summary is None:
            reasons.append(
                "qc.missing: No QC report has been recorded for this content item."
            )
        elif not qc_summary.is_current_for_artifacts:
            reasons.append(
                "qc.stale: Latest QC report does not cover the current artifact "
                "versions."
            )
        elif qc_summary.status is QCStatus.FAIL:
            failed_names = ", ".join(c.name for c in qc_summary.blocking_checks)
            reasons.append(f"qc.failed: Blocking QC checks failed ({failed_names}).")

        if rights_report.gate.blocks:
            codes = ", ".join(
                r.gate_code or "rights.blocked"
                for r in rights_report.gate.blocking_records
            )
            reasons.append(f"rights.blocked: Rights gate blocked ({codes}).")

        if policy_report.blocks:
            rule_ids = ", ".join(f.rule_id for f in policy_report.blocking_findings)
            reasons.append(f"policy.blocked: Policy check blocked ({rule_ids}).")

        return PreviewReportsBundle(
            content_item_id=item.id,
            channel_id=item.channel_id,
            content_type=item.content_type,
            status=item.status,
            qc=qc_summary,
            rights_report=rights_report,
            policy_report=policy_report,
            ready_for_approval=(len(reasons) == 0),
            blocking_reasons=tuple(reasons),
        )
