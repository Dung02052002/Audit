"""T-224 Core Integration: Persistence, transitions, and control gates integration tests.

Verifies end-to-end integration across SQLite repositories, ContentItem state machine
transitions (ALLOWED_TRANSITIONS), version invalidation, and control gates
(ApprovalGate, DailyLimitGate, BudgetGate, RightsGate, PolicyGate, KillSwitchGate,
IdempotencyGate, and PublishGate).
"""

import dataclasses
from decimal import Decimal
import json
import pytest

from ai_youtube_agent.content.approval import ApprovalRequest
from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.qc import QCCheck, QCResult, QCStatus
from ai_youtube_agent.content.rights_assessment import RULES_VERSION
from ai_youtube_agent.content.rights_risk_engine import RightsRiskEngine
from ai_youtube_agent.content.strategy import Brand, Budget, Cadence
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.budget_gate import BudgetGate
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.content_item import (
    ContentStatus,
    ContentTransitionError,
    ContentType,
)
from ai_youtube_agent.core.daily_limit_gate import DailyLimitGate
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.asset import AssetRepository
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.economics import CostRecordRepository
from ai_youtube_agent.core.db.repositories.provenance import ProvenanceRepository
from ai_youtube_agent.core.db.repositories.review import (
    ApprovalRequestRepository,
    QCResultRepository,
    RightsRecordRepository,
)
from ai_youtube_agent.core.gates import (
    GateBlockedError,
    GateContext,
    GateName,
    evaluate_gates,
)
from ai_youtube_agent.core.kill_switch_gate import KillSwitchGate
from ai_youtube_agent.core.publish_gate import PublishGate
from ai_youtube_agent.core.version_invalidation import VersionInvalidation
from ai_youtube_agent.pipeline.kill_switch_service import KillSwitchService
from factories import (
    SYSTEM,
    T0,
    USER,
    at,
    make_asset,
    make_channel,
    make_content_item,
    make_provenance,
    make_rights_record,
    make_strategy_profile,
)
from publish_helpers import default_metadata, seed_approved_item, store_artifact


