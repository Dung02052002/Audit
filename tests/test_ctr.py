"""Tests for click-through rate (CTR) sync and extraction (N-165, prompt #165)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ai_youtube_agent.content.analytics import MetricScope, MetricSnapshot
from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.ctr import (
    CTR_SYNCED,
    CTRError,
    CTRErrorCode,
    CTRSync,
    extract_ctr,
)
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.content_item import ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.publish import PublishJobRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.pipeline.publish import PublishJob
from ai_youtube_agent.pipeline.publish_result import PublishResultService
from ai_youtube_agent.providers.mock_analytics import MockAnalyticsProvider
from ai_youtube_agent.providers.mock_youtube_oauth import MockYouTubeOAuthProvider
from factories import (
    USER,
    make_approval_request,
    make_artifact,
    make_channel,
    make_content_item,
    make_strategy_profile,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
START = datetime(2026, 10, 1, tzinfo=UTC)
END = datetime(2026, 10, 15, tzinfo=UTC)
PUBLISHED = datetime(2026, 10, 5, tzinfo=UTC)


def _vid(n: int) -> str:
    return f"vid{n:06d}ab"


def _publish(database: Database, channel, profile, video_id: str) -> None:
    item = make_content_item(channel, profile, status=ContentStatus.PUBLISHED)
    video = make_artifact(item, ArtifactKind.VIDEO)
    approval = make_approval_request(item, [video]).approve(USER)
    job = (
        PublishJob.create(
            approval, ContentType.SHORTS, f"pub:{video_id}", clock=lambda: PUBLISHED
        )
        .start(clock=lambda: PUBLISHED)
        .succeed(video_id, clock=lambda: PUBLISHED)
    )
    with database.transaction() as connection:
        ContentItemRepository(connection).add(item)
        ArtifactRepository(connection).add(video)
        ApprovalRequestRepository(connection).add(approval)
        PublishJobRepository(connection).add(job)


def test_extract_ctr_never_fabricates_ctr_from_impressions_or_views() -> None:
    present = MetricSnapshot(
        id="s1",
        scope=MetricScope.VIDEO,
        subject_id=_vid(1),
        source="mock_analytics",
        period_start=START,
        period_end=END,
        retrieved_at=NOW,
        metrics={
            "impressions_click_through_rate": Decimal("5.75"),
            "impressions": Decimal("10000"),
        },
    )
    view = extract_ctr(present)
    assert view.available is True
    assert view.impressions_click_through_rate == Decimal("5.75")
    assert view.impressions == Decimal("10000")

    only_impressions = MetricSnapshot(
        id="s2",
        scope=MetricScope.VIDEO,
        subject_id=_vid(1),
        source="mock_analytics",
        period_start=START,
        period_end=END,
        retrieved_at=NOW,
        metrics={"impressions": Decimal("10000"), "views": Decimal("500")},
    )
    missing_view = extract_ctr(only_impressions)
    assert missing_view.available is False
    assert missing_view.impressions_click_through_rate is None
    assert missing_view.impressions == Decimal("10000")


def test_extract_ctr_refuses_out_of_range_percentage_or_negative_impressions() -> None:
    for bad_metrics in (
        {"impressions_click_through_rate": Decimal("-0.01")},
        {"impressions_click_through_rate": Decimal("100.01")},
        {"impressions": Decimal("-1")},
    ):
        snap = MetricSnapshot(
            id="bad",
            scope=MetricScope.VIDEO,
            subject_id=_vid(1),
            source="mock_analytics",
            period_start=START,
            period_end=END,
            retrieved_at=NOW,
            metrics=bad_metrics,
        )
        with pytest.raises(CTRError) as caught:
            extract_ctr(snap)
        assert caught.value.ctr_code is CTRErrorCode.INVALID_CTR_VALUE


def test_sync_videos_and_channel_store_ctr_only_when_actually_available(
    database: Database,
) -> None:
    now = NOW
    channel = make_channel(status=ChannelStatus.ACTIVE)
    profile = make_strategy_profile(channel)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)
    for n in (1, 2, 3):
        _publish(database, channel, profile, _vid(n))

    provider = MockAnalyticsProvider(clock=lambda: now)
    provider.set_channel_metrics(channel.youtube.channel_id, {"views": Decimal("10")})
    # Video 1 has CTR + impressions; Video 2 has only impressions; Video 3 has nothing.
    provider.set_video_metrics(
        channel.youtube.channel_id,
        _vid(1),
        {
            "impressions_click_through_rate": Decimal("6.2"),
            "impressions": Decimal("5000"),
        },
    )
    provider.set_video_metrics(
        channel.youtube.channel_id,
        _vid(2),
        {"impressions": Decimal("1200")},
    )

    sink = InMemoryAuditSink()
    sync = CTRSync(
        database,
        provider,
        MockYouTubeOAuthProvider(clock=lambda: now),
        PublishResultService(database),
        AuditLog(sink),
        clock=lambda: now,
    )

    video_result = sync.sync_videos(
        channel.id, period_start=START, period_end=END, actor=USER
    )
    assert [o.youtube_video_id for o in video_result.outcomes] == [_vid(1)]
    assert video_result.unavailable_video_ids == (_vid(2), _vid(3))
    assert video_result.created_count == 1
    assert sync.for_video(_vid(2)) == []
    assert sync.for_video(_vid(3)) == []
    latest = sync.latest_for_video(_vid(1))
    assert latest is not None and latest.impressions_click_through_rate == Decimal(
        "6.2"
    )

    # Channel has no CTR seeded yet -> no channel snapshot stored.
    channel_empty = sync.sync_channel(
        channel.id, period_start=START, period_end=END, actor=USER
    )
    assert channel_empty.available is False
    assert channel_empty.snapshot is None
    assert sync.for_channel(channel.id) == []

    # Seed channel CTR and sync again.
    now = now + timedelta(minutes=5)
    provider.set_channel_metrics(
        channel.youtube.channel_id,
        {
            "impressions_click_through_rate": Decimal("4.8"),
            "impressions": Decimal("40000"),
        },
    )
    channel_ok = sync.sync_channel(
        channel.id, period_start=START, period_end=END, actor=USER
    )
    assert channel_ok.available is True
    assert channel_ok.created is True
    assert channel_ok.ctr is not None
    assert channel_ok.ctr.impressions_click_through_rate == Decimal("4.8")
    assert len(sync.for_channel(channel.id)) == 1
    assert sink.events()[-1].action == CTR_SYNCED
