"""Tests for S-221 Restore Test using non-production data."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ai_youtube_agent.content.approval import ApprovalRequest
from ai_youtube_agent.content.channel import (
    Channel,
    ChannelStatus,
    YouTubeIdentifiers,
)
from ai_youtube_agent.content.strategy import (
    Audience,
    Brand,
    Budget,
    Cadence,
    LanguageSettings,
    Market,
    Niche,
    Pillar,
    StrategyProfile,
)
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import LocalFileStorage
from ai_youtube_agent.core.audit import Actor, ActorKind, AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.backup_plan import BackupManager
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.core.restore_verifier import (
    RestoreVerificationError,
    RestoreVerifier,
)


def _FixedClock(dt: datetime):
    return lambda: dt


def test_full_non_production_backup_and_restore_verification(
    database: Database,
    tmp_path: Path,
) -> None:
    t0 = datetime(2026, 10, 21, 19, 0, tzinfo=UTC)
    user = Actor(ActorKind.USER, "dung")
    src_artifacts = tmp_path / "src_artifacts"
    backup_root = tmp_path / "backups"
    storage = LocalFileStorage(src_artifacts)

    ch = Channel.create(
        "Restore Drill Channel",
        YouTubeIdentifiers("UC1234567890123456789012", "@restoredrill"),
        clock=_FixedClock(t0),
    ).with_status(ChannelStatus.ACTIVE, clock=_FixedClock(t0))
    sp = StrategyProfile.create(
        ch.id,
        user,
        market=Market("US"),
        languages=LanguageSettings("en"),
        audience=Audience("Builders"),
        niche=Niche("Engineering", (Pillar("Systems"),)),
        brand=Brand("Restore Drill", "calm"),
        cadence=Cadence(shorts_per_day=2, longform_per_day=1, time_zone="UTC"),
        budget=Budget("USD", Decimal("25.00"), Decimal("250.00")),
        clock=_FixedClock(t0),
    )
    item = ContentItem.create(
        ch.id,
        sp.id,
        sp.version,
        ContentType.SHORTS,
        "Restore Test Short",
        clock=_FixedClock(t0),
    ).with_status(ContentStatus.GENERATING, clock=_FixedClock(t0))

    stored_video = storage.write(
        item.id,
        ArtifactKind.VIDEO,
        1,
        b"\x00\x00\x00\x18ftypmp42restore-test-video-bytes",
        media_type="video/mp4",
    )
    video_art = Artifact.create(
        item.id,
        ArtifactKind.VIDEO,
        uri=stored_video.uri,
        sha256=stored_video.sha256,
        size_bytes=stored_video.size_bytes,
        media_type="video/mp4",
        clock=_FixedClock(t0),
    )
    approval = ApprovalRequest.create(
        item.id,
        [video_art],
        requested_by=user,
        clock=_FixedClock(t0),
    ).approve(user, clock=_FixedClock(t0))

    with database.transaction() as conn:
        ChannelRepository(conn).add(ch)
        StrategyProfileRepository(conn).add(sp)
        ContentItemRepository(conn).add(item)
        ArtifactRepository(conn).add(video_art)
        ApprovalRequestRepository(conn).add(approval)

    sink = InMemoryAuditSink()
    audit = AuditLog(sink, clock=_FixedClock(t0))
    bkp_mgr = BackupManager(
        database.path,
        src_artifacts,
        backup_root,
        audit=audit,
        clock=_FixedClock(t0),
    )
    bundle_dir, _ = bkp_mgr.create_backup(label="restore_drill")

    # Restore into a fresh non-production directory
    restored_db_path = tmp_path / "restored" / "restored.db"
    restored_artifacts_dir = tmp_path / "restored" / "artifacts"
    verifier = RestoreVerifier(audit=audit, clock=_FixedClock(t0))

    report = verifier.restore_and_verify(
        bundle_dir,
        target_database_path=restored_db_path,
        target_artifact_dir=restored_artifacts_dir,
        actor=user,
    )
    assert report.passed is True
    assert report.sqlite_integrity_ok is True
    assert report.foreign_keys_ok is True
    assert report.row_counts_matched is True
    assert report.verified_artifact_files == 1
    assert report.verified_db_artifact_rows == 1

    # Confirm restored database entities and artifact bytes are intact
    restored_db = Database(restored_db_path)
    with restored_db.connection() as conn:
        restored_ch = ChannelRepository(conn).get(ch.id)
        restored_item = ContentItemRepository(conn).get(item.id)
        restored_req = ApprovalRequestRepository(conn).get(approval.id)
    assert restored_ch == ch
    assert restored_item == item
    assert restored_req == approval


def test_restore_verifier_rejects_corrupted_artifact_in_bundle(
    database: Database,
    tmp_path: Path,
) -> None:
    t0 = datetime(2026, 10, 21, 19, 30, tzinfo=UTC)
    src_artifacts = tmp_path / "src_artifacts"
    backup_root = tmp_path / "backups"
    storage = LocalFileStorage(src_artifacts)

    stored = storage.write(
        "item-corrupt-1",
        ArtifactKind.METADATA,
        1,
        b'{"title": "clean"}',
        media_type="application/json",
    )
    bkp_mgr = BackupManager(
        database.path,
        src_artifacts,
        backup_root,
        clock=_FixedClock(t0),
    )
    bundle_dir, manifest = bkp_mgr.create_backup(label="corrupt_drill")

    # Corrupt the backed-up artifact file
    backed_up_art = bundle_dir / manifest.artifact_entries[0].relative_path
    backed_up_art.write_bytes(b'{"title": "tampered"}')

    verifier = RestoreVerifier(clock=_FixedClock(t0))
    with pytest.raises(RestoreVerificationError) as exc_info:
        verifier.restore_and_verify(
            bundle_dir,
            target_database_path=tmp_path / "out" / "db.sqlite3",
            target_artifact_dir=tmp_path / "out" / "artifacts",
        )
    assert exc_info.value.code == "domain.restore_checksum_mismatch"
    assert stored.size_bytes > 0
