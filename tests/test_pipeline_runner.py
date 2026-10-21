"""Tests for PipelineRunner (Prompt Pack v8, prompt #206), context C16 Orchestration."""

from datetime import UTC, datetime
from decimal import Decimal
import uuid

import pytest

from ai_youtube_agent.content.approval import (
    ApprovalRequest,
    ApprovalStatus,
    ArtifactBinding,
)
from ai_youtube_agent.content.channel import Channel, ChannelStatus, YouTubeIdentifiers
from ai_youtube_agent.content.strategy import Cadence, StrategyProfile
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.config import Environment, Settings
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
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.pipeline.pipeline_runner import (
    PIPELINE_ADVANCED_ACTION,
    PipelineRunner,
)


def _seed_valid_channel(
    database: Database,
    channel_id: str,
    *,
    shorts_per_day: int = 5,
    longform_per_day: int = 2,
) -> tuple[Channel, StrategyProfile]:
    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    channel = Channel(
        id=channel_id,
        title="Pipeline Test Channel",
        youtube=YouTubeIdentifiers(channel_id=f"UC{channel_id[:22].ljust(22, 'x')}"),
        status=ChannelStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    strategy = StrategyProfile.create(
        channel_id=channel_id,
        actor=Actor(ActorKind.USER, "owner"),
        clock=lambda: now,
    )
    cadence = Cadence(
        shorts_per_day=shorts_per_day,
        longform_per_day=longform_per_day,
        time_zone="UTC",
    )
    strategy = strategy.update(cadence=cadence, actor=Actor(ActorKind.USER, "owner"), clock=lambda: now)

    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(strategy)

    return channel, strategy


def test_advance_draft_to_generating_evaluates_gates(database: Database) -> None:
    channel_id = str(uuid.uuid4())
    _seed_valid_channel(database, channel_id)

    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    item = ContentItem(
        id=str(uuid.uuid4()),
        channel_id=channel_id,
        strategy_profile_id=channel_id,
        strategy_version=1,
        content_type=ContentType.SHORTS,
        title="Test Shorts",
        status=ContentStatus.DRAFT,
        created_at=now,
        updated_at=now,
    )
    with database.transaction() as conn:
        ContentItemRepository(conn).add(item)

    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    settings = Settings(environment=Environment.TEST)
    runner = PipelineRunner(
        database=database,
        settings=settings,
        audit_log=audit,
        clock=lambda: now,
    )

    res = runner.advance(item.id, at=now)
    assert res.transitioned is True
    assert res.from_status is ContentStatus.DRAFT
    assert res.to_status is ContentStatus.GENERATING

    with database.connection() as conn:
        updated = ContentItemRepository(conn).get(item.id)
        assert updated is not None
        assert updated.status is ContentStatus.GENERATING

    assert any(e.action == PIPELINE_ADVANCED_ACTION for e in sink.events())


def test_advance_generating_to_testing_requires_video_artifact(database: Database) -> None:
    channel_id = str(uuid.uuid4())
    _seed_valid_channel(database, channel_id)

    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    item = ContentItem(
        id=str(uuid.uuid4()),
        channel_id=channel_id,
        strategy_profile_id=channel_id,
        strategy_version=1,
        content_type=ContentType.SHORTS,
        title="Test Shorts",
        status=ContentStatus.GENERATING,
        created_at=now,
        updated_at=now,
    )
    with database.transaction() as conn:
        ContentItemRepository(conn).add(item)

    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    settings = Settings(environment=Environment.TEST)
    runner = PipelineRunner(
        database=database,
        settings=settings,
        audit_log=audit,
        clock=lambda: now,
    )

    # 1. Without video artifact, advance fails
    res1 = runner.advance(item.id, at=now)
    assert res1.transitioned is False
    assert "video artifact" in (res1.reason or "").lower()

    # 2. Add video artifact
    video_artifact = Artifact(
        id=str(uuid.uuid4()),
        content_item_id=item.id,
        kind=ArtifactKind.VIDEO,
        version=1,
        uri="file:///path/to/video.mp4",
        sha256="a" * 64,
        size_bytes=1024,
        media_type="video/mp4",
        created_at=now,
    )
    with database.transaction() as conn:
        ArtifactRepository(conn).add(video_artifact)

    # 3. With video artifact, advance succeeds
    res2 = runner.advance(item.id, at=now)
    assert res2.transitioned is True
    assert res2.to_status is ContentStatus.TESTING


def test_advance_preview_ready_creates_approval_and_halts_at_approval_gate(
    database: Database,
) -> None:
    channel_id = str(uuid.uuid4())
    _seed_valid_channel(database, channel_id)

    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    item = ContentItem(
        id=str(uuid.uuid4()),
        channel_id=channel_id,
        strategy_profile_id=channel_id,
        strategy_version=1,
        content_type=ContentType.SHORTS,
        title="Test Shorts",
        status=ContentStatus.PREVIEW_READY,
        created_at=now,
        updated_at=now,
    )
    video_artifact = Artifact(
        id=str(uuid.uuid4()),
        content_item_id=item.id,
        kind=ArtifactKind.VIDEO,
        version=1,
        uri="file:///path/to/video.mp4",
        sha256="b" * 64,
        size_bytes=2048,
        media_type="video/mp4",
        created_at=now,
    )
    with database.transaction() as conn:
        ContentItemRepository(conn).add(item)
        ArtifactRepository(conn).add(video_artifact)

    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    settings = Settings(environment=Environment.TEST)
    runner = PipelineRunner(
        database=database,
        settings=settings,
        audit_log=audit,
        clock=lambda: now,
    )

    # 1. Advance from PREVIEW_READY -> AWAITING_APPROVAL
    step1 = runner.advance(item.id, at=now)
    assert step1.transitioned is True
    assert step1.to_status is ContentStatus.AWAITING_APPROVAL
    assert step1.waiting_for_approval is True

    # 2. Advance while in AWAITING_APPROVAL without human approval MUST HALT
    step2 = runner.advance(item.id, at=now)
    assert step2.transitioned is False
    assert step2.waiting_for_approval is True
    assert "Human approval required" in (step2.reason or "")

    # 3. Simulate human user approving request
    with database.transaction() as conn:
        app_repo = ApprovalRequestRepository(conn)
        reqs = app_repo.list_by_content_item(item.id)
        assert len(reqs) == 1
        req = reqs[0]
        approved_req = req.approve(Actor(ActorKind.USER, "human_reviewer"))
        app_repo.update(approved_req, expected_status=ApprovalStatus.PENDING)

    # 4. Advance now transitions to APPROVED
    step3 = runner.advance(item.id, at=now)
    assert step3.transitioned is True
    assert step3.to_status is ContentStatus.APPROVED


def test_advance_approved_without_publish_flag_safely_halts(database: Database) -> None:
    channel_id = str(uuid.uuid4())
    _seed_valid_channel(database, channel_id)

    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    item = ContentItem(
        id=str(uuid.uuid4()),
        channel_id=channel_id,
        strategy_profile_id=channel_id,
        strategy_version=1,
        content_type=ContentType.SHORTS,
        title="Test Shorts",
        status=ContentStatus.APPROVED,
        created_at=now,
        updated_at=now,
    )
    with database.transaction() as conn:
        ContentItemRepository(conn).add(item)

    flags = FeatureFlags(publish_enabled=False)
    settings = Settings(environment=Environment.TEST, flags=flags)
    runner = PipelineRunner(
        database=database,
        settings=settings,
        audit_log=AuditLog(InMemoryAuditSink()),
        flags=flags,
        clock=lambda: now,
    )

    res = runner.advance(item.id, at=now)
    assert res.transitioned is False
    assert "PUBLISH_ENABLED" in (res.reason or "")


def test_run_stops_at_human_approval_gate(database: Database) -> None:
    channel_id = str(uuid.uuid4())
    _seed_valid_channel(database, channel_id)

    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    item = ContentItem(
        id=str(uuid.uuid4()),
        channel_id=channel_id,
        strategy_profile_id=channel_id,
        strategy_version=1,
        content_type=ContentType.SHORTS,
        title="Test Shorts",
        status=ContentStatus.DRAFT,
        created_at=now,
        updated_at=now,
    )
    video_artifact = Artifact(
        id=str(uuid.uuid4()),
        content_item_id=item.id,
        kind=ArtifactKind.VIDEO,
        version=1,
        uri="file:///path/to/video.mp4",
        sha256="c" * 64,
        size_bytes=3000,
        media_type="video/mp4",
        created_at=now,
    )
    with database.transaction() as conn:
        ContentItemRepository(conn).add(item)
        ArtifactRepository(conn).add(video_artifact)

    settings = Settings(environment=Environment.TEST)
    runner = PipelineRunner(
        database=database,
        settings=settings,
        audit_log=AuditLog(InMemoryAuditSink()),
        clock=lambda: now,
    )

    run_res = runner.run(item.id, max_steps=5, at=now)
    assert run_res.initial_status is ContentStatus.DRAFT
    assert run_res.final_status is ContentStatus.AWAITING_APPROVAL
    assert run_res.waiting_for_approval is True
    assert run_res.completed is False
    assert len(run_res.steps) == 4  # DRAFT -> GENERATING -> TESTING -> PREVIEW_READY -> AWAITING_APPROVAL