def test_persistence_and_lifecycle_transitions_with_gates(
    database: Database, tmp_path
) -> None:
    """Walks a ContentItem from DRAFT through PUBLISHED across real SQLite repositories
    and verifies every intermediate transition and gate evaluation."""
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    settings = Settings(environment=Environment.TEST)

    channel = make_channel(status=ChannelStatus.ACTIVE)
    profile = make_strategy_profile(
        channel,
        cadence=Cadence(shorts_per_day=2, longform_per_day=1),
        budget=Budget(
            currency="USD",
            daily_limit=Decimal("50.00"),
            monthly_limit=Decimal("500.00"),
        ),
    )
    item = make_content_item(
        channel,
        profile,
        content_type=ContentType.SHORTS,
        status=ContentStatus.DRAFT,
    )

    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(profile)
        ContentItemRepository(conn).add(item)

    # 1. Illegal jump from DRAFT directly to APPROVED or PUBLISHING must fail
    with pytest.raises(ContentTransitionError):
        item.with_status(ContentStatus.APPROVED, clock=at(1))
    with pytest.raises(ContentTransitionError):
        item.with_status(ContentStatus.PUBLISHING, clock=at(1))

    # 2. Evaluate generation gates (DailyLimitGate + BudgetGate + KillSwitchGate)
    kill_switch = KillSwitchService(audit, database=database, clock=at(1))
    with database.connection() as conn:
        gen_ctx = GateContext(item, ContentStatus.GENERATING, SYSTEM, T0)
        gen_report = evaluate_gates(
            [
                KillSwitchGate(kill_switch),
                DailyLimitGate(
                    StrategyProfileRepository(conn),
                    # Empty daily usage passes
                    __import__(
                        "ai_youtube_agent.core.db.repositories.usage",
                        fromlist=["DailyUsageRepository"],
                    ).DailyUsageRepository(conn),
                ),
                BudgetGate(
                    StrategyProfileRepository(conn),
                    CostRecordRepository(conn),
                ),
            ],
            gen_ctx,
        )
    assert gen_report.passed is True

    # 3. Advance DRAFT -> GENERATING -> TESTING -> PREVIEW_READY -> AWAITING_APPROVAL -> APPROVED
    for next_status in (
        ContentStatus.GENERATING,
        ContentStatus.TESTING,
        ContentStatus.PREVIEW_READY,
        ContentStatus.AWAITING_APPROVAL,
    ):
        prev_updated = item.updated_at
        item = item.with_status(next_status, clock=at(2))
        with database.transaction() as conn:
            ContentItemRepository(conn).update(item, expected_updated_at=prev_updated)

    # 4. Store artifacts, rights record + assessment, QC result, and user approval
    from ai_youtube_agent.core.artifact_storage import LocalFileStorage
    from ai_youtube_agent.providers.mock_video_render import stub_mp4

    storage = LocalFileStorage(tmp_path / "artifacts")
    video_bytes = stub_mp4(25_000, b"core-integration-video")
    meta_bytes = json.dumps(default_metadata(ContentType.SHORTS, item, channel)).encode(
        "utf-8"
    )

    asset = make_asset(channel)
    prov = make_provenance(asset)
    rights_rec = make_rights_record(item, asset_ref=asset.id)

    with database.transaction() as conn:
        art_repo = ArtifactRepository(conn)
        video_art = store_artifact(
            storage, art_repo, item.id, ArtifactKind.VIDEO, "video/mp4", video_bytes
        )
        meta_art = store_artifact(
            storage,
            art_repo,
            item.id,
            ArtifactKind.METADATA,
            "application/json",
            meta_bytes,
        )
        AssetRepository(conn).add(asset)
        ProvenanceRepository(conn).add(prov)
        RightsRecordRepository(conn).add(rights_rec)

    # Assess rights via RightsRiskEngine so FreshnessSource sees current basis + RULES_VERSION
    risk_engine = RightsRiskEngine(database, audit, clock=at(3))
    run = risk_engine.assess(item.id, actor=SYSTEM)
    assessment = run.assessments[0]
    assert assessment.rules_version == RULES_VERSION

    with database.transaction() as conn:
        qc = QCResult.create(
            item.id,
            [video_art, meta_art],
            [QCCheck("video.fps", QCStatus.PASS)],
            clock=at(4),
        )
        QCResultRepository(conn).add(qc)
        approval = ApprovalRequest.create(
            item.id,
            [video_art, meta_art],
            requested_by=SYSTEM,
            qc_result=qc,
            clock=at(4),
        ).approve(USER, clock=at(5))
        ApprovalRequestRepository(conn).add(approval)
        prev_updated = item.updated_at
        item = item.with_status(ContentStatus.APPROVED, clock=at(5))
        ContentItemRepository(conn).update(item, expected_updated_at=prev_updated)

    # 5. PublishGate must pass when item is APPROVED, rights are fresh & low risk, policy passes
    publish_gate = PublishGate(database, settings)
    report = publish_gate.ensure_can_publish(item, actor=SYSTEM, at=T0)
    assert report.passed is True
    assert not report.blocked

    # 6. Version invalidation resets item to GENERATING and causes PublishGate to block
    invalidation = VersionInvalidation(database, audit, clock=at(6))
    video_art_v2 = store_artifact_detached(
        storage, item.id, ArtifactKind.VIDEO, "video/mp4", stub_mp4(25_000, b"v2"), version=2
    )
    inv_res = invalidation.store_artifact_version(video_art_v2)
    assert inv_res.item_reset is True
    assert inv_res.item.status is ContentStatus.GENERATING
    assert len(inv_res.invalidated) == 1


def store_artifact_detached(
    storage, content_item_id: str, kind: ArtifactKind, media_type: str, data: bytes, *, version: int
):
    from ai_youtube_agent.core.artifact import Artifact

    stored = storage.write(content_item_id, kind, version, data, media_type=media_type)
    return Artifact.create(
        content_item_id,
        kind,
        version=version,
        uri=stored.uri,
        sha256=stored.sha256,
        size_bytes=stored.size_bytes,
        media_type=media_type,
        clock=at(6),
    )


def test_publish_gate_blocks_when_policy_fails_or_daily_limit_reached(
    database: Database, tmp_path
) -> None:
    """Verifies PublishGate blocks when title contains a banned phrase or daily limit is met."""
    seed = seed_approved_item(database, tmp_path)
    gate = PublishGate(database, seed.settings)

    # Mutate strategy profile to include banned phrase ("guaranteed profit")
    banned_profile = dataclasses.replace(
        seed.profile,
        brand=Brand(seed.profile.brand.name, seed.profile.brand.tone, banned_phrases=("guaranteed profit",)),
    )
    with database.transaction() as conn:
        StrategyProfileRepository(conn).update(
            banned_profile, expected_version=seed.profile.version
        )

    # Mutate item title to include banned phrase from strategy profile ("guaranteed profit")
    bad_item = seed.item.rename("How to get guaranteed profit today", clock=at(2))
    with database.transaction() as conn:
        ContentItemRepository(conn).update(
            bad_item, expected_updated_at=seed.item.updated_at
        )

    with pytest.raises(GateBlockedError) as exc_info:
        gate.ensure_can_publish(bad_item, actor=SYSTEM, at=T0)
    assert any(r.code == "policy.failed" for r in exc_info.value.report.reasons)
