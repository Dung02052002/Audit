"""Tests for T-237 LongFormUnlockService."""

import pytest

from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.pipeline.kill_switch_service import KillSwitchService
from ai_youtube_agent.pipeline.longform_unlock import (
    LONGFORM_UNLOCK_BLOCKED_ACTION,
    LONGFORM_UNLOCKED_ACTION,
    LongFormUnlockBlockedError,
    LongFormUnlockNotAllowedError,
    LongFormUnlockService,
)
from ai_youtube_agent.pipeline.pilot_runner import ShortsPilotRunner
from ai_youtube_agent.pipeline.shorts_publisher import ShortsPublisher
from factories import SYSTEM, USER, at
from publish_helpers import PUBLISH_FLAGS, build_publishing, seed_approved_item


def test_longform_unlock_blocked_until_shorts_pilot_passes_and_requires_user(
    database: Database, tmp_path
) -> None:
    """Verifies that LongFormUnlockService:
    1. Rejects SYSTEM actors with LongFormUnlockNotAllowedError.
    2. Blocks unlock before the Shorts pilot is published.
    3. Unlocks longform_enabled=True once the Shorts pilot succeeds and all gates pass.
    4. Blocks unlock if the kill switch is active.
    """
    seed = seed_approved_item(database, tmp_path)
    pub = build_publishing(seed)
    publisher = ShortsPublisher(
        database, seed.storage, pub.executor, flags=PUBLISH_FLAGS
    )

    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    kill_switch = KillSwitchService(audit, database=database, clock=at(5))
    pilot_runner = ShortsPilotRunner(
        database,
        audit,
        shorts_publisher=publisher,
        clock=at(5),
    )
    unlock_svc = LongFormUnlockService(
        database,
        audit,
        pilot_runner=pilot_runner,
        kill_switch=kill_switch,
        clock=at(5),
    )
    initial_flags = FeatureFlags(shorts_enabled=True, longform_enabled=False)

    # 1. SYSTEM actor cannot unlock LongForm
    with pytest.raises(LongFormUnlockNotAllowedError):
        unlock_svc.unlock_longform(
            seed.channel.id, actor=SYSTEM, current_flags=initial_flags
        )

    # 2. Before Shorts pilot is published, unlock is blocked by unlock.shorts_pilot_required
    with pytest.raises(LongFormUnlockBlockedError) as exc_info:
        unlock_svc.unlock_longform(
            seed.channel.id, actor=USER, current_flags=initial_flags
        )
    assert "unlock.shorts_pilot_required" in exc_info.value.evaluation.blockers
    assert any(e.action == LONGFORM_UNLOCK_BLOCKED_ACTION for e in sink.events())

    # 3. Complete the Shorts pilot with explicit USER approval
    pilot_report = pilot_runner.run_pilot(seed.item.id, reviewer=USER)
    assert pilot_report.passed is True

    # 4. Now LongFormUnlockService unlocks longform_enabled=True for USER
    decision = unlock_svc.unlock_longform(
        seed.channel.id, actor=USER, current_flags=initial_flags
    )
    assert decision.unlocked is True
    assert decision.updated_flags.longform_enabled is True
    assert decision.evaluation.passing_pilot_item_id == seed.item.id
    assert any(e.action == LONGFORM_UNLOCKED_ACTION for e in sink.events())

    # 5. If kill switch is activated, LongForm unlock is immediately blocked
    kill_switch.activate(actor=USER, reason="Testing emergency stop gate")
    with pytest.raises(LongFormUnlockBlockedError) as ks_exc:
        unlock_svc.unlock_longform(
            seed.channel.id, actor=USER, current_flags=initial_flags
        )
    assert "unlock.kill_switch_active" in ks_exc.value.evaluation.blockers
