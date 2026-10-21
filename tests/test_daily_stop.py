"""Tests for DailyStopService (Prompt Pack v8, task R-210)."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ai_youtube_agent.content.channel import Channel, ChannelStatus
from ai_youtube_agent.content.strategy import (
    AgeRange,
    AspectRatio,
    Audience,
    AudienceLevel,
    Brand,
    BrandVisual,
    Budget,
    Cadence,
    FormatSettings,
    LanguageSettings,
    LongFormFormat,
    Market,
    Monetization,
    Niche,
    Pillar,
    PublishSchedule,
    Resolution,
    RevenueGoal,
    RevenueSource,
    ShortsFormat,
    StrategyProfile,
    Weekday,
)
from ai_youtube_agent.core.audit import AuditLog
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


def _make_strategy(channel_id: str, shorts_per_day: int = 3, longform_per_day: int = 1) -> StrategyProfile:
    return StrategyProfile(
        channel_id=channel_id,
        cadence=Cadence(
            shorts_per_day=shorts_per_day,
            longform_per_day=longform_per_day,
            time_zone="UTC",
        ),
        budget=Budget(Decimal("500.00"), "USD"),
        monetization=Monetization((RevenueSource.ADS,), RevenueGoal.GROWTH),
        brand=Brand("TestBrand", BrandVisual("dark", "#000000", "#FFFFFF", "Roboto")),
        market=Market(Niche.TECH, (Pillar("AI"),), Audience(AgeRange.ADULTS, AudienceLevel.BEGINNER), LanguageSettings("en")),
        format=FormatSettings(
            ShortsFormat(60, AspectRatio.PORTRAIT, Resolution.HD_1080P),
            LongFormFormat(600, AspectRatio.LANDSCAPE, Resolution.HD_1080P),
        ),
        schedule=PublishSchedule((Weekday.MONDAY,), "18:00"),
    )


@pytest.fixture
def sample_channel(db):
    channel = Channel.create(
        title="Tech Channel",
        handle="@techchannel",
        status=ChannelStatus.ACTIVE,
        clock=_fixed_clock(T0),
    )
    with db.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(_make_strategy(channel.id, shorts_per_day=2, longform_per_day=1))
    return channel


def test_check_channel_limit_within_limit(db, audit_log, sample_channel):
    service = DailyStopService(db, audit_log, clock=_fixed_clock(T0))
    eval_res = service.check_channel_limit(sample_channel.id, ContentType.SHORTS)

    assert eval_res.is_stopped is False
    assert eval_res.used_today == 0
    assert eval_res.daily_limit == 2
    assert eval_res.remaining_today == 2
    assert service.should_stop(sample_channel.id, ContentType.SHORTS) is False


def test_check_channel_limit_reached(db, audit_log, sample_channel):
    # Log 2 production starts
    item1 = ContentItem.create(channel_id=sample_channel.id, title="Item 1", content_type=ContentType.SHORTS, clock=_fixed_clock(T0))
    item2 = ContentItem.create(channel_id=sample_channel.id, title="Item 2", content_type=ContentType.SHORTS, clock=_fixed_clock(T0))
    with db.transaction() as conn:
        ContentItemRepository(conn).add(item1)
        ContentItemRepository(conn).add(item2)
        start_repo = ProductionStartRepository(conn)
        start_repo.add(ProductionStart.record(sample_channel.id, item1.id, ContentType.SHORTS, clock=_fixed_clock(T0)))
        start_repo.add(ProductionStart.record(sample_channel.id, item2.id, ContentType.SHORTS, clock=_fixed_clock(T0)))

    service = DailyStopService(db, audit_log, clock=_fixed_clock(T0))
    eval_res = service.check_channel_limit(sample_channel.id, ContentType.SHORTS)

    assert eval_res.is_stopped is True
    assert eval_res.used_today == 2
    assert eval_res.daily_limit == 2
    assert eval_res.remaining_today == 0
    assert "Daily shorts production limit reached" in (eval_res.reason or "")
    assert service.should_stop(sample_channel.id, ContentType.SHORTS) is True


def test_check_channel_without_strategy(db, audit_log):
    channel = Channel.create(
        title="Unconfigured Channel",
        handle="@unconfigured",
        status=ChannelStatus.ACTIVE,
        clock=_fixed_clock(T0),
    )
    with db.transaction() as conn:
        ChannelRepository(conn).add(channel)

    service = DailyStopService(db, audit_log, clock=_fixed_clock(T0))
    eval_res = service.check_channel_limit(channel.id, ContentType.SHORTS)

    assert eval_res.is_stopped is True
    assert "no strategy" in (eval_res.reason or "").lower()


def test_check_channel_summary(db, audit_log, sample_channel):
    service = DailyStopService(db, audit_log, clock=_fixed_clock(T0))
    summary = service.check_channel(sample_channel.id)

    assert summary.channel_id == sample_channel.id
    assert summary.channel_title == sample_channel.title
    assert summary.shorts_evaluation.daily_limit == 2
    assert summary.longform_evaluation.daily_limit == 1
    assert summary.is_any_stopped is False
    assert summary.is_all_stopped is False


def test_check_all_channels(db, audit_log, sample_channel):
    service = DailyStopService(db, audit_log, clock=_fixed_clock(T0))
    summaries = service.check_all_channels()

    assert len(summaries) == 1
    assert summaries[0].channel_id == sample_channel.id


def test_record_stop_event(db, audit_log, sample_channel):
    service = DailyStopService(db, audit_log, clock=_fixed_clock(T0))
    eval_res = service.check_channel_limit(sample_channel.id, ContentType.SHORTS)
    service.record_stop_event(sample_channel.id, ContentType.SHORTS, eval_res)
