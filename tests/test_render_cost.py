"""Tests for video rendering cost recorder (O-177, prompt #177)."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.cost import CostCategory
from ai_youtube_agent.content.cost_ledger import COST_RECORDED, CostLedger
from ai_youtube_agent.content.render_cost import (
    RenderCostError,
    RenderCostErrorCode,
    RenderCostRecorder,
    RenderPricingPolicy,
)
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.providers.video_render import MP4_MEDIA_TYPE, RenderedVideo
from factories import (
    make_artifact,
    make_channel,
    make_content_item,
    make_strategy_profile,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


class TestRenderCostRecorder:
    def test_records_hd_and_uhd_render_costs_and_replays_idempotently(
        self, database: Database
    ) -> None:
        channel = make_channel(status=ChannelStatus.ACTIVE)
        profile = make_strategy_profile(channel)
        item = make_content_item(channel, profile)
        with database.transaction() as connection:
            ChannelRepository(connection).add(channel)
            StrategyProfileRepository(connection).add(profile)
            ContentItemRepository(connection).add(item)

        sink = InMemoryAuditSink()
        ledger = CostLedger(database, AuditLog(sink), clock=lambda: NOW)
        policy = RenderPricingPolicy(
            price_per_minute=Decimal("0.120"),
            currency="USD",
            uhd_multiplier=Decimal("2.0"),
        )
        recorder = RenderCostRecorder(ledger, policy=policy)

        video_art = make_artifact(item, ArtifactKind.VIDEO)
        rendered_hd = RenderedVideo(
            data=b"fake_mp4_bytes",
            media_type=MP4_MEDIA_TYPE,
            duration_ms=30_000,  # 0.5 minute -> 0.5 * 0.120 = 0.060000
            width=1080,
            height=1920,
            fps=30,
            provider="ffmpeg",
            generated_at=NOW,
        )
        entry = recorder.record_rendered_artifact(
            channel.id, video_art, rendered_hd
        )
        assert entry.created is True
        assert entry.cost.category is CostCategory.RENDER
        assert entry.cost.amount == Decimal("0.060000")
        assert entry.cost.ref == video_art.id
        assert sink.entries[-1].action == COST_RECORDED
        assert sink.entries[-1].metadata["duration_ms"] == 30_000

        # Idempotent replay on same video artifact
        replay = recorder.record_rendered_artifact(
            channel.id, video_art, rendered_hd
        )
        assert replay.created is False
        assert replay.cost.id == entry.cost.id

        # UHD render (3840x2160) applies 2.0x multiplier: 1 min * 0.120 * 2.0 = 0.240000
        uhd_entry = recorder.record_usage(
            channel.id,
            provider="ffmpeg",
            duration_ms=60_000,
            width=3840,
            height=2160,
            fps=60,
            content_item_id=item.id,
            ref="uhd_render:1",
        )
        assert uhd_entry.created is True
        assert uhd_entry.cost.amount == Decimal("0.240000")

    def test_refuses_non_video_artifact_and_invalid_telemetry(
        self, database: Database
    ) -> None:
        channel = make_channel(status=ChannelStatus.ACTIVE)
        profile = make_strategy_profile(channel)
        item = make_content_item(channel, profile)
        with database.transaction() as connection:
            ChannelRepository(connection).add(channel)
            StrategyProfileRepository(connection).add(profile)
            ContentItemRepository(connection).add(item)

        ledger = CostLedger(
            database, AuditLog(InMemoryAuditSink()), clock=lambda: NOW
        )
        recorder = RenderCostRecorder(ledger)

        audio_art = make_artifact(item, ArtifactKind.AUDIO)
        rendered = RenderedVideo(
            data=b"fake_mp4_bytes",
            media_type=MP4_MEDIA_TYPE,
            duration_ms=15_000,
            width=1080,
            height=1920,
            fps=30,
            provider="mock_render",
            generated_at=NOW,
        )
        with pytest.raises(RenderCostError) as exc_kind:
            recorder.record_rendered_artifact(channel.id, audio_art, rendered)
        assert (
            exc_kind.value.render_cost_code
            is RenderCostErrorCode.NOT_VIDEO_ARTIFACT
        )

        with pytest.raises(RenderCostError) as exc_tel:
            recorder.record_usage(
                channel.id,
                provider="mock_render",
                duration_ms=0,
            )
        assert (
            exc_tel.value.render_cost_code
            is RenderCostErrorCode.INVALID_TELEMETRY
        )
