from pathlib import Path

import pytest
from tests.test_longform_project import USER, Clock, _seed_longform_item

from ai_youtube_agent.content.asset import AssetCategory, AssetKind
from ai_youtube_agent.content.asset_registry import AssetRegistry
from ai_youtube_agent.content.broll_manager import (
    BROLL_ASSIGNED,
    BrollAssignment,
    BrollError,
    BrollErrorCode,
    BrollManager,
)
from ai_youtube_agent.content.longform_project import LongFormProjectStatus
from ai_youtube_agent.content.longform_script_generator import LongFormDisabledError
from ai_youtube_agent.content.longform_service import (
    LongFormProjectError,
    LongFormProjectErrorCode,
    LongFormProjectService,
)
from ai_youtube_agent.content.provenance_recorder import ProvenanceRecorder
from ai_youtube_agent.content.rights import RightsRecord, RiskLevel
from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.migrate import migrate
from ai_youtube_agent.core.db.repositories.review import RightsRecordRepository
from ai_youtube_agent.core.flags import FeatureFlags


def test_broll_manager_auto_assigns_backgrounds_and_promotes_to_ready(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "agent.db"
    migrate(db_path)
    database = Database(db_path)
    clock = Clock()
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    channel, item, _, _ = _seed_longform_item(database, clock)
    flags = FeatureFlags(longform_enabled=True)
    service = LongFormProjectService(database, audit, flags, clock=clock)
    broll = BrollManager(database, audit, flags, clock=clock)

    project = service.create(item.id, actor=USER)
    planned = service.plan(project.id, expected_version=1, actor=USER)
    chaptered = service.generate_chapters(
        project.id, expected_version=planned.version, actor=USER
    )
    captioned = service.caption(
        project.id, expected_version=chaptered.version, actor=USER
    )

    registry = AssetRegistry(database, audit, clock=clock)
    provenance_recorder = ProvenanceRecorder(database, audit, clock=clock)

    clip_a = registry.register(
        channel.id,
        kind=AssetKind.VIDEO_CLIP,
        category=AssetCategory.LICENSED,
        title="Tokamak Clip A",
        source="https://assets.example/tokamak-a.mp4",
        license_ref="CC-BY-4.0",
        attribution="Lab A",
        actor=USER,
    )
    clip_b = registry.register(
        channel.id,
        kind=AssetKind.IMAGE,
        category=AssetCategory.USER_OWNED,
        owner="producer",
        title="Diagram B",
        source="https://assets.example/diagram-b.png",
        actor=USER,
    )
    registry.attach(clip_a.id, item.id, purpose="b-roll", actor=USER)
    registry.attach(clip_b.id, item.id, purpose="b-roll", actor=USER)
    # Attaching creates an UNKNOWN (blocking) rights record; assess both as low.
    with database.transaction() as conn:
        rights_repo = RightsRecordRepository(conn)
        for record in rights_repo.list_by_content_item(item.id):
            rights_repo.update(
                record.with_risk_level(RiskLevel.LOW, clock=clock),
                expected_updated_at=record.updated_at,
            )
    prov_a = provenance_recorder.record(
        clip_a.id,
        source_url="https://assets.example/tokamak-a.mp4",
        license_name="CC-BY-4.0",
        attribution="Lab A",
        actor=USER,
    )

    ready = broll.auto_assign_backgrounds(
        project.id, expected_version=captioned.version, actor=USER
    )

    assert ready.status is LongFormProjectStatus.READY
    assigned_ids = [scene.assets[0].asset_id for scene in ready.timeline.scenes]
    assert assigned_ids == [
        clip_a.id,
        clip_b.id,
        clip_a.id,
        clip_b.id,
        clip_a.id,
        clip_b.id,
        clip_a.id,
    ]
    assert ready.timeline.scenes[0].assets[0].provenance_id == prov_a.id

    events = [e for e in sink.events() if e.action == BROLL_ASSIGNED]
    assert len(events) == 1
    assert events[0].metadata["mode"] == "auto"
    assert events[0].metadata["assigned_count"] == 7


def test_broll_manager_refuses_blocked_or_unattached_assets(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "agent.db"
    migrate(db_path)
    database = Database(db_path)
    clock = Clock()
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    channel, item, _, _ = _seed_longform_item(database, clock)
    flags = FeatureFlags(longform_enabled=True)
    service = LongFormProjectService(database, audit, flags, clock=clock)
    broll = BrollManager(database, audit, flags, clock=clock)

    project = service.create(item.id, actor=USER)
    planned = service.plan(project.id, expected_version=1, actor=USER)

    registry = AssetRegistry(database, audit, clock=clock)
    unattached = registry.register(
        channel.id,
        kind=AssetKind.VIDEO_CLIP,
        category=AssetCategory.USER_OWNED,
        owner="producer",
        title="Unattached Clip",
        source="https://assets.example/unattached.mp4",
        actor=USER,
    )

    with pytest.raises(LongFormProjectError) as exc_info:
        broll.assign(
            project.id,
            [BrollAssignment(scene_index=0, asset_id=unattached.id)],
            expected_version=planned.version,
            actor=USER,
        )
    assert exc_info.value.code == LongFormProjectErrorCode.ASSET_NOT_ATTACHED.value

    # Attach an asset but give it an unresolved HIGH rights record.
    registry.attach(unattached.id, item.id, purpose="b-roll", actor=USER)
    record = RightsRecord.create(
        item.id,
        unattached.id,
        source="https://assets.example/unattached.mp4",
        clock=clock,
    ).with_risk_level(RiskLevel.HIGH, clock=clock)
    with database.transaction() as conn:
        RightsRecordRepository(conn).add(record)

    with pytest.raises(BrollError) as blocked_exc:
        broll.assign(
            project.id,
            [BrollAssignment(scene_index=0, asset_id=unattached.id)],
            expected_version=planned.version,
            actor=USER,
        )
    assert blocked_exc.value.code == BrollErrorCode.ASSET_RIGHTS_BLOCKED.value


def test_broll_manager_refuses_when_longform_disabled(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    migrate(db_path)
    database = Database(db_path)
    broll = BrollManager(
        database,
        AuditLog(InMemoryAuditSink()),
        FeatureFlags(longform_enabled=False),
    )

    with pytest.raises(LongFormDisabledError):
        broll.auto_assign_backgrounds("project-1", expected_version=1, actor=USER)
