"""End-to-end Phase O economics scenarios (O-181, prompt #181).

Covers:
1. Strict separation between ``RevenueStage.ESTIMATED`` and ``RevenueStage.FINAL``
   across channel and multi-page per-video syncs, estimate revision history,
   final revenue freshness, and side-by-side variance comparison without mutation.
2. Multi-source revenue breakdown across built-in streams (ads, Shorts ads,
   memberships, sponsorships, affiliate, merchandise) and custom streams, plus
   non-guaranteed ``StrategyProfile.monetization`` goal progress.
3. End-to-end contribution cost ledger across LLM, TTS, render, storage, and API
   recorders, including idempotency by ``(category, ref)`` and append-only TTS
   rate corrections.
4. Channel and per-item contribution margin calculation, verifying ``metric_label ==
   "contribution_margin"``, ``is_net_profit is False``, single-stage isolation,
   and cross-currency guards across revenue, costs, and monetization goals.
5. Daily and monthly ``BudgetGuard`` enforcement in the channel strategy time zone,
   alert threshold progression, pre-execution projected cost blocking, and parity
   with ``BudgetGate``.
"""

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ai_youtube_agent.content.analytics import MetricScope
from ai_youtube_agent.content.audio_artifact import AudioArtifactRecorder
from ai_youtube_agent.content.audio_cost import AudioCostRecorder
from ai_youtube_agent.content.budget_guard import (
    BUDGET_ALERT_TRIGGERED,
    BUDGET_GUARD_BLOCKED,
    BudgetGuard,
    BudgetGuardBlockedError,
    BudgetGuardError,
    BudgetGuardErrorCode,
    BudgetWindowKind,
)
from ai_youtube_agent.content.channel import Channel, ChannelStatus
from ai_youtube_agent.content.channel_metrics import (
    MetricsSyncError,
    MetricsSyncErrorCode,
)
from ai_youtube_agent.content.contribution_margin import (
    METRIC_LABEL,
    NOT_NET_PROFIT_DISCLAIMER,
    ContributionMarginCalculator,
    ContributionMarginError,
    ContributionMarginErrorCode,
)
from ai_youtube_agent.content.cost import CostCategory, CostRecord
from ai_youtube_agent.content.cost_ledger import (
    CostLedger,
    CostLedgerError,
    CostLedgerErrorCode,
)
from ai_youtube_agent.content.estimated_revenue import (
    EstimatedRevenueError,
    EstimatedRevenueErrorCode,
    EstimatedRevenueSync,
)
from ai_youtube_agent.content.final_revenue import (
    FinalRevenueError,
    FinalRevenueErrorCode,
    FinalRevenueSync,
)
from ai_youtube_agent.content.llm_cost import (
    LLMCostRecorder,
    LLMModelRate,
    LLMPricingCatalog,
)
from ai_youtube_agent.content.render_cost import (
    RenderCostRecorder,
    RenderPricingPolicy,
)
from ai_youtube_agent.content.revenue import RevenueStage
from ai_youtube_agent.content.revenue_sources import (
    RevenueSourceError,
    RevenueSourceErrorCode,
    RevenueSourceService,
    RevenueStreamCategory,
    RevenueStreamRegistry,
    RevenueStreamSpec,
)
from ai_youtube_agent.content.storage_api_cost import (
    ApiPricingPolicy,
    StorageApiCostRecorder,
    StoragePricingPolicy,
)
from ai_youtube_agent.content.strategy import (
    Budget,
    Cadence,
    Monetization,
    RevenueGoal,
    RevenueSource,
    StrategyProfile,
)
from ai_youtube_agent.content.tts_cost import TTSCostService
from ai_youtube_agent.content.tts_job import TtsResult
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.artifact_storage import LocalFileStorage
from ai_youtube_agent.core.audit import AuditLog, AuditResult, InMemoryAuditSink
from ai_youtube_agent.core.budget_gate import BudgetGate
from ai_youtube_agent.core.content_item import ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
    VoiceProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
    ScriptRepository,
)
from ai_youtube_agent.core.db.repositories.economics import (
    CostRecordRepository,
    RevenueRecordRepository,
)
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.db.repositories.publish import PublishJobRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.core.gates import GateOutcome, TransitionContext
from ai_youtube_agent.pipeline.publish import PublishJob
from ai_youtube_agent.pipeline.publish_result import PublishResultService
from ai_youtube_agent.pipeline.upload_retry import UploadRetryPolicy
from ai_youtube_agent.providers.mock_revenue import MockRevenueProvider
from ai_youtube_agent.providers.mock_speech_synthesis import MockSpeechSynthesizer
from ai_youtube_agent.providers.mock_video_render import MockVideoRenderer
from ai_youtube_agent.providers.mock_youtube_oauth import MockYouTubeOAuthProvider
from ai_youtube_agent.providers.revenue import RevenueErrorCode
from ai_youtube_agent.providers.speech_synthesis import SpeechRequest
from ai_youtube_agent.providers.text_generation import GeneratedText
from ai_youtube_agent.providers.video_render import VideoRenderRequest
from ai_youtube_agent.providers.youtube_oauth import OAuthScope
from factories import (
    USER,
    make_ai_job,
    make_approval_request,
    make_artifact,
    make_channel,
    make_content_item,
    make_script,
    make_strategy_profile,
    make_voice_profile,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
PERIOD_START = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
PERIOD_END = datetime(2026, 10, 15, 0, 0, tzinfo=UTC)
PUBLISHED_AT = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
IN_WINDOW = datetime(2026, 10, 10, 14, 0, tzinfo=UTC)


def _vid(index: int) -> str:
    return f"ec{index:07d}xy"


def _publish_video(
    database: Database,
    channel: Channel,
    profile: StrategyProfile,
    youtube_video_id: str,
    *,
    content_type: ContentType = ContentType.SHORTS,
    published_at: datetime = PUBLISHED_AT,
):
    item = make_content_item(
        channel,
        profile,
        content_type=content_type,
        status=ContentStatus.PUBLISHED,
    )
    video = make_artifact(item, ArtifactKind.VIDEO)
    approval = make_approval_request(item, [video]).approve(USER)
    job = (
        PublishJob.create(
            approval,
            content_type,
            f"pub:{youtube_video_id}",
            clock=lambda: published_at,
        )
        .start(clock=lambda: published_at)
        .succeed(youtube_video_id, clock=lambda: published_at)
    )
    with database.transaction() as connection:
        ContentItemRepository(connection).add(item)
        ArtifactRepository(connection).add(video)
        ApprovalRequestRepository(connection).add(approval)
        PublishJobRepository(connection).add(job)
    return item, video


def test_estimated_and_final_revenue_lifecycle_across_channel_and_paginated_videos(
    database: Database,
) -> None:
    """Estimated and final revenue stay strictly separate across revisions, pagination, and freshness."""
    channel = make_channel(status=ChannelStatus.ACTIVE)
    profile = make_strategy_profile(channel)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)

    v1, _ = _publish_video(database, channel, profile, _vid(1))
    v2, _ = _publish_video(database, channel, profile, _vid(2))
    v3, _ = _publish_video(
        database,
        channel,
        profile,
        _vid(3),
        published_at=PERIOD_END + timedelta(days=1),
    )

    provider = MockRevenueProvider(
        data_lag=timedelta(hours=6), clock=lambda: NOW
    )
    oauth = MockYouTubeOAuthProvider(clock=lambda: NOW)
    oauth.seed_token(
        channel.id, scopes=frozenset({OAuthScope.YT_ANALYTICS_MONETARY_READONLY})
    )
    sink = InMemoryAuditSink()
    audit = AuditLog(sink, clock=lambda: NOW)
    results = PublishResultService(database, sink)
    fast_retry = UploadRetryPolicy(max_attempts=3, base_delay_seconds=1)

    est_sync = EstimatedRevenueSync(
        database,
        provider,
        oauth,
        audit,
        results=results,
        retry_policy=fast_retry,
        page_size=1,
        sleep=lambda _: None,
        clock=lambda: NOW,
    )
    fin_sync = FinalRevenueSync(
        database,
        provider,
        oauth,
        audit,
        results=results,
        retry_policy=fast_retry,
        page_size=1,
        max_age=timedelta(days=14),
        sleep=lambda _: None,
        clock=lambda: NOW,
    )

    # 1. Seed initial ESTIMATED channel and video revenue (v2 has no revenue -> never zero-filled)
    provider.set_channel_revenue(
        channel.youtube.channel_id,
        stage=RevenueStage.ESTIMATED,
        amounts={"ads": Decimal("120.50"), "shorts_ads": Decimal("34.25")},
    )
    provider.set_video_revenue(
        channel.youtube.channel_id,
        _vid(1),
        stage=RevenueStage.ESTIMATED,
        amounts={"shorts_ads": Decimal("34.25")},
    )

    # Inject a transient timeout to verify retryable recovery
    provider.fail_next(RevenueErrorCode.TIMEOUT, times=1)
    ch_est_1 = est_sync.sync_channel(
        channel.id,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        actor=USER,
        revenue_types=("ads", "shorts_ads", "memberships"),
    )
    assert ch_est_1.is_final is False
    assert ch_est_1.created_count == 2
    assert ch_est_1.unchanged_count == 0
    assert ch_est_1.missing_revenue_types == ("memberships",)
    assert ch_est_1.complete is True

    # Re-running with identical estimates is idempotent
    ch_est_repeat = est_sync.sync_channel(
        channel.id,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        actor=USER,
        revenue_types=("ads", "shorts_ads", "memberships"),
    )
    assert ch_est_repeat.created_count == 0
    assert ch_est_repeat.unchanged_count == 2

    # Revise the estimate for ads -> appends a new ESTIMATED record, preserving history
    provider.set_channel_revenue(
        channel.youtube.channel_id,
        stage=RevenueStage.ESTIMATED,
        amounts={"ads": Decimal("125.00"), "shorts_ads": Decimal("34.25")},
    )
    ch_est_revised = est_sync.sync_channel(
        channel.id,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        actor=USER,
        revenue_types=("ads", "shorts_ads"),
    )
    assert ch_est_revised.created_count == 1
    assert ch_est_revised.unchanged_count == 1
    assert len(est_sync.channel_records(channel.id, revenue_type="ads")) == 2

    # 2. Sync per-video ESTIMATED revenue across 2 pages (v3 published after period_end is skipped)
    vid_est = est_sync.sync_videos(
        channel.id,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        actor=USER,
        revenue_types=("shorts_ads",),
    )
    assert vid_est.pages == 2
    assert vid_est.requested_video_ids == (_vid(1), _vid(2))
    assert vid_est.skipped_video_ids == (_vid(3),)
    assert vid_est.no_data_video_ids == (_vid(2),)
    assert vid_est.created_count == 1
    assert est_sync.video_records(_vid(2)) == []

    # 3. Final revenue is empty until seeded; querying final does not return estimated rows
    assert fin_sync.channel_records(channel.id) == []
    assert fin_sync.video_records(_vid(1)) == []

    # Period rules: estimated refuses future period_start; final refuses unended period_end
    with pytest.raises(EstimatedRevenueError) as exc_future:
        est_sync.sync_channel(
            channel.id,
            period_start=NOW + timedelta(days=1),
            period_end=NOW + timedelta(days=2),
            actor=USER,
        )
    assert (
        exc_future.value.revenue_code
        is EstimatedRevenueErrorCode.PERIOD_NOT_STARTED
    )

    with pytest.raises(MetricsSyncError) as exc_unended:
        fin_sync.sync_channel(
            channel.id,
            period_start=PERIOD_START,
            period_end=NOW + timedelta(hours=1),
            actor=USER,
        )
    assert exc_unended.value.sync_code is MetricsSyncErrorCode.PERIOD_NOT_ENDED

    # 4. Seed FINAL revenue and sync alongside ESTIMATED without overwriting estimates
    provider.set_channel_revenue(
        channel.youtube.channel_id,
        stage=RevenueStage.FINAL,
        amounts={"ads": Decimal("123.75"), "shorts_ads": Decimal("35.00")},
    )
    provider.set_video_revenue(
        channel.youtube.channel_id,
        _vid(1),
        stage=RevenueStage.FINAL,
        amounts={"shorts_ads": Decimal("35.00")},
    )
    ch_fin = fin_sync.sync_channel(
        channel.id,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        actor=USER,
        revenue_types=("ads", "shorts_ads"),
    )
    assert ch_fin.is_final is True
    assert ch_fin.created_count == 2
    assert all(not o.freshness.is_stale for o in ch_fin.outcomes)

    # Freshness becomes stale after max_age (14 days)
    stale_check = fin_sync.freshness(
        ch_fin.records[0], now=NOW + timedelta(days=15)
    )
    assert stale_check.is_stale is True

    vid_fin = fin_sync.sync_videos(
        channel.id,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        actor=USER,
        revenue_types=("shorts_ads",),
    )
    assert vid_fin.is_final is True
    assert vid_fin.created_count == 1
    assert vid_fin.no_data_video_ids == (_vid(2),)

    # Estimated history (2 ads + 1 shorts_ads) and final records (1 ads + 1 shorts_ads) remain distinct
    assert len(est_sync.channel_records(channel.id)) == 3
    assert len(fin_sync.channel_records(channel.id)) == 2

    # 5. Side-by-side comparison computes final - latest_estimated variance without mutating either
    comparison = fin_sync.compare_with_estimated(
        MetricScope.CHANNEL,
        channel.id,
        revenue_type="ads",
        period_start=PERIOD_START,
        period_end=PERIOD_END,
    )
    assert comparison.estimated is not None
    assert comparison.final is not None
    assert comparison.estimated.amount == Decimal("125.00")
    assert comparison.final.amount == Decimal("123.75")
    assert comparison.variance == Decimal("-1.25")
    assert comparison.currency == "USD"


