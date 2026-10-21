"""Tests for DailyStopService (Prompt Pack v8, task R-210)."""

from datetime import UTC, datetime
import uuid

import pytest

from ai_youtube_agent.content.channel import Channel, ChannelStatus, YouTubeIdentifiers
from ai_youtube_agent.content.strategy import (
    Cadence,
    StrategyProfile,
)
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
from ai_youtube_agent.core.db.repositories.usage import ProductionStartRepository
from ai_youtube_agent.core.production_start import ProductionStart
from ai_youtube_agent.pipeline.daily_stop import DailyStopService

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner")


def _fixed_clock(dt: datetime):
    return lambda: dt


@pytest.fixture
def audit_log():
    return AuditLog(InMemoryAuditSink())


@pytest.fixture
def sample_channel(database: Database) -> Channel:
    channel_id = uuid.uuid4().hex
    channel = Channel(
        id=channel_id,
        title="Tech Channel",
        youtube=YouTubeIdentifiers(
            channel_id=f"UC{channel_id[:22].ljust(22, 'x')}",
            handle="@techchannel",
        ),
        status=ChannelStatus.ACTIVE,
        created_at=T0,
        updated_at=T0,
    )
    strategy = StrategyProfile.create(
        channel_id=channel.id,
        actor=USER,
        clock=_fixed_clock(T0),
    ).update(
        cadence=Cadence(shorts_per_day=2, longform_per_day=1, time_zone="UTC"),
        actor=USER,
        clock=_fixed_clock(T0),
    )
    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(strategy)
    return channel


def test_check_channel_limit_within_limit(
    database: Database, audit_log: AuditLog, sample_channel: Channel
) -> None:
    service = DailyStopService(database, audit_log, clock=_fixed_clock(T0))
    eval_res = service.check_channel_limit(sample_channel.id, ContentType.SHORTS)

    assert eval_res.is_stopped is False
    assert eval_res.used_today == 0
    assert eval_res.daily_limit == 2
    assert eval_res.remaining_today == 2
    assert service.should_stop(sample_channel.id, ContentType.SHORTS) is False


def test_check_channel_limit_reached(
    database: Database, audit_log: AuditLog, sample_channel: Channel
) -> None:
    item1 = ContentItem.create(
        channel_id=sample_channel.id,
        strategy_profile_id=sample_channel.id,
        strategy_version=1,
        content_type=ContentType.SHORTS,
        title="Item 1",
        clock=_fixed_clock(T0),
    )
    item2 = ContentItem.create(
        channel_id=sample_channel.id,
        strategy_profile_id=sample_channel.id,
        strategy_version=1,
        content_type=ContentType.SHORTS,
        title="Item 2",
        clock=_fixed_clock(T0),
    )
    with database.transaction() as conn:
        ContentItemRepository(conn).add(item1)
        ContentItemRepository(conn).add(item2)
        start_repo = ProductionStartRepository(conn)
        start_repo.add(ProductionStart.of(item1, T0))
        start_repo.add(ProductionStart.of(item2, T0))

    service = DailyStopService(database, audit_log, clock=_fixed_clock(T0))
    eval_res = service.check_channel_limit(sample_channel.id, ContentType.SHORTS)

    assert eval_res.is_stopped is True
    assert eval_res.used_today == 2
    assert eval_res.daily_limit == 2
    assert eval_res.remaining_today == 0
    assert "Daily shorts production limit reached" in (eval_res.reason or "")
    assert service.should_stop(sample_channel.id, ContentType.SHORTS) is True


def test_check_channel_without_strategy(
    database: Database, audit_log: AuditLog
) -> None:
    channel_id = uuid.uuid4().hex
    channel = Channel(
        id=channel_id,
        title="Unconfigured Channel",
        youtube=YouTubeIdentifiers(channel_id=f"UC{channel_id[:22].ljust(22, 'x')}"),
        status=ChannelStatus.ACTIVE,
        created_at=T0,
        updated_at=T0,
    )
    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)

    service = DailyStopService(database, audit_log, clock=_fixed_clock(T0))
    eval_res = service.check_channel_limit(channel.id, ContentType.SHORTS)

    assert eval_res.is_stopped is True
    assert "no strategy" in (eval_res.reason or "").lower()


def test_check_channel_summary(
    database: Database, audit_log: AuditLog, sample_channel: Channel
) -> None:
    service = DailyStopService(database, audit_log, clock=_fixed_clock(T0))
    summary = service.check_channel(sample_channel.id)

    assert summary.channel_id == sample_channel.id
    assert summary.channel_title == sample_channel.title
    assert summary.shorts_evaluation.daily_limit == 2
    assert summary.longform_evaluation.daily_limit == 1
    assert summary.is_any_stopped is False
    assert summary.is_all_stopped is False


def test_check_all_channels(
    database: Database, audit_log: AuditLog, sample_channel: Channel
) -> None:
    service = DailyStopService(database, audit_log, clock=_fixed_clock(T0))
    summaries = service.check_all_channels()

    assert len(summaries) == 1
    assert summaries[0].channel_id == sample_channel.id


def test_record_stop_event(
    database: Database, audit_log: AuditLog, sample_channel: Channel
) -> None:
    service = DailyStopService(database, audit_log, clock=_fixed_clock(T0))
    eval_res = service.check_channel_limit(sample_channel.id, ContentType.SHORTS)
    service.record_stop_event(sample_channel.id, ContentType.SHORTS, eval_res)
