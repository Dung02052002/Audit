"""Repositories for content items, artifacts, scripts and audio metadata."""

import sqlite3
from datetime import datetime

from ai_youtube_agent.content.script import (
    Claim,
    DurationTarget,
    Evidence,
    Script,
    ScriptSection,
)
from ai_youtube_agent.content.voice import AudioMetadata
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus, ContentType
from ai_youtube_agent.core.db.repositories.base import (
    Repository,
    actor_columns,
    actor_from,
    dt,
    from_json,
    parse_dt,
    to_json,
)


class ContentItemRepository(Repository):
    table = "content_items"

    def add(self, item: ContentItem) -> None:
        self._insert(self.table, _item_row(item))

    def get(self, item_id: str) -> ContentItem | None:
        row = self._one("SELECT * FROM content_items WHERE id = ?", (item_id,))
        return _item(row) if row else None

    def list_by_channel(self, channel_id: str) -> list[ContentItem]:
        rows = self._all(
            "SELECT * FROM content_items WHERE channel_id = ? ORDER BY created_at, id",
            (channel_id,),
        )
        return [_item(row) for row in rows]

    def update(self, item: ContentItem, *, expected_updated_at: datetime) -> None:
        self._update(
            item.id,
            {
                "title": item.title,
                "status": item.status.value,
                "updated_at": dt(item.updated_at),
            },
            guard_column="updated_at",
            guard_value=dt(expected_updated_at),
        )