def test_multi_source_revenue_breakdown_and_non_guaranteed_monetization_goals(
    database: Database,
) -> None:
    """Multi-source revenue supports ads, memberships, affiliate, sponsorships, and custom streams."""
    channel = make_channel(status=ChannelStatus.ACTIVE)
    monetization = Monetization(
        currency="USD",
        goals=(
            RevenueGoal(RevenueSource.ADS, Decimal("200.00")),
            RevenueGoal(RevenueSource.MEMBERSHIPS, Decimal("50.00")),
            RevenueGoal(RevenueSource.SPONSORSHIPS, Decimal("300.00")),
            RevenueGoal(RevenueSource.AFFILIATE, None),
        ),
    )
    profile = dataclasses.replace(
        make_strategy_profile(channel), monetization=monetization
    )
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)

    registry = RevenueStreamRegistry()
    registry.register(
        RevenueStreamSpec(
            revenue_type="course_licensing",
            category=RevenueStreamCategory.CUSTOM,
            default_source="licensing_ledger",
            supports_estimated=False,
            supports_final=True,
            supports_video_scope=False,
        )
    )
    audit = AuditLog(InMemoryAuditSink(), clock=lambda: NOW)
    service = RevenueSourceService(
        database, audit, registry=registry, clock=lambda: NOW
    )

    # Record FINAL revenue across ads, memberships, sponsorships, affiliate, and custom stream
    for rtype, amount in (
        ("ads", Decimal("180.00")),
        ("memberships", Decimal("65.00")),
        ("sponsorships", Decimal("300.00")),
        ("affiliate", Decimal("45.50")),
        ("course_licensing", Decimal("100.00")),
    ):
        outcome = service.record(
            channel.id,
            stage=RevenueStage.FINAL,
            revenue_type=rtype,
            amount=amount,
            currency="USD",
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            actor=USER,
        )
        assert outcome.created is True

    # Also record ESTIMATED ads revenue in the same period to prove single-stage isolation
    service.record(
        channel.id,
        stage=RevenueStage.ESTIMATED,
        revenue_type="ads",
        amount=Decimal("210.00"),
        currency="USD",
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        actor=USER,
    )

    final_bd = service.breakdown(
        MetricScope.CHANNEL,
        channel.id,
        stage=RevenueStage.FINAL,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
    )
    assert final_bd.is_final is True
    assert final_bd.currency == "USD"
    assert final_bd.total_amount == Decimal("690.50")
    assert final_bd.by_category == {
        "youtube_ads": Decimal("180.00"),
        "youtube_fan_funding": Decimal("65.00"),
        "partner_external": Decimal("345.50"),
        "custom": Decimal("100.00"),
    }
    assert len(final_bd.goal_progress) == 4
    progress_by_type = {g.revenue_type: g for g in final_bd.goal_progress}
    assert progress_by_type["ads"].target_delta == Decimal("-20.00")
    assert progress_by_type["memberships"].target_delta == Decimal("15.00")
    assert progress_by_type["sponsorships"].target_delta == Decimal("0.00")
    assert progress_by_type["affiliate"].target_delta is None
    assert all(g.is_guaranteed is False for g in final_bd.goal_progress)

    est_bd = service.breakdown(
        MetricScope.CHANNEL,
        channel.id,
        stage=RevenueStage.ESTIMATED,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
    )
    assert est_bd.is_final is False
    assert est_bd.total_amount == Decimal("210.00")


