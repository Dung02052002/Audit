"""Tests for K-130 QC Regression Suite (`qc_regression.py` and `QCReportBuilder`).

End-to-end regression suite covering both ``Shorts`` (9:16 vertical) and
``LongForm`` (16:9 horizontal) across all 12 checks (#117–#128) and the
aggregated ``QCReport`` (#129).
"""

import dataclasses
from pathlib import Path

from ai_youtube_agent.content.asset import Asset, AssetCategory, AssetKind
from ai_youtube_agent.content.asset_usage import AssetUsage
from ai_youtube_agent.content.policy_check import PolicyStatus
from ai_youtube_agent.content.qc import QCStatus
from ai_youtube_agent.content.qc_regression import (
    DEFAULT_REGRESSION_TIME,
    REGRESSION_ACTOR,
    build_longform_qc_fixture,
    build_shorts_qc_fixture,
)
from ai_youtube_agent.content.qc_report import (
    ALL_QC_CHECK_NAMES,
    QC_REPORT_AUDIT_ACTION,
    QCGate,
    TestGate,
)
from ai_youtube_agent.content.shorts_metadata import ShortsMetadata
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.artifact_storage import LocalFileStorage
from ai_youtube_agent.core.content_item import ContentStatus
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.asset import (
    AssetRepository,
    AssetUsageRepository,
)
from ai_youtube_agent.core.db.repositories.content import ArtifactRepository
from ai_youtube_agent.core.db.repositories.review import QCResultRepository
from ai_youtube_agent.core.gates import GateContext, GateOutcome


def test_shorts_qc_regression_clean_pass(database: Database, tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path / "shorts_artifacts")
    fixture = build_shorts_qc_fixture(database, storage)

    report = fixture.report_builder.generate(
        fixture.item.id,
        actor=REGRESSION_ACTOR,
        actual_width=1080,
        actual_height=1920,
        actual_fps=30.0,
    )

    assert report.status is QCStatus.PASS
    assert not report.blocks
    assert len(report.checks) == 12
    assert tuple(c.name for c in report.checks) == ALL_QC_CHECK_NAMES
    assert len(report.passed_checks) == 12
    assert len(report.warning_checks) == 0
    assert len(report.failed_checks) == 0

    with database.transaction() as conn:
        stored = QCResultRepository(conn).get(report.qc_result.id)
        assert stored is not None
        assert stored.status is QCStatus.PASS
        assert len(stored.artifact_ids) == 5

    events = [
        e for e in fixture.audit_sink.events() if e.action == QC_REPORT_AUDIT_ACTION
    ]
    assert len(events) == 1
    assert events[0].metadata["status"] == "pass"
    assert events[0].metadata["pass_count"] == 12
    assert events[0].metadata["fail_count"] == 0


def test_longform_qc_regression_clean_pass(database: Database, tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path / "longform_artifacts")
    fixture = build_longform_qc_fixture(database, storage)

    report = fixture.report_builder.generate(
        fixture.item.id,
        actor=REGRESSION_ACTOR,
        actual_width=1920,
        actual_height=1080,
        actual_fps=30.0,
    )

    assert report.status is QCStatus.PASS
    assert not report.blocks
    assert len(report.checks) == 12
    assert tuple(c.name for c in report.checks) == ALL_QC_CHECK_NAMES
    assert len(report.passed_checks) == 12
    assert len(report.warning_checks) == 0
    assert len(report.failed_checks) == 0

    report_dict = report.to_dict()
    assert report_dict["status"] == "pass"
    assert report_dict["summary"] == {
        "total": 12,
        "passed": 12,
        "warnings": 0,
        "failed": 0,
    }


