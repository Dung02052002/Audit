"""Tests for storage and external API cost recorder (O-178, prompt #178)."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.cost import CostCategory
from ai_youtube_agent.content.cost_ledger import CostLedger
from ai_youtube_agent.content.storage_api_cost import (
    BYTES_PER_GIB,
    ApiPricingPolicy,
    StorageApiCostError,
    StorageApiCostErrorCode,
    StorageApiCostRecorder,
    StoragePricingPolicy,
)
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from factories import (
    make_artifact,
    make_channel,
    make_content_item,
    make_strategy_profile,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


class TestStorageApiCostRecorder:
    def test_records_storage_and_api_costs_with_operation_rates_and_idempotency(
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
        recorder = StorageApiCostRecorder(
            ledger,
            storage_policy=StoragePricingPolicy(
                price_per_gb=Decimal("0.040"),
                currency="USD",
            ),
            api_policy=ApiPricingPolicy(
                price_per_1k_requests=Decimal("0.500"),
                currency="USD",
                operation_rates={"youtube.upload": Decimal("2.000")},
            ),
        )

        # 0.5 GiB storage -> 0.5 * 0.040 = 0.020000
        half_gib = int(BYTES_PER_GIB) // 2
        video_art = make_artifact(
            item, ArtifactKind.VIDEO, size_bytes=half_gib
        )
        st_entry = recorder.record_artifact_storage(channel.id, video_art)
        assert st_entry.created is True
        assert st_entry.cost.category is CostCategory.STORAGE
        assert st_entry.cost.amount == Decimal("0.020000")
        assert st_entry.cost.ref == f"storage:{video_art.id}"

        # Replay artifact storage
        replay_st = recorder.record_artifact_storage(channel.id, video_art)
        assert replay_st.created is False
        assert replay_st.cost.id == st_entry.cost.id

        # API call with default rate: 200 requests * 0.500 / 1000 = 0.100000
        api_default = recorder.record_api_calls(
            channel.id,
            provider="research_api",
            requests=200,
            operation="research.search",
            content_item_id=item.id,
            ref="api:search:1",
        )
        assert api_default.created is True
        assert api_default.cost.category is CostCategory.API
        assert api_default.cost.amount == Decimal("0.100000")

        # API call with operation-specific rate: 10 requests * 2.000 / 1000 = 0.020000
        api_upload = recorder.record_api_calls(
            channel.id,
            provider="youtube_data_api",
            requests=10,
            operation="youtube.upload",
            content_item_id=item.id,
            ref="api:upload:1",
        )
        assert api_upload.created is True
        assert api_upload.cost.amount == Decimal("0.020000")
        assert sink.entries[-1].metadata["operation"] == "youtube.upload"

    def test_refuses_invalid_bytes_requests_and_blank_operation(
        self, database: Database
    ) -> None:
        channel = make_channel(status=ChannelStatus.ACTIVE)
        profile = make_strategy_profile(channel)
        with database.transaction() as connection:
            ChannelRepository(connection).add(channel)
            StrategyProfileRepository(connection).add(profile)

        ledger = CostLedger(
            database, AuditLog(InMemoryAuditSink()), clock=lambda: NOW
        )
        recorder = StorageApiCostRecorder(ledger)

        with pytest.raises(StorageApiCostError) as exc_bytes:
            recorder.record_storage_bytes(
                channel.id, provider="local_storage", size_bytes=0
            )
        assert (
            exc_bytes.value.cost_code
            is StorageApiCostErrorCode.INVALID_STORAGE_BYTES
        )

        with pytest.raises(StorageApiCostError) as exc_req:
            recorder.record_api_calls(
                channel.id, provider="ext_api", requests=0
            )
        assert (
            exc_req.value.cost_code
            is StorageApiCostErrorCode.INVALID_API_REQUESTS
        )

        with pytest.raises(StorageApiCostError) as exc_op:
            recorder.record_api_calls(
                channel.id, provider="ext_api", requests=1, operation="   "
            )
        assert (
            exc_op.value.cost_code
            is StorageApiCostErrorCode.INVALID_OPERATION
        )
