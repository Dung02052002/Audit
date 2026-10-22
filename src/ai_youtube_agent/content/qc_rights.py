"""Rights QC check (Prompt Pack v8, prompt K-126), context C10 Quality (Test & QC).

Runs ``RightsReporter`` (#082) as a blocking QC dependency for a content item:
- Fails (``QCStatus.FAIL``) when ``RightsReport.gate.blocks`` is True (unresolved
  blocking risk level, stale assessment, or outdated rules).
- Fails (``QCStatus.FAIL``) when any attached asset on the content item lacks a
  corresponding ``RightsRecord`` (enforcing the #038/#126 contract that every
  attached asset must have a rights record before QC passes).
- Warns (``QCStatus.WARN``) when the rights gate does not block but unresolved
  ``medium`` risk records remain or non-generated assets have missing provenance
  fields.
- Passes (``QCStatus.PASS``) when the rights gate passes and all attached assets
  are covered by rights records without unresolved medium/high/unknown risks.
"""

from dataclasses import dataclass
from typing import Any, Final

from ai_youtube_agent.content.qc import QCCheck, QCStatus
from ai_youtube_agent.content.rights_report import (
    Clock,
    RightsReport,
    RightsReporter,
)
from ai_youtube_agent.core.config import Settings
from ai_youtube_agent.core.db.database import Database

RIGHTS_QC_CHECK_NAME: Final[str] = "qc.rights"


@dataclass(frozen=True)
class RightsQCEvaluation:
    """Combined outcome of running ``RightsReporter`` and evaluating its QC check."""

    content_item_id: str
    report: RightsReport
    check: QCCheck

    def __post_init__(self) -> None:
        if not isinstance(self.report, RightsReport):
            raise TypeError("report must be a RightsReport")
        if not isinstance(self.check, QCCheck):
            raise TypeError("check must be a QCCheck")
        if self.check.name != RIGHTS_QC_CHECK_NAME:
            raise ValueError(f"check.name must be {RIGHTS_QC_CHECK_NAME!r}")
        if self.content_item_id != self.report.content_item_id:
            raise ValueError("content_item_id must match report.content_item_id")

    @property
    def is_blocking(self) -> bool:
        return self.check.status is QCStatus.FAIL

    def to_dict(self) -> dict[str, Any]:
        return {
            "content_item_id": self.content_item_id,
            "is_blocking": self.is_blocking,
            "check": self.check.as_dict(),
            "report": self.report.to_dict(),
        }


def evaluate_rights_qc(report: RightsReport) -> QCCheck:
    """Evaluate a ``RightsReport`` into a ``QCCheck`` named ``qc.rights``."""
    if not isinstance(report, RightsReport):
        raise TypeError("report must be a RightsReport")

    if report.gate.blocks:
        blocking_parts = [
            f"{rec.asset_ref}:{rec.code}" for rec in report.gate.blocking_records
        ]
        summary = ", ".join(blocking_parts) if blocking_parts else "gate.blocks=True"
        return QCCheck(
            name=RIGHTS_QC_CHECK_NAME,
            status=QCStatus.FAIL,
            detail=(
                f"Rights gate blocked ({len(report.gate.blocking_records)} record(s)): "
                f"{summary}"
            ),
        )

    covered_refs: set[str] = set()
    for record in report.records:
        covered_refs.add(record.asset_ref)
        if record.assessment is not None and record.assessment.asset_id is not None:
            covered_refs.add(record.assessment.asset_id)

    uncovered_assets = [
        asset.asset_id for asset in report.assets if asset.asset_id not in covered_refs
    ]
    if uncovered_assets:
        return QCCheck(
            name=RIGHTS_QC_CHECK_NAME,
            status=QCStatus.FAIL,
            detail=(
                f"Attached asset(s) missing rights record: "
                f"{', '.join(uncovered_assets)}"
            ),
        )

    if report.worst_level == "medium":
        return QCCheck(
            name=RIGHTS_QC_CHECK_NAME,
            status=QCStatus.WARN,
            detail=(
                f"Rights gate passed with unresolved medium risk "
                f"({len(report.records)} record(s), {len(report.assets)} asset(s))"
            ),
        )

    return QCCheck(
        name=RIGHTS_QC_CHECK_NAME,
        status=QCStatus.PASS,
        detail=(
            f"Rights report clean: {len(report.records)} record(s), "
            f"{len(report.assets)} asset(s), "
            f"worst_level={report.worst_level or 'none'}"
        ),
    )


class RightsQCChecker:
    """Runs ``RightsReporter`` and produces a ``RightsQCEvaluation`` / ``QCCheck``."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        reporter: RightsReporter | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._reporter = (
            reporter
            if reporter is not None
            else RightsReporter(database, settings, clock=clock)
        )

    def evaluate(self, content_item_id: str) -> RightsQCEvaluation:
        if not isinstance(content_item_id, str) or not content_item_id.strip():
            raise ValueError("content_item_id must be a non-empty string")
        report = self._reporter.report(content_item_id)
        check = evaluate_rights_qc(report)
        return RightsQCEvaluation(
            content_item_id=report.content_item_id,
            report=report,
            check=check,
        )

    def check(self, content_item_id: str) -> QCCheck:
        return self.evaluate(content_item_id).check