def test_shorts_and_longform_ai_disclosure_required_regression(
    database: Database, tmp_path: Path
) -> None:
    storage = LocalFileStorage(tmp_path / "disclosure_artifacts")
    shorts_fixture = build_shorts_qc_fixture(database, storage, realistic_visual=True)
    shorts_report = shorts_fixture.report_builder.generate(
        shorts_fixture.item.id,
        actor=REGRESSION_ACTOR,
        actual_width=1080,
        actual_height=1920,
        actual_fps=30.0,
    )
    assert shorts_report.status is QCStatus.PASS
    assert shorts_report.disclosure_evaluation.metadata_satisfied

    # Now write a new metadata version that omits synthetic media disclosure
    bad_meta = ShortsMetadata(
        project_id="shorts-proj-1",
        project_version=1,
        content_item_id=shorts_fixture.item.id,
        channel_id=shorts_fixture.channel.id,
        script_id="shorts-scr-1",
        script_version=1,
        strategy_version=shorts_fixture.strategy.version,
        title="Fast AI Quality Control Tip #Shorts",
        description="Validate every video artifact before publishing. #Shorts",
        tags=("shorts", "ai", "qc"),
        language="en-US",
        made_for_kids=False,
        contains_synthetic_media=False,
        disclosure_decision_id=shorts_fixture.disclosure_record.id,
        policy_rule_set_id="policy",
        policy_rule_set_version=1,
        policy_status=PolicyStatus.PASS,
    )
    raw_bad = bad_meta.to_json_bytes()
    stored_bad = storage.write(
        shorts_fixture.item.id,
        ArtifactKind.METADATA,
        2,
        raw_bad,
        media_type="application/json",
    )
    next_meta_art = shorts_fixture.metadata_artifact.next_version(
        uri=stored_bad.uri,
        sha256=stored_bad.sha256,
        size_bytes=stored_bad.size_bytes,
        media_type="application/json",
        clock=lambda: DEFAULT_REGRESSION_TIME,
    )
    with database.transaction() as conn:
        ArtifactRepository(conn).add(next_meta_art)

    failed_report = shorts_fixture.report_builder.generate(
        shorts_fixture.item.id,
        actor=REGRESSION_ACTOR,
        actual_width=1080,
        actual_height=1920,
        actual_fps=30.0,
    )
    assert failed_report.status is QCStatus.FAIL
    assert failed_report.blocks
    failed_names = {c.name for c in failed_report.failed_checks}
    assert "video.metadata" in failed_names
    assert "qc.ai_disclosure" in failed_names


def test_regression_detects_resolution_fps_and_black_frame_anomalies(
    database: Database, tmp_path: Path
) -> None:
    storage = LocalFileStorage(tmp_path / "anomaly_artifacts")
    fixture = build_longform_qc_fixture(database, storage)

    # 1. Warn on moderate black frames (~15% black)
    warn_lumas = [0.0] * 15 + [0.3 + 0.005 * i for i in range(85)]
    warn_report = fixture.report_builder.generate(
        fixture.item.id,
        actor=REGRESSION_ACTOR,
        actual_width=1920,
        actual_height=1080,
        actual_fps=30.0,
        frame_luma_samples=warn_lumas,
    )
    assert warn_report.status is QCStatus.WARN
    assert not warn_report.blocks
    assert [c.name for c in warn_report.warning_checks] == ["video.black_frames"]

    # 2. Fail on wrong aspect ratio (9:16 for LongForm), wrong FPS (24), and 40% black
    # frames
    fail_lumas = [0.0] * 40 + [0.3 + 0.005 * i for i in range(60)]
    fail_report = fixture.report_builder.generate(
        fixture.item.id,
        actor=REGRESSION_ACTOR,
        actual_width=1080,
        actual_height=1920,
        actual_fps=24.0,
        frame_luma_samples=fail_lumas,
    )
    assert fail_report.status is QCStatus.FAIL
    assert fail_report.blocks
    failed_names = {c.name for c in fail_report.failed_checks}
    assert failed_names == {
        "video.resolution",
        "video.fps",
        "video.black_frames",
    }


