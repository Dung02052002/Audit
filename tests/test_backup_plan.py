"""Tests for S-220 Backup Plan."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.factories import make_strategy_profile

from ai_youtube_agent.content.channel import (
    Channel,
    ChannelStatus,
    YouTubeIdentifiers,
)
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import LocalFileStorage
from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.backup_plan import BackupError, BackupManager
from ai_youtube_agent.core.content_item import ContentItem, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)


def _FixedClock(dt: datetime):
    return lambda: dt


def test_backup_manager_creates_consistent_db_and_artifact_bundle(
    database: Database,
    tmp_path: Path,
) -> None:
    t0 = datetime(2026, 10, 21, 18, 0, tzinfo=UTC)
    artifact_dir = tmp_path / "artifacts"
    backup_root = tmp_path / "backups"
    storage = LocalFileStorage(artifact_dir)

    ch = Channel.create(
        "Backup Channel",
        YouTubeIdentifiers("UC1234567890123456789012", "@backupch"),
        clock=_FixedClock(t0),
    ).with_status(ChannelStatus.ACTIVE, clock=_FixedClock(t0))
    profile = make_strategy_profile(ch)
    item = ContentItem.create(
        ch.id,
        profile.id,
        profile.version,
        ContentType.SHORTS,
        "Backed Up Short",
        clock=_FixedClock(t0),
    )

    stored_file = storage.write(
        item.id,
        ArtifactKind.SUBTITLES,
        1,
        b"1\n00:00:00,000 --> 00:00:02,000\nBackup subtitle line\n",
        media_type="application/x-subrip",
    )
    artifact = Artifact.create(
        item.id,
        ArtifactKind.SUBTITLES,
        uri=stored_file.uri,
        sha256=stored_file.sha256,
        size_bytes=stored_file.size_bytes,
        media_type="application/x-subrip",
        clock=_FixedClock(t0),
    )

    with database.transaction() as conn:
        ChannelRepository(conn).add(ch)
        StrategyProfileRepository(conn).add(profile)
        ContentItemRepository(conn).add(item)
        ArtifactRepository(conn).add(artifact)

    sink = InMemoryAuditSink()
    audit = AuditLog(sink, clock=_FixedClock(t0))
    manager = BackupManager(
        database.path,
        artifact_dir,
        backup_root,
        audit=audit,
        clock=_FixedClock(t0),
    )

    bundle_dir, manifest = manager.create_backup(label="nightly")
    assert bundle_dir.is_dir()
    assert manifest.backup_id == "bkp_20261021T180000Z_nightly"
    assert manifest.database_entry.size_bytes > 0
    assert len(manifest.artifact_entries) == 1
    assert manifest.artifact_entries[0].sha256 == artifact.sha256
    assert manifest.table_row_counts["channels"] == 1
    assert manifest.table_row_counts["content_items"] == 1
    assert manifest.table_row_counts["artifacts"] == 1

    reloaded = BackupManager.load_manifest(bundle_dir)
    assert reloaded == manifest
    assert len(sink.events()) == 1
    assert sink.events()[0].action == "backup.created"


def test_backup_missing_database_raises(tmp_path: Path) -> None:
    manager = BackupManager(
        tmp_path / "nonexistent.db",
        tmp_path / "artifacts",
        tmp_path / "backups",
    )
    with pytest.raises(BackupError) as exc_info:
        manager.create_backup()
    assert exc_info.value.code == "domain.backup_database_missing"
