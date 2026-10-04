"""Repository of the provenance history of assets (G-077)."""

import sqlite3

from ai_youtube_agent.content.provenance import Provenance
from ai_youtube_agent.core.db.repositories.base import (
    Repository,
    actor_columns,
    actor_from,
    dt,
    parse_dt,
)


class ProvenanceRepository(Repository):
    """The history is append only: add and read, no update and no delete."""

    table = "asset_provenance"

    def add(self, provenance: Provenance) -> None:
        self._insert(
            self.table,
            {
                "id": provenance.id,
                "asset_id": provenance.asset_id,
                "source_url": provenance.source_url,
                "retrieved_at": dt(provenance.retrieved_at),
                "license_name": provenance.license_name,
                "license_url": provenance.license_url,
                "license_ref": provenance.license_ref,
                "attribution": provenance.attribution,
                "owner": provenance.owner,
                "file_sha256": provenance.file_sha256,
                "proof": provenance.proof,
                **actor_columns("recorded_by", provenance.recorded_by),
                "created_at": dt(provenance.created_at),
            },
        )

    def latest(self, asset_id: str) -> Provenance | None:
        row = self._one(
            "SELECT * FROM asset_provenance WHERE asset_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (asset_id,),
        )
        return _provenance(row) if row else None

    def list_by_asset(self, asset_id: str) -> list[Provenance]:
        rows = self._all(
            "SELECT * FROM asset_provenance WHERE asset_id = ? "
            "ORDER BY created_at, rowid",
            (asset_id,),
        )
        return [_provenance(row) for row in rows]


def _provenance(row: sqlite3.Row) -> Provenance:
    return Provenance(
        id=row["id"],
        asset_id=row["asset_id"],
        source_url=row["source_url"],
        retrieved_at=parse_dt(row["retrieved_at"]),
        license_name=row["license_name"],
        license_url=row["license_url"],
        license_ref=row["license_ref"],
        attribution=row["attribution"],
        owner=row["owner"],
        file_sha256=row["file_sha256"],
        proof=row["proof"],
        recorded_by=actor_from(row, "recorded_by"),
        created_at=parse_dt(row["created_at"]),
    )