def test_end_to_end_cost_recorders_rate_corrections_and_contribution_margin(
    database: Database, tmp_path
) -> None:
    """LLM, TTS, render, storage, and API costs flow into CostLedger and ContributionMarginCalculator."""
    channel = make_channel(status=ChannelStatus.ACTIVE)
    profile = make_strategy_profile(channel)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)

    item, video_artifact = _publish_video(database, channel, profile, _vid(10))
    script = make_script(item)
    voice = make_voice_profile(channel)
    tts_job = (
        make_ai_job(item, kind="audio.generate", idempotency_key="aud:ec10")
        .start(clock=lambda: IN_WINDOW)
        .succeed(clock=lambda: IN_WINDOW)
    )
    with database.transaction() as connection:
        ScriptRepository(connection).add(script)
        VoiceProfileRepository(connection).add(voice)
        AIJobRepository(connection).add(tts_job)

    storage = LocalFileStorage(tmp_path / "artifacts")
    sink = InMemoryAuditSink()
    audit = AuditLog(sink, clock=lambda: IN_WINDOW)
    ledger = CostLedger(database, audit, clock=lambda: IN_WINDOW)

    # 1. LLM cost (2000 input @ $0.005/1k + 1000 output @ $0.015/1k = $0.025000)
    llm_catalog = LLMPricingCatalog(
        {
            ("mock", "gpt-script"): LLMModelRate(
                input_price_per_1k=Decimal("0.005"),
                output_price_per_1k=Decimal("0.015"),
                currency="USD",
            )
        },
        default_rate=None,
    )
    llm_recorder = LLMCostRecorder(ledger, catalog=llm_catalog)
    generated = GeneratedText(
        texts=("Generated script hook",),
        provider="mock",
        model="gpt-script",
        generated_at=IN_WINDOW,
        input_tokens=2000,
        output_tokens=1000,
    )
    llm_entry = llm_recorder.record_generated(
        channel.id, generated, content_item_id=item.id, ref="llm:script:1"
    )
    assert llm_entry.created is True
    assert llm_entry.cost.amount == Decimal("0.025000")
    assert (
        llm_recorder.record_generated(
            channel.id, generated, content_item_id=item.id, ref="llm:script:1"
        ).created
        is False
    )

    # 2. TTS cost via AudioArtifactRecorder + TTSCostService (1000 chars @ $0.015/1k = $0.015000)
    synth = MockSpeechSynthesizer(clock=lambda: IN_WINDOW)
    speech = synth.synthesize(
        SpeechRequest(
            text="A" * 100,
            voice_id=voice.voice_id,
            language=voice.language,
        )
    )
    tts_result = TtsResult(
        job=tts_job,
        audio=speech.audio,
        media_type=speech.media_type,
        duration_ms=speech.duration_ms,
        provider=voice.provider,
        voice_id=voice.voice_id,
        characters=1000,
        chunks=1,
        synthesized_chunks=1,
        reused_chunks=0,
    )
    audio_artifact = AudioArtifactRecorder(
        database, storage, audit, clock=lambda: IN_WINDOW
    ).record(tts_result, script=script, voice=voice)
    audio_cost_recorder = AudioCostRecorder(
        database,
        audit,
        price_per_1k=Decimal("0.015"),
        currency="USD",
        clock=lambda: IN_WINDOW,
    )
    tts_service = TTSCostService(
        ledger,
        audio_cost_recorder,
        price_per_1k=Decimal("0.015"),
        currency="USD",
    )
    tts_entry = tts_service.record_artifact(audio_artifact, tts_result, voice)
    assert tts_entry.created is True
    assert tts_entry.cost.amount == Decimal("0.015000")

    # Apply a TTS rate correction from $0.015/1k to $0.020/1k -> appends $0.005000
    tts_corr = tts_service.record_rate_correction(
        audio_artifact.artifact.id,
        characters=1000,
        new_price_per_1k=Decimal("0.020"),
    )
    assert tts_corr.created is True
    assert tts_corr.cost.amount == Decimal("0.005000")
    assert tts_service.summarize_channel(channel.id).total_amount == Decimal(
        "0.020000"
    )

    # 3. Render cost (30,000 ms = 0.5 min @ $0.20/min = $0.100000)
    rendered = MockVideoRenderer(clock=lambda: IN_WINDOW).render(
        VideoRenderRequest(duration_ms=30_000, width=1080, height=1920, fps=30)
    )
    render_recorder = RenderCostRecorder(
        ledger,
        policy=RenderPricingPolicy(
            price_per_minute=Decimal("0.20"), currency="USD"
        ),
    )
    render_entry = render_recorder.record_rendered_artifact(
        channel.id, video_artifact, rendered
    )
    assert render_entry.created is True
    assert render_entry.cost.amount == Decimal("0.100000")

    # 4. Storage and API costs ($0.055000 total: $0.025000 storage + $0.030000 API)
    storage_api_recorder = StorageApiCostRecorder(
        ledger,
        storage_policy=StoragePricingPolicy(
            price_per_gb=Decimal("0.025"), currency="USD"
        ),
        api_policy=ApiPricingPolicy(
            price_per_1k_requests=Decimal("0.010"),
            currency="USD",
            operation_rates={"youtube.upload": Decimal("0.030")},
        ),
    )
    st_entry = storage_api_recorder.record_storage_bytes(
        channel.id,
        provider="local_storage",
        size_bytes=1_073_741_824,
        content_item_id=item.id,
        ref="storage:item10",
    )
    api_entry = storage_api_recorder.record_api_calls(
        channel.id,
        provider="youtube_data",
        requests=1000,
        operation="youtube.upload",
        content_item_id=item.id,
        ref="api:item10",
    )
    assert st_entry.cost.amount == Decimal("0.025000")
    assert api_entry.cost.amount == Decimal("0.030000")

    # Channel-level overhead API cost not bound to a content item ($0.050000)
    storage_api_recorder.record_api_calls(
        channel.id,
        provider="research_api",
        requests=5000,
        ref="api:channel_research",
    )

    # Verify CostLedger summary: item costs = 0.025 + 0.020 + 0.100 + 0.025 + 0.030 = 0.200000
    # Channel total costs = 0.200000 + 0.050000 = 0.250000
    item_summary = ledger.summarize(channel.id, content_item_id=item.id)
    assert item_summary.total_amount == Decimal("0.200000")
    channel_summary = ledger.summarize(
        channel.id, start=PERIOD_START, end=PERIOD_END
    )
    assert channel_summary.total_amount == Decimal("0.250000")
    assert channel_summary.by_category == {
        "llm": Decimal("0.025000"),
        "tts": Decimal("0.020000"),
        "render": Decimal("0.100000"),
        "storage": Decimal("0.025000"),
        "api": Decimal("0.080000"),
    }

    # 5. Record video & channel revenue and verify ContributionMarginCalculator
    rev_service = RevenueSourceService(database, audit, clock=lambda: NOW)
    rev_service.record(
        channel.id,
        stage=RevenueStage.ESTIMATED,
        revenue_type="shorts_ads",
        amount=Decimal("1.00"),
        currency="USD",
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        actor=USER,
        youtube_video_id=_vid(10),
    )
    rev_service.record(
        channel.id,
        stage=RevenueStage.FINAL,
        revenue_type="shorts_ads",
        amount=Decimal("2.00"),
        currency="USD",
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        actor=USER,
        youtube_video_id=_vid(10),
    )
    rev_service.record(
        channel.id,
        stage=RevenueStage.FINAL,
        revenue_type="shorts_ads",
        amount=Decimal("2.50"),
        currency="USD",
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        actor=USER,
    )

    margin_calc = ContributionMarginCalculator(
        database, rev_service, ledger, publish_results=PublishResultService(database)
    )
    item_est_margin = margin_calc.for_content_item(
        item.id,
        stage=RevenueStage.ESTIMATED,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
    )
    assert item_est_margin.metric_label == METRIC_LABEL
    assert item_est_margin.is_net_profit is False
    assert item_est_margin.is_final_revenue is False
    assert item_est_margin.disclaimer == NOT_NET_PROFIT_DISCLAIMER
    assert "net_profit" not in item_est_margin.as_dict()
    assert item_est_margin.revenue_total == Decimal("1.00")
    assert item_est_margin.tracked_contribution_costs_total == Decimal("0.200000")
    assert item_est_margin.contribution_margin == Decimal("0.800000")
    assert item_est_margin.contribution_margin_ratio == Decimal("80.00")

    item_fin_margin = margin_calc.for_content_item(
        item.id,
        stage=RevenueStage.FINAL,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
    )
    assert item_fin_margin.is_final_revenue is True
    assert item_fin_margin.revenue_total == Decimal("2.00")
    assert item_fin_margin.contribution_margin == Decimal("1.800000")
    assert item_fin_margin.contribution_margin_ratio == Decimal("90.00")

    ch_fin_margin = margin_calc.for_channel(
        channel.id,
        stage=RevenueStage.FINAL,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
    )
    assert ch_fin_margin.revenue_total == Decimal("2.50")
    assert ch_fin_margin.tracked_contribution_costs_total == Decimal("0.250000")
    assert ch_fin_margin.contribution_margin == Decimal("2.250000")
    assert ch_fin_margin.contribution_margin_ratio == Decimal("90.00")


