"""T-235 Recovery Test: Simulates worker crash and upload interruption, verifying
safe checkpoint recovery without duplicate artifact work or duplicate uploads.
"""

from datetime import UTC, datetime, timedelta
import pytest

from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.content_item import ContentStatus
from ai_youtube_agent.core.db.codec import format_datetime
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.pipeline.crash_resume import CrashResumeService
from ai_youtube_agent.pipeline.job import AIJob, AIJobStatus
from ai_youtube_agent.pipeline.job_queue import LEASE_STEP_NAME, SqliteJobQueue
from ai_youtube_agent.pipeline.shorts_publisher import ShortsPublisher
from ai_youtube_agent.providers.youtube_upload import (
    UploadErrorCode,
    YouTubeUploadError,
)
from factories import SYSTEM
from publish_helpers import PUBLISH_FLAGS, build_publishing, seed_approved_item


def test_crash_recovery_resumes_ai_job_from_last_safe_checkpoint(
    database: Database,
) -> None:
    """Simulates a worker crash mid-pipeline after 'script.generated' and
    'audio.synthesized' checkpoints, then verifies CrashResumeService recovers
    the job from its last safe checkpoint without losing completed steps."""
    t0 = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    current_time = [t0]
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)

    queue = SqliteJobQueue(database, clock=lambda: current_time[0])
    crashed_job = (
        AIJob.create(
            "content.generate",
            "key-recovery-e2e-1",
            clock=lambda: current_time[0],
        )
        .start(clock=lambda: current_time[0])
        .checkpoint(
            LEASE_STEP_NAME,
            {
                "worker_id": "worker-1",
                "token": "lease-1",
                "expires_at": format_datetime(t0 + timedelta(seconds=60)),
            },
            clock=lambda: t0 + timedelta(seconds=1),
        )
        .checkpoint(
            "script.generated",
            {"script_version": 1},
            clock=lambda: t0 + timedelta(seconds=10),
        )
        .checkpoint(
            "audio.synthesized",
            {"audio_artifact_id": "art-audio-1"},
            clock=lambda: t0 + timedelta(seconds=20),
        )
    )
    with database.transaction() as conn:
        AIJobRepository(conn).add(crashed_job)

    # Advance clock past the 60s lease expiry to simulate worker-1 crash
    current_time[0] = t0 + timedelta(seconds=120)
    resume_svc = CrashResumeService(
        database,
        audit,
        queue=queue,
        max_attempts=3,
        clock=lambda: current_time[0],
    )

    report = resume_svc.recover_interrupted_jobs(force_reclaim=False)
    assert report.scanned_running_jobs == 1
    assert len(report.recovered_jobs) == 1
    plan = report.recovered_jobs[0]
    assert plan.resumed is True
    assert plan.job.status is AIJobStatus.RUNNING
    assert plan.checkpoint_state.last_safe_step == "audio.synthesized"
    assert plan.checkpoint_state.completed_domain_steps == (
        "script.generated",
        "audio.synthesized",
    )

    inspected = resume_svc.inspect_checkpoint_state(crashed_job.id)
    assert "script.generated" in inspected.completed_domain_steps
    assert "audio.synthesized" in inspected.completed_domain_steps
    assert "video.rendered" not in inspected.completed_domain_steps


def test_interrupted_publish_resumes_safely_without_duplicate_upload(
    database: Database, tmp_path
) -> None:
    """Simulates a network interruption during upload (exhausting initial retries),
    leaving the item in PUBLISHING and the PublishJob in FAILED, then resumes publish
    and verifies exactly one upload succeeds and subsequent calls replay."""
    seed = seed_approved_item(database, tmp_path)
    pub = build_publishing(seed)
    publisher = ShortsPublisher(
        database, seed.storage, pub.executor, flags=PUBLISH_FLAGS
    )

    # Fail all 3 attempts of the first publish call with a resumable NETWORK_ERROR
    pub.uploader.fail_next(UploadErrorCode.NETWORK_ERROR, times=3)
    with pytest.raises(YouTubeUploadError):
        publisher.publish(seed.item.id, actor=SYSTEM)

    assert len(pub.uploader.uploads) == 0

    # Resume the interrupted publish: PublishDuplicateGuard returns RESUME,
    # PublishExecutor re-evaluates PublishGate, uploads once, and marks item PUBLISHED
    resumed_outcome = publisher.publish(seed.item.id, actor=SYSTEM)
    assert resumed_outcome.replayed is False
    assert resumed_outcome.item.status is ContentStatus.PUBLISHED
    assert len(pub.uploader.uploads) == 1

    # Calling publish again replays idempotently without uploading a second time
    replay_outcome = publisher.publish(seed.item.id, actor=SYSTEM)
    assert replay_outcome.replayed is True
    assert replay_outcome.result.youtube_video_id == resumed_outcome.result.youtube_video_id
    assert len(pub.uploader.uploads) == 1
