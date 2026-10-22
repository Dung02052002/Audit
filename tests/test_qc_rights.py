"""Tests for K-126 Rights QC check (`qc.rights`)."""

from datetime import UTC, datetime

from ai_youtube_agent.content.asset import Asset, AssetCategory, AssetKind
from ai_youtube_agent.content.asset_usage import AssetUsage
from ai_youtube_agent.content.qc import QCStatus
from ai_youtube_agent.content.qc_rights import (
    RIGHTS_QC_CHECK_NAME,
    RightsQCChecker,
    evaluate_rights_qc,
)
from ai_youtube_agent.content.rights import RightsRecord, RiskLevel
from ai_youtube_agent.content.rights_assessment import Outcome, RightsAssessment
from ai_youtube_agent.content.rights_report import (
    AssetReport,
    BlockingRecord,
    GateSummary,
    RecordReport,
    RightsReport,
)
from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.config import Settings
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.asset import (
    AssetRepository,
    AssetUsageRepository,
)
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.review import RightsRecordRepository
from ai_youtube_agent.core.db.repositories.rights_assessment import (
    RightsAssessmentRepository,
)
from factories import make_channel, make_content_item, make_strategy_profile

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
SYSTEM = Actor(ActorKind.SYSTEM, "qc")


def test_evaluate_rights_qc_fails_when_gate_blocks() -> None:
    report = RightsReport(
        content_item_id="item-1",
        channel_id="ch-1",
        generated_at=NOW,
        worst_level="high",
        records=(
            RecordReport(
                id="rec-1",
                asset_ref="asset-1",
                status="unresolved",
                level="high",
                resolved_at=None,
                has_license=False,
                has_source=False,
                assessment=None,
                blocks=True,
                gate_code="rights.unresolved_high",
            ),
        ),
        assets=(),
        disclosure=None,
        gate=GateSummary(
            blocks=True,
            blocking_levels=("high", "unknown"),
            blocking_records=(
                BlockingRecord("rec-1", "asset-1", "rights.unresolved_high"),
            ),
        ),
    )
    check = evaluate_rights_qc(report)
    assert check.name == RIGHTS_QC_CHECK_NAME
    assert check.status is QCStatus.FAIL
    assert "asset-1:rights.unresolved_high" in (check.detail or "")


def test_evaluate_rights_qc_fails_when_attached_asset_missing_record() -> None:
    report = RightsReport(
        content_item_id="item-1",
        channel_id="ch-1",
        generated_at=NOW,
        worst_level=None,
        records=(),
        assets=(
            AssetReport(
                asset_id="asset-untracked",
                kind="image",
                category="licensed",
                provenance_id=None,
                has_source_url=False,
                has_license=False,
                has_license_url=False,
                has_proof=False,
                has_owner=False,
                has_file_sha256=False,
                has_attribution=False,
                missing=(
                    "attribution",
                    "file_sha256",
                    "license",
                    "license_url",
                    "owner",
                    "proof",
                    "source_url",
                ),
            ),
        ),
        disclosure=None,
        gate=GateSummary(
            blocks=False,
            blocking_levels=("high", "unknown"),
            blocking_records=(),
        ),
    )
    check = evaluate_rights_qc(report)
    assert check.status is QCStatus.FAIL
    assert "asset-untracked" in (check.detail or "")


def test_evaluate_rights_qc_warns_on_unresolved_medium_risk() -> None:
    report = RightsReport(
        content_item_id="item-1",
        channel_id="ch-1",
        generated_at=NOW,
        worst_level="medium",
        records=(
            RecordReport(
                id="rec-1",
                asset_ref="asset-1",
                status="unresolved",
                level="medium",
                resolved_at=None,
                has_license=True,
                has_source=True,
                assessment=None,
                blocks=False,
                gate_code=None,
            ),
        ),
        assets=(),
        disclosure=None,
        gate=GateSummary(
            blocks=False,
            blocking_levels=("high", "unknown"),
            blocking_records=(),
        ),
    )
    check = evaluate_rights_qc(report)
    assert check.status is QCStatus.WARN
    assert "medium" in (check.detail or "")


def test_rights_qc_checker_end_to_end(database: Database) -> None:
    settings = Settings(_env_file=None)
    channel = make_channel()
    strategy = make_strategy_profile(channel)
    item = make_content_item(channel, strategy)
    asset = Asset.create(
        channel.id,
        AssetKind.IMAGE,
        AssetCategory.GENERATED,
        title="scene-1.png",
        source="ai-gen",
        clock=lambda: NOW,
    )
    record = RightsRecord.create(
        item.id,
        asset.id,
        source="ai-gen",
        license="internal",
        clock=lambda: NOW,
    ).with_risk_level(RiskLevel.LOW, clock=lambda: NOW)
    assessment = RightsAssessment.create(
        record.id,
        item.id,
        Outcome(
            level=RiskLevel.LOW,
            rule_codes=("generated.declared",),
            asset_id=asset.id,
            provenance_id=None,
        ),
        assessed_by=SYSTEM,
        clock=lambda: NOW,
    )
    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(strategy)
        ContentItemRepository(conn).add(item)
        assets_repo = AssetRepository(conn)
        assets_repo.add(asset)
        AssetUsageRepository(conn).add(
            AssetUsage.create(asset.id, item.id, attached_by=SYSTEM, clock=lambda: NOW)
        )
        RightsRecordRepository(conn).add(record)
        RightsAssessmentRepository(conn).add(assessment)

    checker = RightsQCChecker(database, settings, clock=lambda: NOW)
    evaluation = checker.evaluate(item.id)
    assert not evaluation.is_blocking
    assert evaluation.check.status is QCStatus.PASS
    assert evaluation.check.name == RIGHTS_QC_CHECK_NAME
