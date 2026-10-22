"""Tests for LLM usage cost recorder (O-175, prompt #175)."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.cost import CostCategory
from ai_youtube_agent.content.cost_ledger import (
    COST_RECORDED,
    CostLedger,
    CostLedgerError,
    CostLedgerErrorCode,
)
from ai_youtube_agent.content.llm_cost import (
    LLMCostError,
    LLMCostErrorCode,
    LLMCostRecorder,
    LLMModelRate,
    LLMPricingCatalog,
)
from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.providers.text_generation import GeneratedText
from factories import (
    make_channel,
    make_content_item,
    make_strategy_profile,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


class TestLLMCostRecorder:
    def test_records_generated_text_cost_into_ledger_and_replays_idempotently(
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
        catalog = LLMPricingCatalog(
            rates={
                ("mock_llm", "gpt-test-1"): LLMModelRate(
                    input_price_per_1k=Decimal("0.010"),
                    output_price_per_1k=Decimal("0.030"),
                    currency="USD",
                )
            },
            default_rate=None,
        )
        recorder = LLMCostRecorder(ledger, catalog=catalog)

        generated = GeneratedText(
            texts=("Hook text candidate",),
            provider="mock_llm",
            model="gpt-test-1",
            generated_at=NOW,
            input_tokens=1500,
            output_tokens=500,
        )
        entry = recorder.record_generated(
            channel.id,
            generated,
            content_item_id=item.id,
            ref="hook:job1",
        )
        # (1500 * 0.010 + 500 * 0.030) / 1000 = 0.015 + 0.015 = 0.030000
        assert entry.created is True
        assert entry.cost.category is CostCategory.LLM
        assert entry.cost.amount == Decimal("0.030000")
        assert entry.cost.currency == "USD"
        assert entry.cost.ref == "hook:job1"
        assert sink.entries[-1].action == COST_RECORDED
        assert sink.entries[-1].metadata["model"] == "gpt-test-1"
        assert sink.entries[-1].metadata["input_tokens"] == 1500
        assert sink.entries[-1].metadata["output_tokens"] == 500

        # Replay with same ref is idempotent
        replay = recorder.record_generated(
            channel.id,
            generated,
            content_item_id=item.id,
            ref="hook:job1",
        )
        assert replay.created is False
        assert replay.cost.id == entry.cost.id

    def test_refuses_missing_token_counts_and_unknown_model_when_no_default(
        self, database: Database
    ) -> None:
        channel = make_channel(status=ChannelStatus.ACTIVE)
        profile = make_strategy_profile(channel)
        with database.transaction() as connection:
            ChannelRepository(connection).add(channel)
            StrategyProfileRepository(connection).add(profile)

        ledger = CostLedger(database, AuditLog(InMemoryAuditSink()), clock=lambda: NOW)
        catalog = LLMPricingCatalog(rates={}, default_rate=None)
        recorder = LLMCostRecorder(ledger, catalog=catalog)

        no_tokens = GeneratedText(
            texts=("Text without token telemetry",),
            provider="mock_llm",
            model="gpt-test-1",
            generated_at=NOW,
            input_tokens=None,
            output_tokens=None,
        )
        with pytest.raises(LLMCostError) as exc_missing:
            recorder.record_generated(channel.id, no_tokens, ref="missing:1")
        assert exc_missing.value.llm_cost_code is LLMCostErrorCode.MISSING_TOKEN_USAGE

        with pytest.raises(LLMCostError) as exc_unknown:
            recorder.record_usage(
                channel.id,
                provider="mock_llm",
                model="unconfigured-model",
                input_tokens=100,
                output_tokens=100,
            )
        assert exc_unknown.value.llm_cost_code is LLMCostErrorCode.UNKNOWN_MODEL_RATE

    def test_currency_mismatch_is_refused_by_ledger(self, database: Database) -> None:
        channel = make_channel(status=ChannelStatus.ACTIVE)
        profile = make_strategy_profile(channel)
        with database.transaction() as connection:
            ChannelRepository(connection).add(channel)
            StrategyProfileRepository(connection).add(profile)

        ledger = CostLedger(database, AuditLog(InMemoryAuditSink()), clock=lambda: NOW)
        catalog = LLMPricingCatalog(
            default_rate=LLMModelRate(
                input_price_per_1k=Decimal("0.01"),
                output_price_per_1k=Decimal("0.02"),
                currency="EUR",
            )
        )
        recorder = LLMCostRecorder(ledger, catalog=catalog)
        with pytest.raises(CostLedgerError) as exc_curr:
            recorder.record_usage(
                channel.id,
                provider="mock_llm",
                model="euro-model",
                input_tokens=1000,
                output_tokens=500,
            )
        assert exc_curr.value.ledger_code is CostLedgerErrorCode.CURRENCY_MISMATCH
