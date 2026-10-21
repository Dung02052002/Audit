"""Tests for SessionManager (Prompt Pack v8, task R-209)."""

from datetime import UTC, datetime, timedelta

import pytest

from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
)
from ai_youtube_agent.core.content_item import ContentItem, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository, SessionRepository
from ai_youtube_agent.pipeline.job import (
    AIJob,
    SessionNotAllowedError,
    SessionStatus,
)
from ai_youtube_agent.pipeline.session_manager import (
    SessionConflictError,
    SessionManager,
    SessionNotFoundError,
)
from ai_youtube_agent.pipeline.stages import PipelineStage

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "alice")
SYSTEM = Actor(ActorKind.SYSTEM, "scheduler")


def _fixed_clock(dt: datetime):
    return lambda: dt


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    database.run_migrations()
    return database


@pytest.fixture
def audit_log(db):
    return AuditLog(db)


def test_start_session_by_user_succeeds(db, audit_log):
    manager = SessionManager(db, audit_log, clock=_fixed_clock(T0))
    session = manager.start_session(USER)

    assert session.is_active is True
    assert session.started_by == USER
    assert session.started_at == T0

    with db.transaction() as conn:
        persisted = SessionRepository(conn).get(session.id)
        assert persisted is not None
        assert persisted.status == SessionStatus.ACTIVE


def test_start_session_by_non_user_fails(db, audit_log):
    manager = SessionManager(db, audit_log, clock=_fixed_clock(T0))
    with pytest.raises(SessionNotAllowedError, match="Only a user"):
        manager.start_session(SYSTEM)


def test_start_session_conflict_detection(db, audit_log):
    manager = SessionManager(db, audit_log, clock=_fixed_clock(T0))
    session1 = manager.start_session(USER)

    with pytest.raises(SessionConflictError, match="An active session"):
        manager.start_session(USER)

    # With auto_stop_existing=True, session1 should be stopped and new one created
    session2 = manager.start_session(USER, auto_stop_existing=True)
    assert session2.id != session1.id
    assert session2.is_active is True

    s1 = manager.get_session(session1.id)
    assert s1 is not None
    assert s1.status == SessionStatus.STOPPED


def test_stop_session(db, audit_log):
    t_stop = T0 + timedelta(minutes=15)
    manager = SessionManager(db, audit_log, clock=_fixed_clock(T0))
    session = manager.start_session(USER)

    manager._clock = _fixed_clock(t_stop)
    stopped = manager.stop_session(session.id, actor=USER, reason="Work complete")

    assert stopped.is_active is False
    assert stopped.status == SessionStatus.STOPPED
    assert stopped.stopped_at == t_stop


def test_stop_session_idempotent(db, audit_log):
    manager = SessionManager(db, audit_log, clock=_fixed_clock(T0))
    session = manager.start_session(USER)
    stopped1 = manager.stop_session(session.id, actor=USER)
    stopped2 = manager.stop_session(session.id, actor=USER)

    assert stopped1.status == SessionStatus.STOPPED
    assert stopped2.status == SessionStatus.STOPPED


def test_get_active_session(db, audit_log):
    manager = SessionManager(db, audit_log, clock=_fixed_clock(T0))
    assert manager.get_active_session() is None

    session = manager.start_session(USER)
    active = manager.get_active_session()
    assert active is not None
    assert active.id == session.id

    manager.stop_session(session.id, actor=USER)
    assert manager.get_active_session() is None


def test_session_summary_and_jobs(db, audit_log):
    manager = SessionManager(db, audit_log, clock=_fixed_clock(T0))
    session = manager.start_session(USER)

    # Create content item and jobs within this session
    item = ContentItem.create(title="Video", content_type=ContentType.SHORTS, clock=_fixed_clock(T0))
    with db.transaction() as conn:
        ContentItemRepository(conn).add(item)
        job1 = AIJob.create(
            content_item_id=item.id,
            pipeline_stage=PipelineStage.SCRIPT,
            session_id=session.id,
            clock=_fixed_clock(T0),
        ).start(clock=_fixed_clock(T0)).succeed(clock=_fixed_clock(T0))
        job2 = AIJob.create(
            content_item_id=item.id,
            pipeline_stage=PipelineStage.VOICE,
            session_id=session.id,
            clock=_fixed_clock(T0),
        ).start(clock=_fixed_clock(T0)).fail("Audio failure", clock=_fixed_clock(T0))
        AIJobRepository(conn).add(job1)
        AIJobRepository(conn).add(job2)

    jobs = manager.get_session_jobs(session.id)
    assert len(jobs) == 2

    summary = manager.get_session_summary(session.id)
    assert summary.total_jobs == 2
    assert summary.succeeded_jobs == 1
    assert summary.failed_jobs == 1