def test_currency_consistency_enforced_across_revenue_cost_ledger_and_margin(
    database: Database,
) -> None:
    """Cross-currency mixing is refused across CostLedger, FinalRevenueSync, RevenueSourceService, and Margin."""
    channel = make_channel(status=ChannelStatus.ACTIVE)
    budget = Budget(
        currency="USD",
        daily_limit=Decimal("15.00"),
        monthly_limit=Decimal("100.00"),
    )
    monetization = Monetization(
        currency="USD",
        goals=(RevenueGoal(RevenueSource.ADS, Decimal("100.00")),),
    )
    profile = dataclasses.replace(
        make_strategy_profile(channel), budget=budget, monetization=monetization
    )
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)

    audit = AuditLog(InMemoryAuditSink(), clock=lambda: NOW)
    ledger = CostLedger(database, audit, clock=lambda: IN_WINDOW)
    rev_service = RevenueSourceService(database, audit, clock=lambda: NOW)
    fin_sync = FinalRevenueSync(
        database,
        MockRevenueProvider(clock=lambda: NOW),
        MockYouTubeOAuthProvider(clock=lambda: NOW),
        audit,
        clock=lambda: NOW,
    )
    margin_calc = ContributionMarginCalculator(database, rev_service, ledger)

    # 1. CostLedger refuses recording EUR when channel budget is USD
    with pytest.raises(CostLedgerError) as exc_cost_curr:
        ledger.record(
            channel.id,
            CostCategory.API,
            provider="ext_api",
            amount=Decimal("1.00"),
            currency="EUR",
            ref="api:eur",
        )
    assert (
        exc_cost_curr.value.ledger_code is CostLedgerErrorCode.CURRENCY_MISMATCH
    )

    # 2. FinalRevenueSync.compare_with_estimated refuses mismatched currencies
    rev_service.record(
        channel.id,
        stage=RevenueStage.ESTIMATED,
        revenue_type="ads",
        amount=Decimal("50.00"),
        currency="USD",
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        actor=USER,
    )
    rev_service.record(
        channel.id,
        stage=RevenueStage.FINAL,
        revenue_type="ads",
        amount=Decimal("45.00"),
        currency="EUR",
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        actor=USER,
    )
    with pytest.raises(FinalRevenueError) as exc_comp:
        fin_sync.compare_with_estimated(
            MetricScope.CHANNEL,
            channel.id,
            revenue_type="ads",
            period_start=PERIOD_START,
            period_end=PERIOD_END,
        )
    assert exc_comp.value.revenue_code is FinalRevenueErrorCode.CURRENCY_MISMATCH

    # 3. RevenueSourceService.breakdown refuses when recorded currency (EUR) != monetization goal currency (USD)
    with pytest.raises(RevenueSourceError) as exc_goal_curr:
        rev_service.breakdown(
            MetricScope.CHANNEL,
            channel.id,
            stage=RevenueStage.FINAL,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
        )
    assert (
        exc_goal_curr.value.source_code
        is RevenueSourceErrorCode.CURRENCY_MISMATCH
    )

    # 4. ContributionMarginCalculator maps currency mismatch to ContributionMarginError
    with pytest.raises(ContributionMarginError) as exc_margin_curr:
        margin_calc.for_channel(
            channel.id,
            stage=RevenueStage.FINAL,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
        )
    assert (
        exc_margin_curr.value.margin_code
        is ContributionMarginErrorCode.CURRENCY_MISMATCH
    )


