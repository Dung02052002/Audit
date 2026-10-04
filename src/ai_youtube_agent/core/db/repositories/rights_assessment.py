"""Repository of the rights assessment history of rights records (G-078)."""

import sqlite3

from ai_youtube_agent.content.rights import RiskLevel
from ai_youtube_agent.content.rights_assessment import RightsAssessment
from ai_youtube_agent.core.db.repositories.base import (
    Repository,
    actor_columns,
    actor_from,
    dt,
    from_json,
    parse_dt,
    to_json,
)


class RightsAssessmentRepository(Repository):
    """The history is append only: add and read, no update and no delete."""

    table = "rights_assessments"

    def add(self, assessment: RightsAssessment) -> None:
        self._insert(
            self.table,
            {
                "id": assessment.id,
                "rights_record_id": assessment.rights_record_id,
                "content_item_id": assessment.content_item_id,
                "asset_id": assessment.asset_id,
                "level": assessment.level.value,
                "rule_codes_json": to_json(list(assessment.rule_codes)),
                "rules_version": assessment.rules_version,
                "provenance_id": assessment.provenance_id,
                **actor_columns("assessed_by", assessment.assessed_by),
                "created_at": dt(assessment.created_at),
            },
        )

    def latest(self, rights_record_id: str) -> RightsAssessment | None:
        row = self._one(
            "SELECT * FROM rights_assessments WHERE rights_record_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (rights_record_id,),
        )
        return _assessment(row) if row else None

    def list_by_rights_record(self, rights_record_id: str) -> list[RightsAssessment]:
        rows = self._all(
            "SELECT * FROM rights_assessments WHERE rights_record_id = ? "
            "ORDER BY created_at, rowid",
            (rights_record_id,),
        )
        return [_assessment(row) for row in rows]

    def list_by_content_item(self, content_item_id: str) -> list[RightsAssessment]:
        rows = self._all(
            "SELECT * FROM rights_assessments WHERE content_item_id = ? "
            "ORDER BY created_at, rowid",
            (content_item_id,),
        )
        return [_assessment(row) for row in rows]


def _assessment(row: sqlite3.Row) -> RightsAssessment:
    return RightsAssessment(
        id=row["id"],
        rights_record_id=row["rights_record_id"],
        content_item_id=row["content_item_id"],
        asset_id=row["asset_id"],
        level=RiskLevel(row["level"]),
        rule_codes=tuple(from_json(row["rule_codes_json"])),
        rules_version=row["rules_version"],
        provenance_id=row["provenance_id"],
        assessed_by=actor_from(row, "assessed_by"),
        created_at=parse_dt(row["created_at"]),
    )