def test_regression_detects_rights_and_policy_failures_and_gate_staleness(
    database: Database, tmp_path: Path
) -> None:
    storage = LocalFileStorage(tmp_path / "gate_artifacts")
    fixture = build_shorts_qc_fixture(database, storage)

    # First generate a clean report and verify TestGate and QCGate pass
    clean_report = fixture.report_builder.generate(
        fixture.item.id,
        actor=REGRESSION_ACTOR,
        actual_width=1080,
        actual_height=1920,
        actual_fps=30.0,
    )
    assert clean_report.status is QCStatus.PASS

    approved_item = dataclasses.replace(fixture.item, status=ContentStatus.APPROVED)
    gate_ctx = GateContext(
        approved_item,
        ContentStatus.PUBLISHING,
        REGRESSION_ACTOR,
        DEFAULT_REGRESSION_TIME,
    )
    with database.transaction() as conn:
        qc_repo = QCResultRepository(conn)
        art_repo = ArtifactRepository(conn)
        assert (
            TestGate(qc_repo, art_repo).evaluate(gate_ctx).outcome is GateOutcome.PASS
        )
        assert QCGate(qc_repo, art_repo).evaluate(gate_ctx).outcome is GateOutcome.PASS

    # Attach an untracked licensed asset and write a metadata version with a banned
    # phrase
    untracked_asset = Asset.create(
        fixture.channel.id,
        AssetKind.AUDIO,
        AssetCategory.LICENSED,
        title="untracked.mp3",
        source="stock-site",
        license_ref="lic-1",
        clock=lambda: DEFAULT_REGRESSION_TIME,
    )
    bad_policy_meta = ShortsMetadata(
        project_id="shorts-proj-1",
        project_version=1,
        content_item_id=fixture.item.id,
        channel_id=fixture.channel.id,
        script_id="shorts-scr-1",
        script_version=1,
        strategy_version=fixture.strategy.version,
        title="Fast AI Quality Control Tip #Shorts",
        description="Get guaranteed_profit right now! #Shorts",
        tags=("shorts", "ai"),
        language="en-US",
        made_for_kids=False,
        contains_synthetic_media=False,
        disclosure_decision_id=fixture.disclosure_record.id,
        policy_rule_set_id="policy",
        policy_rule_set_version=1,
        policy_status=PolicyStatus.PASS,
    )
    raw_bad_policy = bad_policy_meta.to_json_bytes()
    stored_bad_policy = storage.write(
        fixture.item.id,
        ArtifactKind.METADATA,
        2,
        raw_bad_policy,
        media_type="application/json",
    )
    next_meta_art = fixture.metadata_artifact.next_version(
        uri=stored_bad_policy.uri,
        sha256=stored_bad_policy.sha256,
        size_bytes=stored_bad_policy.size_bytes,
        media_type="application/json",
        clock=lambda: DEFAULT_REGRESSION_TIME,
    )
    with database.transaction() as conn:
        assets_repo = AssetRepository(conn)
        assets_repo.add(untracked_asset)
        AssetUsageRepository(conn).add(
            AssetUsage.create(
                untracked_asset.id,
                fixture.item.id,
                attached_by=REGRESSION_ACTOR,
                clock=lambda: DEFAULT_REGRESSION_TIME,
            )
        )
        ArtifactRepository(conn).add(next_meta_art)

    # Before rerunning QCReportBuilder, TestGate and QCGate detect stale artifacts
    with database.transaction() as conn:
        qc_repo = QCResultRepository(conn)
        art_repo = ArtifactRepository(conn)
        stale_test = TestGate(qc_repo, art_repo).evaluate(gate_ctx)
        stale_qc = QCGate(qc_repo, art_repo).evaluate(gate_ctx)
        assert stale_test.outcome is GateOutcome.BLOCK
        assert stale_test.reasons[0].code == "test.stale"
        assert stale_qc.outcome is GateOutcome.BLOCK
        assert stale_qc.reasons[0].code == "qc.stale"

    # Rerun QCReportBuilder: qc.rights and qc.policy now fail
    blocked_report = fixture.report_builder.generate(
        fixture.item.id,
        actor=REGRESSION_ACTOR,
        actual_width=1080,
        actual_height=1920,
        actual_fps=30.0,
    )
    assert blocked_report.status is QCStatus.FAIL
    failed_names = {c.name for c in blocked_report.failed_checks}
    assert "qc.rights" in failed_names
    assert "qc.policy" in failed_names