class ArtifactRepository(Repository):
    """Artifact versions are immutable: add only."""

    table = "artifacts"

    def add(self, artifact: Artifact) -> None:
        self._insert(
            self.table,
            {
                "id": artifact.id,
                "content_item_id": artifact.content_item_id,
                "kind": artifact.kind.value,
                "version": artifact.version,
                "uri": artifact.uri,
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
                "media_type": artifact.media_type,
                "created_at": dt(artifact.created_at),
            },
        )

    def get(self, artifact_id: str) -> Artifact | None:
        row = self._one("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
        return _artifact(row) if row else None

    def list_by_content_item(self, content_item_id: str) -> list[Artifact]:
        rows = self._all(
            "SELECT * FROM artifacts WHERE content_item_id = ? ORDER BY kind, version",
            (content_item_id,),
        )
        return [_artifact(row) for row in rows]


class ScriptRepository(Repository):
    """Script versions, claims and evidence are immutable: add only."""

    table = "scripts"

    def add(self, script: Script) -> None:
        target = script.duration_target
        self._insert(
            "scripts",
            {
                "id": script.id,
                "content_item_id": script.content_item_id,
                "version": script.version,
                "text": script.text,
                "created_at": dt(script.created_at),
                "sections_json": to_json([s.as_dict() for s in script.sections]),
                "duration_min_seconds": target.min_seconds if target else None,
                "duration_max_seconds": target.max_seconds if target else None,
                **(
                    actor_columns("created_by", script.created_by)
                    if script.created_by
                    else {"created_by_kind": None, "created_by_id": None}
                ),
                "reason": script.reason,
                "parent_id": script.parent_id,
                "strategy_version": script.strategy_version,
                "research_report_id": script.research_report_id,
            },
        )

    def get(self, script_id: str) -> Script | None:
        row = self._one("SELECT * FROM scripts WHERE id = ?", (script_id,))
        return _script(row) if row else None

    def list_by_content_item(self, content_item_id: str) -> list[Script]:
        rows = self._all(
            "SELECT * FROM scripts WHERE content_item_id = ? ORDER BY version",
            (content_item_id,),
        )
        return [_script(row) for row in rows]

    def add_claim(self, claim: Claim) -> None:
        self._insert(
            "claims",
            {
                "id": claim.id,
                "script_id": claim.script_id,
                "text": claim.text,
                "created_at": dt(claim.created_at),
                "section_index": claim.section_index,
            },
        )

    def list_claims(self, script_id: str) -> list[Claim]:
        rows = self._all(
            "SELECT * FROM claims WHERE script_id = ? ORDER BY created_at, id",
            (script_id,),
        )
        return [
            Claim(
                row["id"],
                row["script_id"],
                row["text"],
                parse_dt(row["created_at"]),
                row["section_index"],
            )
            for row in rows
        ]

    def add_evidence(self, evidence: Evidence) -> None:
        self._insert(
            "evidence",
            {
                "id": evidence.id,
                "claim_id": evidence.claim_id,
                "source_ref": evidence.source_ref,
                "excerpt": evidence.excerpt,
                "created_at": dt(evidence.created_at),
            },
        )

    def list_evidence(self, claim_id: str) -> list[Evidence]:
        rows = self._all(
            "SELECT * FROM evidence WHERE claim_id = ? ORDER BY created_at, id",
            (claim_id,),
        )
        return [
            Evidence(
                row["id"],
                row["claim_id"],
                row["source_ref"],
                row["excerpt"],
                parse_dt(row["created_at"]),
            )
            for row in rows
        ]


class AudioMetadataRepository(Repository):
    """Audio metadata is immutable: add only."""

    table = "audio_metadata"

    def add(self, audio: AudioMetadata) -> None:
        self._insert(
            self.table,
            {
                "id": audio.id,
                "artifact_id": audio.artifact_id,
                "script_id": audio.script_id,
                "voice_profile_id": audio.voice_profile_id,
                "voice_profile_version": audio.voice_profile_version,
                "provider": audio.provider,
                "duration_ms": audio.duration_ms,
                "created_at": dt(audio.created_at),
            },
        )

    def get(self, audio_id: str) -> AudioMetadata | None:
        row = self._one("SELECT * FROM audio_metadata WHERE id = ?", (audio_id,))
        return _audio(row) if row else None

    def get_by_artifact(self, artifact_id: str) -> AudioMetadata | None:
        row = self._one(
            "SELECT * FROM audio_metadata WHERE artifact_id = ?", (artifact_id,)
        )
        return _audio(row) if row else None


def _item_row(item: ContentItem) -> dict:
    return {
        "id": item.id,
        "channel_id": item.channel_id,
        "strategy_profile_id": item.strategy_profile_id,
        "strategy_version": item.strategy_version,
        "content_type": item.content_type.value,
        "title": item.title,
        "status": item.status.value,
        "created_at": dt(item.created_at),
        "updated_at": dt(item.updated_at),
    }


def _item(row: sqlite3.Row) -> ContentItem:
    return ContentItem(
        id=row["id"],
        channel_id=row["channel_id"],
        strategy_profile_id=row["strategy_profile_id"],
        strategy_version=row["strategy_version"],
        content_type=ContentType(row["content_type"]),
        title=row["title"],
        status=ContentStatus(row["status"]),
        created_at=parse_dt(row["created_at"]),
        updated_at=parse_dt(row["updated_at"]),
    )


def _artifact(row: sqlite3.Row) -> Artifact:
    return Artifact(
        id=row["id"],
        content_item_id=row["content_item_id"],
        kind=ArtifactKind(row["kind"]),
        version=row["version"],
        uri=row["uri"],
        sha256=row["sha256"],
        size_bytes=row["size_bytes"],
        media_type=row["media_type"],
        created_at=parse_dt(row["created_at"]),
    )


def _script(row: sqlite3.Row) -> Script:
    return Script(
        id=row["id"],
        content_item_id=row["content_item_id"],
        version=row["version"],
        sections=tuple(
            ScriptSection.from_dict(item) for item in from_json(row["sections_json"])
        ),
        created_at=parse_dt(row["created_at"]),
        duration_target=(
            DurationTarget(row["duration_min_seconds"], row["duration_max_seconds"])
            if row["duration_min_seconds"] is not None
            else None
        ),
        created_by=(
            actor_from(row, "created_by")
            if row["created_by_kind"] is not None
            else None
        ),
        reason=row["reason"],
        parent_id=row["parent_id"],
        strategy_version=row["strategy_version"],
        research_report_id=row["research_report_id"],
    )


def _audio(row: sqlite3.Row) -> AudioMetadata:
    return AudioMetadata(
        id=row["id"],
        artifact_id=row["artifact_id"],
        script_id=row["script_id"],
        voice_profile_id=row["voice_profile_id"],
        voice_profile_version=row["voice_profile_version"],
        provider=row["provider"],
        duration_ms=row["duration_ms"],
        created_at=parse_dt(row["created_at"]),
    )
