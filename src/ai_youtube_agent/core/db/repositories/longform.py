"""Repository of the LongForm projects (J-105): one mutable row per content item."""

import sqlite3
from typing import Any

from ai_youtube_agent.content.longform_config import LongFormConfig
from ai_youtube_agent.content.longform_project import (
    LongFormProject,
    LongFormProjectStatus,
)
from ai_youtube_agent.content.longform_timeline import LongFormTimeline
from ai_youtube_agent.core.db.repositories.base import (
    Repository,
    actor_columns,
    actor_from,
    dt,
    from_json,
    parse_dt,
    to_json,
)


class LongFormProjectRepository(Repository):
    """A project changes through ``update``, guarded by its ``version``."""

    table = "longform_projects"

    def add(self, project: LongFormProject) -> None:
        self._insert(self.table, _row(project))

    def get(self, project_id: str) -> LongFormProject | None:
        row = self._one("SELECT * FROM longform_projects WHERE id = ?", (project_id,))
        return _project(row) if row else None

    def get_by_content_item(self, content_item_id: str) -> LongFormProject | None:
        row = self._one(
            "SELECT * FROM longform_projects WHERE content_item_id = ?",
            (content_item_id,),
        )
        return _project(row) if row else None

    def list_by_channel(self, channel_id: str) -> list[LongFormProject]:
        rows = self._all(
            "SELECT longform_projects.* FROM longform_projects "
            "JOIN content_items ON content_items.id = "
            "longform_projects.content_item_id "
            "WHERE content_items.channel_id = ? "
            "ORDER BY longform_projects.created_at, longform_projects.rowid",
            (channel_id,),
        )
        return [_project(row) for row in rows]

    def update(self, project: LongFormProject, *, expected_version: int) -> None:
        values = _row(project)
        for column in (
            "id",
            "content_item_id",
            "created_by_kind",
            "created_by_id",
        ):
            del values[column]
        del values["created_at"]
        self._update(
            project.id,
            values,
            guard_column="version",
            guard_value=expected_version,
        )


def _row(project: LongFormProject) -> dict[str, Any]:
    return {
        "id": project.id,
        "content_item_id": project.content_item_id,
        "script_id": project.script_id,
        "script_version": project.script_version,
        "audio_artifact_id": project.audio_artifact_id,
        "audio_version": project.audio_version,
        "audio_sha256": project.audio_sha256,
        "audio_duration_ms": project.audio_duration_ms,
        "strategy_version": project.strategy_version,
        "format_json": to_json(project.config.to_dict()),
        "timeline_json": project.timeline.to_json(),
        "status": project.status.value,
        "version": project.version,
        **actor_columns("created_by", project.created_by),
        "created_at": dt(project.created_at),
        **actor_columns("updated_by", project.updated_by),
        "updated_at": dt(project.updated_at),
    }


def _project(row: sqlite3.Row) -> LongFormProject:
    return LongFormProject(
        id=row["id"],
        content_item_id=row["content_item_id"],
        script_id=row["script_id"],
        script_version=row["script_version"],
        audio_artifact_id=row["audio_artifact_id"],
        audio_version=row["audio_version"],
        audio_sha256=row["audio_sha256"],
        audio_duration_ms=row["audio_duration_ms"],
        strategy_version=row["strategy_version"],
        config=LongFormConfig.from_dict(from_json(row["format_json"])),
        timeline=LongFormTimeline.from_json(row["timeline_json"]),
        status=LongFormProjectStatus(row["status"]),
        version=row["version"],
        created_by=actor_from(row, "created_by"),
        created_at=parse_dt(row["created_at"]),
        updated_by=actor_from(row, "updated_by"),
        updated_at=parse_dt(row["updated_at"]),
    )
