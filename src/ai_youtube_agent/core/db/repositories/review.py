"""Repositories for rights records, QC results and approval requests."""

import sqlite3
from datetime import datetime

from ai_youtube_agent.content.approval import (
    ApprovalRequest,
    ApprovalStatus,
    ArtifactBinding,
)
from ai_youtube_agent.content.qc import QCCheck, QCResult, QCStatus
from ai_youtube_agent.content.rights import RightsRecord, RiskLevel, RiskResolution
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.db.repositories.base import (
    Repository,
    actor_columns,
    actor_from,
    dt,
    parse_dt,
)


class RightsRecordRepository(Repository):
    table = "rights_records"

    def add(self, record: RightsRecord) -> None:
        self._insert(self.table, _rights_row(record))

    def get(self, record_id: str) -> RightsRecord | None:
        row = self._one("SELECT * FROM rights_records WHERE id = ?", (record_id,))
        return _rights(row) if row else None

    def list_by_content_item(self, content_item_id: str) -> list[RightsRecord]:
        rows = self._all(
            "SELECT * FROM rights_records WHERE content_item_id = ? "
            "ORDER BY created_at, id",
            (content_item_id,),
        )
        return [_rights(row) for row in rows]

    def update(self, record: RightsRecord, *, expected_updated_at: datetime) -> None:
        values = _rights_row(record)
        for column in ("id", "content_item_id", "asset_ref", "created_at"):
            del values[column]
        self._update(
            record.id,
            values,
            guard_column="updated_at",
            guard_value=dt(expected_updated_at),
        )


class QCResultRepository(Repository):
    """A QC result is complete when created: add only."""

    table = "qc_results"

    def add(self, result: QCResult) -> None:
        self._insert(
            "qc_results",
            {
                "id": result.id,
                "content_item_id": result.content_item_id,
                "created_at": dt(result.created_at),
            },
        )
        for position, artifact_id in enumerate(result.artifact_ids):
            self._insert(
                "qc_result_artifacts",
                {
                    "qc_result_id": result.id,
                    "position": position,
                    "artifact_id": artifact_id,
                },
            )
        for position, check in enumerate(result.checks):
            self._insert(
                "qc_checks",
                {
                    "qc_result_id": result.id,
                    "position": position,
                    "name": check.name,
                    "status": check.status.value,
                    "detail": check.detail,
                },
            )

    def get(self, result_id: str) -> QCResult | None:
        row = self._one("SELECT * FROM qc_results WHERE id = ?", (result_id,))
        return self._result(row) if row else None

    def list_by_content_item(self, content_item_id: str) -> list[QCResult]:
        rows = self._all(
            "SELECT * FROM qc_results WHERE content_item_id = ? "
            "ORDER BY created_at, id",
            (content_item_id,),
        )
        return [self._result(row) for row in rows]

    def _result(self, row: sqlite3.Row) -> QCResult:
        artifacts = self._all(
            "SELECT artifact_id FROM qc_result_artifacts WHERE qc_result_id = ? "
            "ORDER BY position",
            (row["id"],),
        )
        checks = self._all(
            "SELECT * FROM qc_checks WHERE qc_result_id = ? ORDER BY position",
            (row["id"],),
        )
        return QCResult(
            id=row["id"],
            content_item_id=row["content_item_id"],
            artifact_ids=tuple(a["artifact_id"] for a in artifacts),
            checks=tuple(
                QCCheck(c["name"], QCStatus(c["status"]), c["detail"]) for c in checks
            ),
            created_at=parse_dt(row["created_at"]),
        )


class ApprovalRequestRepository(Repository):
    """Only the status of a request changes; its bindings never do."""

    table = "approval_requests"

    def add(self, request: ApprovalRequest) -> None:
        self._insert(
            "approval_requests",
            {
                "id": request.id,
                "content_item_id": request.content_item_id,
                "status": request.status.value,
                **actor_columns("requested_by", request.requested_by),
                "qc_result_id": request.qc_result_id,
                "created_at": dt(request.created_at),
            },
        )
        for position, binding in enumerate(request.artifacts):
            self._insert(
                "approval_artifacts",
                {
                    "approval_request_id": request.id,
                    "position": position,
                    "artifact_id": binding.artifact_id,
                    "kind": binding.kind.value,
                    "version": binding.version,
                    "sha256": binding.sha256,
                },
            )

    def get(self, request_id: str) -> ApprovalRequest | None:
        row = self._one("SELECT * FROM approval_requests WHERE id = ?", (request_id,))
        return self._request(row) if row else None

    def list_by_content_item(self, content_item_id: str) -> list[ApprovalRequest]:
        rows = self._all(
            "SELECT * FROM approval_requests WHERE content_item_id = ? "
            "ORDER BY created_at, id",
            (content_item_id,),
        )
        return [self._request(row) for row in rows]

    def update(
        self, request: ApprovalRequest, *, expected_status: ApprovalStatus
    ) -> None:
        self._update(
            request.id,
            {"status": request.status.value},
            guard_column="status",
            guard_value=expected_status.value,
        )

    def _request(self, row: sqlite3.Row) -> ApprovalRequest:
        bindings = self._all(
            "SELECT * FROM approval_artifacts WHERE approval_request_id = ? "
            "ORDER BY position",
            (row["id"],),
        )
        return ApprovalRequest(
            id=row["id"],
            content_item_id=row["content_item_id"],
            artifacts=tuple(
                ArtifactBinding(
                    b["artifact_id"], ArtifactKind(b["kind"]), b["version"], b["sha256"]
                )
                for b in bindings
            ),
            status=ApprovalStatus(row["status"]),
            requested_by=actor_from(row, "requested_by"),
            qc_result_id=row["qc_result_id"],
            created_at=parse_dt(row["created_at"]),
        )


def _rights_row(record: RightsRecord) -> dict:
    return {
        "id": record.id,
        "content_item_id": record.content_item_id,
        "asset_ref": record.asset_ref,
        "source": record.source,
        "license": record.license,
        "risk_level": record.risk_level.value,
        "resolution": record.resolution.value,
        **actor_columns("resolved_by", record.resolved_by),
        "resolved_at": dt(record.resolved_at),
        "created_at": dt(record.created_at),
        "updated_at": dt(record.updated_at),
    }


def _rights(row: sqlite3.Row) -> RightsRecord:
    return RightsRecord(
        id=row["id"],
        content_item_id=row["content_item_id"],
        asset_ref=row["asset_ref"],
        source=row["source"],
        license=row["license"],
        risk_level=RiskLevel(row["risk_level"]),
        resolution=RiskResolution(row["resolution"]),
        resolved_by=actor_from(row, "resolved_by"),
        resolved_at=parse_dt(row["resolved_at"]),
        created_at=parse_dt(row["created_at"]),
        updated_at=parse_dt(row["updated_at"]),
    )
