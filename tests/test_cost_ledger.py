"""Tests for unified contribution cost ledger (O-174, prompt #174)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.cost import CostCategory
from ai_youtube_agent.content.cost_ledger import (
    COST_CORRECTION_RECORDED,
    COST_RECORDED,
    CostLedger,
    CostLedgerError,
    CostLedgerErrorCode,
)
from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from factories import (
    USER,
    make_channel,
    make_content_item,
    make_strategy_profile,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


class TestCostLedger:
    def test_records_all_categories_and_summarizes_by_channel_and_item(
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

        e_llm = ledger.record(
            channel.id,
            CostCategory.LLM,
            provider="mock_llm",
            amount=Decimal("0.045"),
            currency="USD",
            content_item_id=item.id,
            ref="script:1",
        )
        e_tts = ledger.record(
            channel.id,
            CostCategory.TTS,
            provider="mock_tts",
            amount=Decimal("0.120"),
            currency="USD",
            content_item_id=item.id,
            ref="audio:1",
        )
        e_render = ledger.record(
            channel.id,
            CostCategory.RENDER,
            provider="ffmpeg",
            amount=Decimal("0.250"),
            currency="USD",
            content_item_id=item.id,
            ref="video:1",
        )
        e_storage = ledger.record(
            channel.id,
            CostCategory.STORAGE,
            provider="local_storage",
            amount=Decimal("0.010"),
            currency="USD",
            ref="storage:oct",
        )
        e_api = ledger.record(
            channel.id,
            CostCategory.API,
            provider="youtube_data_api",
            amount=Decimal("0.005"),
            currency="USD",
            ref="api:batch1",
        )
        e_prod = ledger.record(
            channel.id,
            CostCategory.PRODUCTION,
            provider="licensed_music",
            amount=Decimal("1.500"),
            currency="USD",
            content_item_id=item.id,
            ref="asset:music1",
        )
        assert all(
            entry.created
            for entry in (e_llm, e_tts, e_render, e_storage, e_api, e_prod)
        )
        assert sink.entries[-1].action == COST_RECORDED

        # Idempotent replay on same (category, ref)
        replay = ledger.record(
            channel.id,
            CostCategory.LLM,
            provider="mock_llm",
            amount=Decimal("9.999"),
            currency="USD",
            content_item_id=item.id,
            ref="script:1",
        )
        assert replay.created is False
        assert replay.cost.id == e_llm.cost.id
        assert replay.cost.amount == Decimal("0.045000")

        # Channel summary includes all 6 records
        summary = ledger.summarize(channel.id)
        assert summary.currency == "USD"
        assert summary.total_amount == Decimal("1.930000")
        assert summary.by_category == {
            "llm": Decimal("0.045000"),
            "tts": Decimal("0.120000"),
            "render": Decimal("0.250000"),
            "storage": Decimal("0.010000"),
            "api": Decimal("0.005000"),
            "production": Decimal("1.500000"),
        }

        # Item summary includes only the 4 item-scoped costs
        item_summary = ledger.summarize(channel.id, content_item_id=item.id)
        assert item_summary.total_amount == Decimal("1.915000")
        assert len(ledger.list_for_content_item(item.id)) == 4

    def test_append_only_correction_and_idempotency(self, database: Database) -> None:
        channel = make_channel(status=ChannelStatus.ACTIVE)
        profile = make_strategy_profile(channel)
        with database.transaction() as connection:
            ChannelRepository(connection).add(channel)
            StrategyProfileRepository(connection).add(profile)

        sink = InMemoryAuditSink()
        ledger = CostLedger(database, AuditLog(sink), clock=lambda: NOW)
        orig = ledger.record(
            channel.id,
            CostCategory.RENDER,
            provider="ffmpeg",
            amount=Decimal("0.30"),
            currency="USD",
            ref="render:job1",
        )
        corr = ledger.record_correction(
            orig.cost.id,
            additional_amount=Decimal("0.05"),
            actor=USER,
        )
        assert corr.created is True
        assert corr.cost.ref == f"correction:{orig.cost.id}"
        assert sink.entries[-1].action == COST_CORRECTION_RECORDED

        replay_corr = ledger.record_correction(
            orig.cost.id,
            additional_amount=Decimal("0.05"),
            actor=USER,
        )
        assert replay_corr.created is False
        assert replay_corr.cost.id == corr.cost.id
        assert ledger.summarize(channel.id).total_amount == Decimal("0.350000")

    def test_enforces_currency_and_channel_ownership_guards(
        self, database: Database
    ) -> None:
        channel = make_channel(status=ChannelStatus.ACTIVE)
        other_channel = make_channel(status=ChannelStatus.ACTIVE)
        profile = make_strategy_profile(channel)
        other_profile = make_strategy_profile(other_channel)
        other_item = make_content_item(other_channel, other_profile)
        with database.transaction() as connection:
            ChannelRepository(connection).add(channel)
            ChannelRepository(connection).add(other_channel)
            StrategyProfileRepository(connection).add(profile)
            StrategyProfileRepository(connection).add(other_profile)
            ContentItemRepository(connection).add(other_item)

        ledger = CostLedger(database, AuditLog(InMemoryAuditSink()), clock=lambda: NOW)

        with pytest.raises(CostLedgerError) as exc_curr:
            ledger.record(
                channel.id,
                CostCategory.API,
                provider="ext_api",
                amount=Decimal("1.00"),
                currency="EUR",
            )
        assert exc_curr.value.ledger_code is CostLedgerErrorCode.CURRENCY_MISMATCH

        with pytest.raises(CostLedgerError) as exc_ch:
            ledger.record(
                channel.id,
                CostCategory.LLM,
                provider="mock_llm",
                amount=Decimal("0.10"),
                currency="USD",
                content_item_id=other_item.id,
            )
        assert exc_ch.value.ledger_code is CostLedgerErrorCode.CHANNEL_MISMATCH

        with pytest.raises(CostLedgerError) as exc_range:
            ledger.list_for_channel(channel.id, start=NOW, end=NOW - timedelta(hours=1))
        assert exc_range.value.ledger_code is CostLedgerErrorCode.INVALID_RANGE
