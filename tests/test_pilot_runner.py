"""Tests for T-236 ShortsPilotRunner and ShortsPilotReport."""

import pytest

from ai_youtube_agent.content.approval import ApprovalNotAllowedError
from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.content_item import ContentStatus
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.pipeline.pilot_runner import (
    PILOT_BLOCKED_ACTION,
    PILOT_COMPLETED_ACTION,
    ShortsPilotRunner,
)
from ai_youtube_agent.pipeline.shorts_publisher import ShortsPublisher
from factories import SYSTEM, USER, at
from publish_helpers import PUBLISH_FLAGS, build_publishing, seed_approved_item


def test_shorts_pilot_runner_requires_human_user_and_produces_technical_report(
    database: Database, tmp_path
) -> None:
    """Verifies that ShortsPilotRunner rejects SYSTEM actors, runs controlled Shorts
    publishing when given USER approval, and produces a passing ShortsPilotReport."""
    seed = seed_approved_item(database, tmp_path)
    pub = build_publishing(seed)
    publisher = ShortsPublisher(
        database, seed.storage, pub.executor, flags=PUBLISH_FLAGS
    )

    sink = InMemoryAuditSink()
    runner = ShortsPilotRunner(
        database,
        AuditLog(sink),
        shorts_publisher=publisher,
        clock=at(10),
    )

    # 1. SYSTEM actor cannot authorize a pilot run
    with pytest.raises(ApprovalNotAllowedError):
        runner.run_pilot(seed.item.id, reviewer=SYSTEM)

    # 2. Before publishing, evaluate_pilot reports pilot.publish_incomplete
    pre_report = runner.evaluate_pilot(seed.item.id)
    assert pre_report.passed is False
    assert "pilot.publish_incomplete" in pre_report.blockers
    assert any(e.action == PILOT_BLOCKED_ACTION for e in sink.events())

    # 3. Run pilot with explicit USER reviewer -> publishes and passes all 6 stage checks
    report = runner.run_pilot(seed.item.id, reviewer=USER)
    assert report.passed is True
    assert report.blockers == ()
    assert report.approved_by_user_id == USER.id
    assert report.video_artifact_id == seed.artifacts[seed.item.content_type.__class__.SHORTS and __import__("ai_youtube_agent.core.artifact", fromlist=["ArtifactKind"]).ArtifactKind.VIDEO].id
    assert report.youtube_video_id is not None
    assert len(report.stage_checks) == 6
    assert all(c.passed for c in report.stage_checks)

    md = report.to_markdown()
    assert "# T-236 Shorts Pilot Technical Report" in md
    assert "`PASS`" in md
    assert any(e.action == PILOT_COMPLETED_ACTION for e in sink.events())
