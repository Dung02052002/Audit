"""Policy QC check (Prompt Pack v8, prompt K-127), context C10 Quality (Test & QC).

Runs ``PolicyReporter`` (#083) as a blocking QC dependency for a content item:
- Fails (``QCStatus.FAIL``) when ``PolicyReport.blocks`` is True
  (``PolicyStatus.BLOCK``).
- Warns (``QCStatus.WARN``) when ``PolicyReport.status`` is ``PolicyStatus.WARN``
  (non-blocking policy findings present).
- Passes (``QCStatus.PASS``) when ``PolicyReport.status`` is ``PolicyStatus.PASS``.
- Automatically loads ``description`` and ``tags`` from the item's latest
  ``ArtifactKind.METADATA`` artifact when ``storage`` is provided and explicit
  ``description``/``tags`` arguments are omitted.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from ai_youtube_agent.content.policy_check import (
    POLICY_RULES,
    PolicyRuleSetCatalog,
    PolicyStatus,
)
from ai_youtube_agent.content.policy_report import (
    Clock,
    PolicyReport,
    PolicyReporter,
)
from ai_youtube_agent.content.policy_rule import RuleSet
from ai_youtube_agent.content.qc import QCCheck, QCStatus
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.artifact_storage import ArtifactStorage
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import ArtifactRepository

POLICY_QC_CHECK_NAME: Final[str] = "qc.policy"


@dataclass(frozen=True)
class PolicyQCEvaluation:
    """Combined outcome of running ``PolicyReporter`` and evaluating its QC check."""

    content_item_id: str
    report: PolicyReport
    check: QCCheck

    def __post_init__(self) -> None:
        if not isinstance(self.report, PolicyReport):
            raise TypeError("report must be a PolicyReport")
        if not isinstance(self.check, QCCheck):
            raise TypeError("check must be a QCCheck")
        if self.check.name != POLICY_QC_CHECK_NAME:
            raise ValueError(f"check.name must be {POLICY_QC_CHECK_NAME!r}")
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


def evaluate_policy_qc(report: PolicyReport) -> QCCheck:
    """Evaluate a ``PolicyReport`` into a ``QCCheck`` named ``qc.policy``."""
    if not isinstance(report, PolicyReport):
        raise TypeError("report must be a PolicyReport")

    if report.blocks or report.status is PolicyStatus.BLOCK:
        rule_ids = [finding.rule_id for finding in report.blocking_findings]
        summary = ", ".join(rule_ids) if rule_ids else "status=block"
        return QCCheck(
            name=POLICY_QC_CHECK_NAME,
            status=QCStatus.FAIL,
            detail=(
                f"Policy gate blocked ({report.rule_set.stored_version}, "
                f"{len(report.blocking_findings)} blocking finding(s)): {summary}"
            ),
        )

    if report.status is PolicyStatus.WARN:
        rule_ids = [finding.rule_id for finding in report.findings]
        summary = ", ".join(rule_ids) if rule_ids else "status=warn"
        return QCCheck(
            name=POLICY_QC_CHECK_NAME,
            status=QCStatus.WARN,
            detail=(
                f"Policy report passed with warning(s) "
                f"({report.rule_set.stored_version}, "
                f"{len(report.findings)} finding(s)): {summary}"
            ),
        )

    return QCCheck(
        name=POLICY_QC_CHECK_NAME,
        status=QCStatus.PASS,
        detail=(
            f"Policy report clean ({report.rule_set.stored_version}, "
            f"{len(report.outcomes)} rule(s) passed)"
        ),
    )


class PolicyQCChecker:
    """Runs ``PolicyReporter`` and produces a ``PolicyQCEvaluation`` / ``QCCheck``."""

    def __init__(
        self,
        database: Database,
        *,
        storage: ArtifactStorage | None = None,
        reporter: PolicyReporter | None = None,
        rule_set: RuleSet = POLICY_RULES,
        catalog: PolicyRuleSetCatalog | None = None,
        clock: Clock | None = None,
    ) -> None:
        if not isinstance(rule_set, RuleSet):
            raise TypeError("rule_set must be a RuleSet")
        self._database = database
        self._storage = storage
        self._rule_set = rule_set
        self._reporter = (
            reporter
            if reporter is not None
            else PolicyReporter(database, clock=clock, catalog=catalog)
        )

    def evaluate(
        self,
        content_item_id: str,
        *,
        rule_set: RuleSet | None = None,
        description: str | None = None,
        tags: Sequence[str] | None = None,
    ) -> PolicyQCEvaluation:
        if not isinstance(content_item_id, str) or not content_item_id.strip():
            raise ValueError("content_item_id must be a non-empty string")
        effective_rule_set = rule_set if rule_set is not None else self._rule_set
        resolved_description = description
        resolved_tags: tuple[str, ...] | list[str] = (
            list(tags) if tags is not None else ()
        )
        if description is None and tags is None and self._storage is not None:
            meta_desc, meta_tags = self._load_metadata_text(content_item_id)
            if meta_desc is not None:
                resolved_description = meta_desc
            if meta_tags is not None:
                resolved_tags = meta_tags

        report = self._reporter.report(
            content_item_id,
            rule_set=effective_rule_set,
            description=resolved_description,
            tags=resolved_tags,
        )
        check = evaluate_policy_qc(report)
        return PolicyQCEvaluation(
            content_item_id=report.content_item_id,
            report=report,
            check=check,
        )

    def check(
        self,
        content_item_id: str,
        *,
        rule_set: RuleSet | None = None,
        description: str | None = None,
        tags: Sequence[str] | None = None,
    ) -> QCCheck:
        return self.evaluate(
            content_item_id,
            rule_set=rule_set,
            description=description,
            tags=tags,
        ).check

    def _load_metadata_text(
        self, content_item_id: str
    ) -> tuple[str | None, tuple[str, ...] | None]:
        assert self._storage is not None
        with self._database.transaction() as connection:
            artifacts = ArtifactRepository(connection).list_by_content_item(
                content_item_id, kind=ArtifactKind.METADATA
            )
        if not artifacts:
            return None, None
        latest = artifacts[-1]
        try:  # a missing or unreadable file counts as no metadata
            payload = json.loads(self._storage.read(latest.uri).decode("utf-8"))
        except Exception:
            return None, None
        if not isinstance(payload, dict):
            return None, None
        desc = payload.get("description")
        raw_tags = payload.get("tags")
        parsed_desc = desc if isinstance(desc, str) else None
        parsed_tags = (
            tuple(t for t in raw_tags if isinstance(t, str))
            if isinstance(raw_tags, list)
            else None
        )
        return parsed_desc, parsed_tags
