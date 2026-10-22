"""Tests for K-128 AI Disclosure QC check (`qc.ai_disclosure`)."""

from datetime import UTC, datetime

from ai_youtube_agent.content.asset import Asset, AssetCategory, AssetKind
from ai_youtube_agent.content.asset_usage import AssetUsage
from ai_youtube_agent.content.disclosure import DisclosureRecord
from ai_youtube_agent.content.disclosure_rule import (
    DISCLOSURE_RULES,
    DisclosureFacts,
    evaluate_disclosure,
)
from ai_youtube_agent.content.qc import QCStatus
from ai_youtube_agent.content.qc_disclosure import (
    DISCLOSURE_QC_CHECK_NAME,
    DisclosureQCChecker,
    evaluate_disclosure_qc,
)
from ai_youtube_agent.core.audit import Actor, ActorKind
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
from ai_youtube_agent.core.db.repositories.disclosure import DisclosureRepository
from factories import make_channel, make_content_item, make_strategy_profile

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
SYSTEM = Actor(ActorKind.SYSTEM, "qc")


def test_evaluate_disclosure_qc_fails_when_record_missing() -> None:
    evaluation = evaluate_disclosure_qc("item-1", "ch-1", None)
    assert evaluation.check.name == DISCLOSURE_QC_CHECK_NAME
    assert evaluation.check.status is QCStatus.FAIL
    assert "No AI disclosure decision" in (evaluation.check.detail or "")


def test_evaluate_disclosure_qc_fails_when_required_not_in_metadata() -> None:
    eval_result = evaluate_disclosure(
        "item-1",
        "ch-1",
        DisclosureFacts(
            realistic_person=True,
            realistic_event=False,
            synthetic_voice_of_real_person=False,
            realistic_visual=False,
        ),
        (),
        rule_set=DISCLOSURE_RULES,
    )
    record = DisclosureRecord.create(
        "item-1",
        "ch-1",
        eval_result,
        decided_by=SYSTEM,
        clock=lambda: NOW,
    )
    qc_eval = evaluate_disclosure_qc(
        "item-1",
        "ch-1",
        record,
        metadata_payload={
            "disclosure_decision_id": record.id,
            "contains_synthetic_media": False,
        },
    )
    assert qc_eval.check.status is QCStatus.FAIL
    assert "contains_synthetic_media=False" in (qc_eval.check.detail or "")


def test_evaluate_disclosure_qc_passes_when_required_and_satisfied() -> None:
    eval_result = evaluate_disclosure(
        "item-1",
        "ch-1",
        DisclosureFacts(
            realistic_person=True,
            realistic_event=False,
            synthetic_voice_of_real_person=False,
            realistic_visual=False,
        ),
        (),
        rule_set=DISCLOSURE_RULES,
    )
    record = DisclosureRecord.create(
        "item-1",
        "ch-1",
        eval_result,
        decided_by=SYSTEM,
        clock=lambda: NOW,
    )
    qc_eval = evaluate_disclosure_qc(
        "item-1",
        "ch-1",
        record,
        metadata_payload={
            "disclosure_decision_id": record.id,
            "contains_synthetic_media": True,
        },
    )
    assert qc_eval.check.status is QCStatus.PASS
    assert qc_eval.metadata_satisfied


def test_disclosure_qc_checker_detects_asset_drift(database: Database) -> None:
    channel = make_channel()
    strategy = make_strategy_profile(channel)
    item = make_content_item(channel, strategy)
    eval_result = evaluate_disclosure(
        item.id,
        channel.id,
        DisclosureFacts(
            realistic_person=False,
            realistic_event=False,
            synthetic_voice_of_real_person=False,
            realistic_visual=True,
        ),
        (),
        rule_set=DISCLOSURE_RULES,
    )
    record = DisclosureRecord.create(
        item.id,
        channel.id,
        eval_result,
        decided_by=SYSTEM,
        clock=lambda: NOW,
    )
    new_asset = Asset.create(
        channel.id,
        AssetKind.IMAGE,
        AssetCategory.GENERATED,
        title="gen.png",
        source="ai-gen",
        clock=lambda: NOW,
    )
    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(strategy)
        ContentItemRepository(conn).add(item)
        DisclosureRepository(conn).add(record)
        assets_repo = AssetRepository(conn)
        assets_repo.add(new_asset)
        AssetUsageRepository(conn).add(
            AssetUsage.create(
                new_asset.id, item.id, attached_by=SYSTEM, clock=lambda: NOW
            )
        )

    checker = DisclosureQCChecker(database)
    qc_eval = checker.evaluate(item.id)
    assert qc_eval.check.status is QCStatus.FAIL
    assert "stale" in (qc_eval.check.detail or "")
