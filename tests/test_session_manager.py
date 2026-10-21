"""Tests for SessionManager (Prompt Pack v8, task R-209)."""

from datetime import UTC, datetime, timedelta
import uuid

import pytest

from ai_youtube_agent.content.channel import Channel, ChannelStatus, YouTubeIdentifiers
from ai_youtube_agent.content.strategy import StrategyProfile
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.content_item import ContentItem, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
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

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "alice")
SYSTEM = Actor(ActorKind.SYSTEM, "scheduler")


def _fixed_clock(dt: datetime):
    return lambda: dt


@pytest.fixture
def audit_log():
    return AuditLog(InMemoryAuditSink())


def test_start_session_by_user_succeeds(database: Database, audit_log: AuditLog) -> None:
    manager = SessionManager(database, audit_log, clock=_fixed_clock(T0))
    session = manager.start_session(USER)

    assert session.is_active is True
    assert session.started_by == USER
    assert session.started_at == T0

    with database.connection() as conn:
        persisted = SessionRepository(conn).get(session.id)
        assert persisted is not None
        assert persisted.status is SessionStatus.ACTIVE


def test_start_session_by_non_user_fails(database: Database, audit_log: AuditLog) -> None:
    manager = SessionManager(database, audit_log, clock=_fixed_clock(T0))
    with pytest.raises(SessionNotAllowedError, match="Only a user"):
        manager.start_session(SYSTEM)


def test_start_session_conflict_detection(database: Database, audit_log: AuditLog) -> None:
    manager = SessionManager(database, audit_log, clock=_fixed_clock(T0))
    session1 = manager.start_session(USER)

    with pytest.raises(SessionConflictError, match="An active session"):
        manager.start_session(USER)

    session2 = manager.start_session(USER, auto_stop_existing=True)
    assert session2.id != session1.id
    assert session2.is_active is True

    s1 = manager.get_session(session1.id)
    assert s1 is not None
    assert s1.status is SessionStatus.STOPPED


def test_stop_session(database: Database, audit_log: AuditLog) -> None:
    t_stop = T0 + timedelta(minutes=15)
    manager = SessionManager(database, audit_log, clock=_fixed_clock(T0))
    session = manager.start_session(USER)

    manager._clock = _fixed_clock(t_stop)
    stopped = manager.stop_session(session.id, actor=USER, reason="Work complete")

    assert stopped.is_active is False
    assert stopped.status is SessionStatus.STOPPED
    assert stopped.stopped_at == t_stop


def test_stop_session_idempotent(database: Database, audit_log: AuditLog) -> None:
    manager = SessionManager(database, audit_log, clock=_fixed_clock(T0))
    session = manager.start_session(USER)
    stopped1 = manager.stop_session(session.id, actor=USER)
    stopped2 = manager.stop_session(session.id, actor=USER)

    assert stopped1.status is SessionStatus.STOPPED
    assert stopped2.status is SessionStatus.STOPPED


def test_get_active_session(database: Database, audit_log: AuditLog) -> None:
    manager = SessionManager(database, audit_log, clock=_fixed_clock(T0))
    assert manager.get_active_session() is None

    session = manager.start_session(USER)
    active = manager.get_active_session()
    assert active is not None
    assert active.id == session.id

    manager.stop_session(session.id, actor=USER)
    assert manager.get_active_session() is None


def test_session_summary_and_jobs(database: Database, audit_log: AuditLog) -> None:
    manager = SessionManager(database, audit_log, clock=_fixed_clock(T0))
    session = manager.start_session(USER)

    channel_id = uuid.uuid4().hex
    channel = Channel(
        id=channel_id,
        title="Session Test Channel",
        youtube=YouTubeIdentifiers(channel_id=f"UC{channel_id[:22].ljust(22, 'x')}"),
        status=ChannelStatus.ACTIVE,
        created_at=T0,
        updated_at=T0,
    )
    strategy = StrategyProfile.create(
        channel_id=channel_id,
        actor=USER,
        clock=_fixed_clock(T0),
    )
    item = ContentItem.create(
        channel_id=channel.id,
        strategy_profile_id=strategy.id,
        strategy_version=strategy.version,
        content_type=ContentType.SHORTS,
        title="Video",
        clock=_fixed_clock(T0),
    )
    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(strategy)
        ContentItemRepository(conn).add(item)
        job1 = (
            AIJob.create(
                "script.generate",
                "key-1",
                content_item_id=item.id,
                session_id=session.id,
                clock=_fixed_clock(T0),
            )
            .start(clock=_fixed_clock(T0))
            .succeed(clock=_fixed_clock(T0))
        )
        job2 = (
            AIJob.create(
                "audio.generate",
                "key-2",
                content_item_id=item.id,
                session_id=session.id,
                clock=_fixed_clock(T0),
            )
            .start(clock=_fixed_clock(T0))
            .fail("Audio failure", clock=_fixed_clock(T0))
        )
        AIJobRepository(conn).add(job1)
        AIJobRepository(conn).add(job2)

    jobs = manager.get_session_jobs(session.id)
    assert len(jobs) == 2

    summary = manager.get_session_summary(session.id)
    assert summary.total_jobs == 2
    assert summary.succeeded_jobs == 1
    assert summary.failed_jobs == 1