def test_budget_guard_timezone_windows_projected_blocking_and_budget_gate_parity(
    database: Database,
) -> None:
    """BudgetGuard respects the strategy time zone, triggers alerts, blocks projections, and aligns with BudgetGate."""
    channel = make_channel(status=ChannelStatus.ACTIVE)
    cadence = Cadence(
        shorts_per_day=2,
        longform_per_week=1,
        time_zone="Asia/Ho_Chi_Minh",
    )
    budget = Budget(
        currency="USD",
        daily_limit=Decimal("10.00"),
        monthly_limit=Decimal("25.00"),
        alert_thresholds=(50, 80, 100),
    )
    profile = dataclasses.replace(
        make_strategy_profile(channel), cadence=cadence, budget=budget
    )
    item = make_content_item(
        channel, profile, status=ContentStatus.PLANNED
    )
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)
        ContentItemRepository(connection).add(item)

    # In Asia/Ho_Chi_Minh (UTC+7):
    # local midnight of 2026-10-20 is 2026-10-19T17:00:00Z.
    # Spend at 2026-10-19T16:30:00Z is in yesterday's local window (Oct 19),
    # whereas spend at 2026-10-19T17:30:00Z is in today's local window (Oct 20).
    eval_at = datetime(2026, 10, 20, 2, 0, tzinfo=UTC)  # 2026-10-20 09:00+07:00
    yesterday_local = datetime(2026, 10, 19, 16, 30, tzinfo=UTC)
    today_local = datetime(2026, 10, 19, 17, 30, tzinfo=UTC)

    sink = InMemoryAuditSink()
    audit = AuditLog(sink, clock=lambda: eval_at)
    guard = BudgetGuard(database, audit, clock=lambda: eval_at)
    gate = BudgetGate(database)

    with database.transaction() as connection:
        costs = CostRecordRepository(connection)
        costs.add(
            CostRecord.create(
                channel.id,
                CostCategory.LLM,
                provider="mock_llm",
                amount=Decimal("15.00"),
                currency="USD",
                clock=lambda: yesterday_local,
            )
        )
        costs.add(
            CostRecord.create(
                channel.id,
                CostCategory.TTS,
                provider="mock_tts",
                amount=Decimal("5.00"),
                currency="USD",
                clock=lambda: today_local,
            )
        )

    # 1. Current daily spend = $5.00 (50% of $10), monthly spend = $20.00 (80% of $25)
    status = guard.enforce(channel.id, at=eval_at)
    assert status.allowed is True
    assert status.time_zone == "Asia/Ho_Chi_Minh"
    assert status.daily_spend == Decimal("5.00")
    assert status.monthly_spend == Decimal("20.00")
    assert [(a.window, a.threshold_percent) for a in status.alerts] == [
        (BudgetWindowKind.DAILY, 50),
        (BudgetWindowKind.MONTHLY, 50),
        (BudgetWindowKind.MONTHLY, 80),
    ]
    assert sink.entries[-1].action == BUDGET_ALERT_TRIGGERED

    # BudgetGate also passes at this point
    gate_ctx = TransitionContext(
        item=item,
        target=ContentStatus.GENERATING,
        actor=USER,
        at=eval_at,
    )
    assert gate.evaluate(gate_ctx).outcome is GateOutcome.PASS

    # 2. Pre-execution projection of +$6.00 pushes projected daily ($11 > $10) AND projected monthly ($26 > $25)
    with pytest.raises(BudgetGuardBlockedError) as exc_proj:
        guard.enforce(
            channel.id,
            estimated_additional_cost=Decimal("6.00"),
            at=eval_at,
        )
    assert [r.code for r in exc_proj.value.status.blocking_reasons] == [
        "budget.projected_daily_exceeded",
        "budget.projected_monthly_exceeded",
    ]
    assert sink.entries[-1].action == BUDGET_GUARD_BLOCKED
    assert sink.entries[-1].result is AuditResult.DENIED

    # 3. Record actual spend of +$5.00 today -> daily reaches $10.00, monthly reaches $25.00
    CostLedger(database, audit, clock=lambda: eval_at).record(
        channel.id,
        CostCategory.RENDER,
        provider="ffmpeg",
        amount=Decimal("5.00"),
        currency="USD",
        ref="render:limit",
    )
    with pytest.raises(BudgetGuardBlockedError) as exc_actual:
        guard.enforce(channel.id, at=eval_at)
    blocking_codes = [r.code for r in exc_actual.value.status.blocking_reasons]
    assert blocking_codes == [
        "budget.daily_exceeded",
        "budget.monthly_exceeded",
    ]

    # BudgetGate blocks with the exact same reason codes
    gate_result = gate.evaluate(gate_ctx)
    assert gate_result.outcome is GateOutcome.BLOCK
    assert [r.code for r in gate_result.reasons] == blocking_codes
