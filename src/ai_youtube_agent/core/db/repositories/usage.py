"""Repositories for daily usage (C3): production starts and publish counts.

``ProductionStartRepository`` stores the append-only production start log
(migration 0002): add only. ``DailyUsageRepository`` counts what the daily
limit gate (#036) needs, in a half-open UTC window ``[start, end)``.
"""

import sqlite3
from datetime import datetime

from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.core.db.repositories.base import Repository, dt, parse_dt
from ai_youtube_agent.core.production_start import ProductionStart
from ai_youtube_agent.pipeline.publish import PublishStatus


class ProductionStartRepository(Repository):
    table = "production_starts"

    def add(self, start: ProductionStart) -> None:
        self._insert(
            "production_starts",
            {
                "id": start.id,
                "content_item_id": start.content_item_id,
                "channel_id": start.channel_id,
                "content_type": start.content_type.value,
                "started_at": dt(start.started_at),
            },
        )

    def list_by_content_item(self, content_item_id: str) -> list[ProductionStart]:
        rows = self._all(
            "SELECT * FROM production_starts WHERE content_item_id = ? "
            "ORDER BY started_at, id",
            (content_item_id,),
        )
        return [_start(row) for row in rows]


class DailyUsageRepository(Repository):
    def count_production_starts(
        self,
        channel_id: str,
        content_type: ContentType,
        start: datetime,
        end: datetime,
    ) -> int:
        row = self._one(
            "SELECT count(*) AS n FROM production_starts "
            "WHERE channel_id = ? AND content_type = ? "
            "AND started_at >= ? AND started_at < ?",
            (channel_id, content_type.value, dt(start), dt(end)),
        )
        return row["n"]

    def count_publishes(
        self,
        channel_id: str,
        content_type: ContentType,
        start: datetime,
        end: datetime,
        *,
        exclude: str | None = None,
    ) -> int:
        """Publish jobs created in the window that have not failed.

        ``exclude`` leaves out the jobs of one content item, so an item is
        never counted against its own publish.
        """
        row = self._one(
            "SELECT count(*) AS n FROM publish_jobs AS job "
            "JOIN content_items AS item ON item.id = job.content_item_id "
            "WHERE item.channel_id = ? AND job.content_type = ? "
            "AND job.status <> ? AND job.created_at >= ? AND job.created_at < ? "
            "AND job.content_item_id IS NOT ?",
            (
                channel_id,
                content_type.value,
                PublishStatus.FAILED.value,
                dt(start),
                dt(end),
                exclude,
            ),
        )
        return row["n"]


def _start(row: sqlite3.Row) -> ProductionStart:
    return ProductionStart(
        id=row["id"],
        content_item_id=row["content_item_id"],
        channel_id=row["channel_id"],
        content_type=ContentType(row["content_type"]),
        started_at=parse_dt(row["started_at"]),
    )
