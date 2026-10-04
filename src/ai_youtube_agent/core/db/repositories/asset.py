"""Repositories of the asset registry (G-076): assets and their usages."""

import sqlite3

from ai_youtube_agent.content.asset import Asset, AssetCategory, AssetKind
from ai_youtube_agent.content.asset_usage import AssetUsage
from ai_youtube_agent.core.db.repositories.base import (
    Repository,
    actor_columns,
    actor_from,
    dt,
    parse_dt,
)


class AssetRepository(Repository):
    """An asset is complete when registered: add only."""

    table = "assets"

    def add(self, asset: Asset, *, source_key: str, title_key: str) -> None:
        self._insert(
            self.table,
            {
                "id": asset.id,
                "channel_id": asset.channel_id,
                "kind": asset.kind.value,
                "category": asset.category.value,
                "title": asset.title,
                "source": asset.source,
                "source_key": source_key,
                "title_key": title_key,
                "artifact_id": asset.artifact_id,
                "license_ref": asset.license_ref,
                "attribution": asset.attribution,
                "owner": asset.owner,
                "created_at": dt(asset.created_at),
            },
        )

    def get(self, asset_id: str) -> Asset | None:
        row = self._one("SELECT * FROM assets WHERE id = ?", (asset_id,))
        return _asset(row) if row else None

    def find_by_key(
        self, channel_id: str, source_key: str, title_key: str
    ) -> Asset | None:
        row = self._one(
            "SELECT * FROM assets "
            "WHERE channel_id = ? AND source_key = ? AND title_key = ?",
            (channel_id, source_key, title_key),
        )
        return _asset(row) if row else None

    def get_by_artifact(self, artifact_id: str) -> Asset | None:
        row = self._one("SELECT * FROM assets WHERE artifact_id = ?", (artifact_id,))
        return _asset(row) if row else None

    def list_by_channel(self, channel_id: str) -> list[Asset]:
        rows = self._all(
            "SELECT * FROM assets WHERE channel_id = ? ORDER BY created_at, rowid",
            (channel_id,),
        )
        return [_asset(row) for row in rows]

    def list_by_content_item(self, content_item_id: str) -> list[Asset]:
        rows = self._all(
            "SELECT assets.* FROM assets "
            "JOIN asset_usages ON asset_usages.asset_id = assets.id "
            "WHERE asset_usages.content_item_id = ? "
            "ORDER BY asset_usages.created_at, asset_usages.rowid",
            (content_item_id,),
        )
        return [_asset(row) for row in rows]


class AssetUsageRepository(Repository):
    """A usage is complete when attached: add only."""

    table = "asset_usages"

    def add(self, usage: AssetUsage) -> None:
        self._insert(
            self.table,
            {
                "id": usage.id,
                "asset_id": usage.asset_id,
                "content_item_id": usage.content_item_id,
                "purpose": usage.purpose,
                **actor_columns("attached_by", usage.attached_by),
                "created_at": dt(usage.created_at),
            },
        )

    def get_by_pair(self, asset_id: str, content_item_id: str) -> AssetUsage | None:
        row = self._one(
            "SELECT * FROM asset_usages WHERE asset_id = ? AND content_item_id = ?",
            (asset_id, content_item_id),
        )
        return _usage(row) if row else None

    def list_by_asset(self, asset_id: str) -> list[AssetUsage]:
        rows = self._all(
            "SELECT * FROM asset_usages WHERE asset_id = ? ORDER BY created_at, rowid",
            (asset_id,),
        )
        return [_usage(row) for row in rows]

    def list_by_content_item(self, content_item_id: str) -> list[AssetUsage]:
        rows = self._all(
            "SELECT * FROM asset_usages WHERE content_item_id = ? "
            "ORDER BY created_at, rowid",
            (content_item_id,),
        )
        return [_usage(row) for row in rows]


def _asset(row: sqlite3.Row) -> Asset:
    return Asset(
        id=row["id"],
        channel_id=row["channel_id"],
        kind=AssetKind(row["kind"]),
        category=AssetCategory(row["category"]),
        title=row["title"],
        source=row["source"],
        artifact_id=row["artifact_id"],
        license_ref=row["license_ref"],
        attribution=row["attribution"],
        owner=row["owner"],
        created_at=parse_dt(row["created_at"]),
    )


def _usage(row: sqlite3.Row) -> AssetUsage:
    return AssetUsage(
        id=row["id"],
        asset_id=row["asset_id"],
        content_item_id=row["content_item_id"],
        purpose=row["purpose"],
        attached_by=actor_from(row, "attached_by"),
        created_at=parse_dt(row["created_at"]),
    )
