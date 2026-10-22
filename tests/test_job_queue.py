"""Tests for durable JobQueue abstraction (Prompt Pack v8, prompt R-204)."""

from datetime import UTC, datetime, timedelta

import pytest

from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.pipeline.job import AIJobStatus
from ai_youtube_agent.pipeline.job_queue import (
    InMemoryJobQueue,
    JobQueue,
    JobQueueError,
    SqliteJobQueue,
)

FIXED_NOW = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)


def test_job_queue_protocol_conformance(database: Database) -> None:
    sqlite_queue = SqliteJobQueue(database)
    in_memory_queue = InMemoryJobQueue()

    assert isinstance(sqlite_queue, JobQueue)
    assert isinstance(in_memory_queue, JobQueue)


def test_sqlite_job_queue_enqueue_dequeue_and_ack(database: Database) -> None:
    clock_time = FIXED_NOW
    queue = SqliteJobQueue(database, clock=lambda: clock_time)

    # Initially empty
    assert queue.size() == 0
    assert queue.dequeue("worker-1") is None

    # Enqueue a new job
    res1 = queue.enqueue_new("script.generate", idempotency_key="key-script-001")
    assert res1.is_new is True
    assert res1.reused is False
    assert res1.status is AIJobStatus.QUEUED
    assert queue.size() == 1

    # Idempotent re-enqueue of the same key reuses existing job
    res2 = queue.enqueue_new("script.generate", idempotency_key="key-script-001")
    assert res2.is_new is False
    assert res2.reused is True
    assert res2.job.id == res1.job.id
    assert queue.size() == 1

    # Dequeue with worker-1
    lease = queue.dequeue("worker-1", lease_duration=timedelta(minutes=5))
    assert lease is not None
    assert lease.worker_id == "worker-1"
    assert lease.job.id == res1.job.id
    assert lease.job.status is AIJobStatus.RUNNING
    assert lease.job.attempts == 1
    assert queue.size() == 0

    # Ack the job (succeed)
    succeeded = queue.ack(lease)
    assert succeeded.status is AIJobStatus.SUCCEEDED


def test_sqlite_job_queue_lease_expiration_and_renewal(database: Database) -> None:
    clock_time = FIXED_NOW
    queue = SqliteJobQueue(database, clock=lambda: clock_time)

    queue.enqueue_new("audio.generate", idempotency_key="key-audio-001")
    lease = queue.dequeue("worker-1", lease_duration=timedelta(minutes=2))
    assert lease is not None

    # Advance clock past lease expiration
    clock_time = clock_time + timedelta(minutes=3)
    assert lease.is_expired(clock_time) is True

    # Reclaim expired leases
    reclaimed = queue.reclaim_expired()
    assert reclaimed == 1

    # Attempting to ack expired lease raises JobQueueError
    with pytest.raises(JobQueueError):
        queue.ack(lease)


def test_in_memory_job_queue_kind_filtering_and_nack() -> None:
    clock_time = FIXED_NOW
    queue = InMemoryJobQueue(clock=lambda: clock_time)

    queue.enqueue_new("script.generate", "key-script-1")
    queue.enqueue_new("render.video", "key-render-1")

    # Dequeue only render.video
    lease_render = queue.dequeue("worker-render", kinds=["render.video"])
    assert lease_render is not None
    assert lease_render.job.kind == "render.video"

    # Nack with failure
    failed_job = queue.nack(lease_render, "FFmpeg container missing track")
    assert failed_job.status is AIJobStatus.FAILED
    assert failed_job.last_error == "FFmpeg container missing track"

    # The script job remains in the queue
    assert queue.size() == 1
    lease_script = queue.dequeue("worker-script")
    assert lease_script is not None
    assert lease_script.job.kind == "script.generate"
