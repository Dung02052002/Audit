import logging
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_youtube_agent import __version__
from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.audio_artifact import AudioArtifactRecorder
from ai_youtube_agent.content.audio_cost import AudioCostRecorder
from ai_youtube_agent.content.audio_validation import AudioValidator
from ai_youtube_agent.content.broll_manager import BrollManager
from ai_youtube_agent.content.longform_audio_mix import LongFormAudioMixer
from ai_youtube_agent.content.longform_bundle import LongFormBundleBuilder
from ai_youtube_agent.content.longform_metadata import LongFormMetadataRecorder
from ai_youtube_agent.content.longform_render import LongFormRenderService
from ai_youtube_agent.content.longform_service import LongFormProjectService
from ai_youtube_agent.content.longform_subtitles import LongFormSubtitleRecorder
from ai_youtube_agent.content.longform_thumbnail import LongFormThumbnailRecorder
from ai_youtube_agent.content.qc_disclosure import DisclosureQCChecker
from ai_youtube_agent.content.qc_harness import VideoArtifactTestHarness
from ai_youtube_agent.content.qc_policy import PolicyQCChecker
from ai_youtube_agent.content.qc_report import QCReportBuilder
from ai_youtube_agent.content.qc_rights import RightsQCChecker
from ai_youtube_agent.content.shorts_audio_mix import (
    ArtifactAssetSource,
    ShortsAudioMixer,
)
from ai_youtube_agent.content.shorts_bundle import ShortsBundleBuilder
from ai_youtube_agent.content.shorts_metadata import ShortsMetadataRecorder
from ai_youtube_agent.content.shorts_render import (
    ArtifactVisualSource,
    ShortsRenderService,
)
from ai_youtube_agent.content.shorts_service import ShortsProjectService
from ai_youtube_agent.content.shorts_subtitles import ShortsSubtitleRecorder
from ai_youtube_agent.content.shorts_thumbnail import ShortsThumbnailRecorder
from ai_youtube_agent.core.artifact_storage import ArtifactStorage, LocalFileStorage
from ai_youtube_agent.core.config import Environment, RenderProviderKind, Settings
from ai_youtube_agent.core.di import Container
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.core.log import Severity
from ai_youtube_agent.main import create_app, provide
from ai_youtube_agent.providers.ffmpeg_video_render import FfmpegVideoRenderer
from ai_youtube_agent.providers.mock_video_render import MockVideoRenderer
from ai_youtube_agent.providers.video_render import VideoRenderer


@pytest.fixture(autouse=True)
def restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment=Environment.TEST,
        app_name="agent-under-test",
        log_level=Severity.WARNING,
        flags=FeatureFlags(longform_enabled=True),
    )


def test_container_registers_core_interfaces(settings: Settings) -> None:
    container = build_container(settings)

    assert container.resolve(Settings) is settings
    assert container.resolve(FeatureFlags) is settings.flags
    assert container.resolve(FeatureFlags).longform_enabled is True


def test_create_app_uses_the_given_container(settings: Settings) -> None:
    container = build_container(settings)

    app = create_app(container)

    assert app.state.container is container
    assert app.title == "agent-under-test"
    assert logging.getLogger().level == logging.WARNING


def test_health_keeps_status_and_version(settings: Settings) -> None:
    client = TestClient(create_app(build_container(settings)))

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__


def test_provide_resolves_from_the_app_container(settings: Settings) -> None:
    app = create_app(build_container(settings))

    @app.get("/flags")
    def flags(
        flags: Annotated[FeatureFlags, provide(FeatureFlags)],
    ) -> dict[str, bool]:
        return {"longform_enabled": flags.longform_enabled}

    assert TestClient(app).get("/flags").json() == {"longform_enabled": True}


def test_override_changes_what_the_app_receives(settings: Settings) -> None:
    container = build_container(settings)
    app: FastAPI = create_app(container)

    @app.get("/flags")
    def flags(
        flags: Annotated[FeatureFlags, provide(FeatureFlags)],
    ) -> dict[str, bool]:
        return {"longform_enabled": flags.longform_enabled}

    with container.override(FeatureFlags, lambda _: FeatureFlags()):
        assert TestClient(app).get("/flags").json() == {"longform_enabled": False}


def test_module_app_is_built_with_a_container() -> None:
    """`uvicorn ai_youtube_agent.main:app` still works and carries a container."""
    import ai_youtube_agent.main as main_module

    assert isinstance(main_module.app.state.container, Container)
    assert main_module.app.state.container.is_registered(Settings)


def test_container_resolves_the_artifact_storage_and_audio_recorder(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    settings = Settings(
        environment=Environment.TEST,
        database_path=tmp_path / "agent.db",
        artifact_dir=root,
    )
    container = build_container(settings)

    storage = container.resolve(ArtifactStorage)
    recorder = container.resolve(AudioArtifactRecorder)

    assert isinstance(storage, LocalFileStorage)
    assert isinstance(storage, ArtifactStorage)
    assert storage.root == root.resolve()
    assert isinstance(recorder, AudioArtifactRecorder)
    assert container.resolve(ArtifactStorage) is storage
    assert not root.exists()
    assert not (tmp_path / "agent.db").exists()


def test_container_resolves_the_audio_validator_and_cost_recorder(
    tmp_path: Path,
) -> None:
    settings = Settings(
        environment=Environment.TEST,
        database_path=tmp_path / "agent.db",
        artifact_dir=tmp_path / "artifacts",
        tts_price_per_1k_characters=Decimal("0.015"),
        tts_cost_currency="EUR",
    )
    container = build_container(settings)

    validator = container.resolve(AudioValidator)
    cost_recorder = container.resolve(AudioCostRecorder)

    assert isinstance(validator, AudioValidator)
    assert isinstance(cost_recorder, AudioCostRecorder)
    assert cost_recorder._price_per_1k == Decimal("0.015")
    assert cost_recorder._currency == "EUR"
    assert container.resolve(AudioValidator) is validator
    assert not (tmp_path / "agent.db").exists()


def test_container_resolves_the_shorts_project_service(tmp_path: Path) -> None:
    settings = Settings(
        environment=Environment.TEST, database_path=tmp_path / "agent.db"
    )
    container = build_container(settings)

    service = container.resolve(ShortsProjectService)

    assert isinstance(service, ShortsProjectService)
    assert container.resolve(ShortsProjectService) is service
    assert not (tmp_path / "agent.db").exists()


def test_container_resolves_the_shorts_subtitle_recorder(tmp_path: Path) -> None:
    settings = Settings(
        environment=Environment.TEST,
        database_path=tmp_path / "agent.db",
        artifact_dir=tmp_path / "artifacts",
    )
    container = build_container(settings)

    recorder = container.resolve(ShortsSubtitleRecorder)

    assert isinstance(recorder, ShortsSubtitleRecorder)
    assert container.resolve(ShortsSubtitleRecorder) is recorder
    assert not (tmp_path / "agent.db").exists()
    assert not (tmp_path / "artifacts").exists()


def test_container_resolves_the_shorts_audio_mixer(tmp_path: Path) -> None:
    settings = Settings(
        environment=Environment.TEST,
        database_path=tmp_path / "agent.db",
        artifact_dir=tmp_path / "artifacts",
    )
    container = build_container(settings)

    mixer = container.resolve(ShortsAudioMixer)

    assert isinstance(mixer, ShortsAudioMixer)
    assert container.resolve(ShortsAudioMixer) is mixer
    assert isinstance(mixer._assets, ArtifactAssetSource)
    assert not (tmp_path / "agent.db").exists()
    assert not (tmp_path / "artifacts").exists()


def test_container_resolves_the_mock_renderer_and_the_render_service(
    tmp_path: Path,
) -> None:
    settings = Settings(
        environment=Environment.TEST,
        database_path=tmp_path / "agent.db",
        artifact_dir=tmp_path / "artifacts",
    )
    container = build_container(settings)

    service = container.resolve(ShortsRenderService)

    assert isinstance(service, ShortsRenderService)
    assert container.resolve(ShortsRenderService) is service
    renderer = container.resolve(VideoRenderer)
    assert isinstance(renderer, MockVideoRenderer)
    assert service._renderer is renderer
    assert service._mixer is container.resolve(ShortsAudioMixer)
    assert isinstance(service._visuals, ArtifactVisualSource)
    assert not (tmp_path / "agent.db").exists()
    assert not (tmp_path / "artifacts").exists()


def test_container_builds_the_ffmpeg_renderer_from_the_settings(
    tmp_path: Path,
) -> None:
    settings = Settings(
        environment=Environment.TEST,
        database_path=tmp_path / "agent.db",
        render_provider=RenderProviderKind.FFMPEG,
        ffmpeg_binary="/opt/ffmpeg",
        render_timeout_seconds=90,
        render_font_file=tmp_path / "captions.ttf",
    )

    renderer = build_container(settings).resolve(VideoRenderer)

    assert isinstance(renderer, FfmpegVideoRenderer)
    assert renderer.name == "ffmpeg"
    assert renderer._binary == "/opt/ffmpeg"
    assert renderer._timeout_s == 90
    assert renderer._font_file == tmp_path / "captions.ttf"


def test_container_resolves_shorts_thumbnail_metadata_and_bundle_services(
    tmp_path: Path,
) -> None:
    settings = Settings(
        environment=Environment.TEST,
        database_path=tmp_path / "agent.db",
        artifact_dir=tmp_path / "artifacts",
    )
    container = build_container(settings)

    thumbs = container.resolve(ShortsThumbnailRecorder)
    metadata = container.resolve(ShortsMetadataRecorder)
    bundles = container.resolve(ShortsBundleBuilder)

    assert isinstance(thumbs, ShortsThumbnailRecorder)
    assert isinstance(metadata, ShortsMetadataRecorder)
    assert isinstance(bundles, ShortsBundleBuilder)
    assert container.resolve(ShortsThumbnailRecorder) is thumbs
    assert container.resolve(ShortsMetadataRecorder) is metadata
    assert container.resolve(ShortsBundleBuilder) is bundles


def test_container_resolves_longform_production_services(
    tmp_path: Path,
) -> None:
    settings = Settings(
        environment=Environment.TEST,
        database_path=tmp_path / "agent.db",
        artifact_dir=tmp_path / "artifacts",
        flags=FeatureFlags(longform_enabled=True),
    )
    container = build_container(settings)

    projects = container.resolve(LongFormProjectService)
    broll = container.resolve(BrollManager)
    subtitles = container.resolve(LongFormSubtitleRecorder)
    mixer = container.resolve(LongFormAudioMixer)

    assert isinstance(projects, LongFormProjectService)
    assert isinstance(broll, BrollManager)
    assert isinstance(subtitles, LongFormSubtitleRecorder)
    assert isinstance(mixer, LongFormAudioMixer)
    assert isinstance(container.resolve(LongFormRenderService), LongFormRenderService)
    assert isinstance(
        container.resolve(LongFormThumbnailRecorder), LongFormThumbnailRecorder
    )
    assert isinstance(
        container.resolve(LongFormMetadataRecorder), LongFormMetadataRecorder
    )
    assert isinstance(container.resolve(LongFormBundleBuilder), LongFormBundleBuilder)
    assert container.resolve(LongFormProjectService) is projects
    assert container.resolve(BrollManager) is broll
    assert container.resolve(LongFormSubtitleRecorder) is subtitles
    assert container.resolve(LongFormAudioMixer) is mixer


def test_container_resolves_qc_services(tmp_path: Path) -> None:
    settings = Settings(
        environment=Environment.TEST,
        database_path=tmp_path / "agent.db",
        artifact_dir=tmp_path / "artifacts",
    )
    container = build_container(settings)

    harness = container.resolve(VideoArtifactTestHarness)
    rights_qc = container.resolve(RightsQCChecker)
    policy_qc = container.resolve(PolicyQCChecker)
    disclosure_qc = container.resolve(DisclosureQCChecker)
    report_builder = container.resolve(QCReportBuilder)

    assert isinstance(harness, VideoArtifactTestHarness)
    assert isinstance(rights_qc, RightsQCChecker)
    assert isinstance(policy_qc, PolicyQCChecker)
    assert isinstance(disclosure_qc, DisclosureQCChecker)
    assert isinstance(report_builder, QCReportBuilder)
    assert container.resolve(QCReportBuilder) is report_builder


def test_container_resolves_preview_and_approval_services(tmp_path: Path) -> None:
    from ai_youtube_agent.content.approval_service import ApprovalService
    from ai_youtube_agent.content.email_approval import ApprovalEmailService
    from ai_youtube_agent.content.preview_artifact import PreviewArtifactService
    from ai_youtube_agent.content.preview_edit_loop import PreviewEditLoopService
    from ai_youtube_agent.content.preview_metadata import PreviewMetadataService
    from ai_youtube_agent.content.preview_player import PreviewPlayerService
    from ai_youtube_agent.content.preview_reports import PreviewReportService
    from ai_youtube_agent.content.version_comparison import VersionComparisonService
    from ai_youtube_agent.providers.approval_email import ApprovalEmailProvider
    from ai_youtube_agent.providers.mock_approval_email import MockApprovalEmailProvider

    settings = Settings(
        environment=Environment.TEST,
        database_path=tmp_path / "agent.db",
        artifact_dir=tmp_path / "artifacts",
    )
    container = build_container(settings)

    assert isinstance(
        container.resolve(PreviewArtifactService), PreviewArtifactService
    )
    assert isinstance(container.resolve(PreviewPlayerService), PreviewPlayerService)
    assert isinstance(
        container.resolve(PreviewMetadataService), PreviewMetadataService
    )
    assert isinstance(container.resolve(PreviewReportService), PreviewReportService)
    assert isinstance(
        container.resolve(VersionComparisonService), VersionComparisonService
    )
    assert isinstance(
        container.resolve(PreviewEditLoopService), PreviewEditLoopService
    )
    from ai_youtube_agent.content.approval_audit import ApprovalAuditService

    assert isinstance(
        container.resolve(ApprovalEmailProvider), MockApprovalEmailProvider
    )
    assert isinstance(container.resolve(ApprovalEmailService), ApprovalEmailService)
    assert isinstance(container.resolve(ApprovalService), ApprovalService)
    assert isinstance(container.resolve(ApprovalAuditService), ApprovalAuditService)


def test_container_resolves_youtube_publishing_services(tmp_path: Path) -> None:
    from ai_youtube_agent.pipeline.duplicate_guard import PublishDuplicateGuard
    from ai_youtube_agent.pipeline.longform_publisher import LongFormPublisher
    from ai_youtube_agent.pipeline.publish_audit import PublishAuditService
    from ai_youtube_agent.pipeline.publish_executor import PublishExecutor
    from ai_youtube_agent.pipeline.publish_metadata_mapper import (
        PublishMetadataMapper,
    )
    from ai_youtube_agent.pipeline.publish_result import PublishResultService
    from ai_youtube_agent.pipeline.shorts_publisher import ShortsPublisher
    from ai_youtube_agent.providers.mock_youtube_channel import (
        MockYouTubeChannelProvider,
    )
    from ai_youtube_agent.providers.mock_youtube_oauth import (
        MockYouTubeOAuthProvider,
    )
    from ai_youtube_agent.providers.mock_youtube_upload import (
        MockYouTubeUploadProvider,
    )
    from ai_youtube_agent.providers.youtube_channel import YouTubeChannelProvider
    from ai_youtube_agent.providers.youtube_oauth import YouTubeOAuthProvider
    from ai_youtube_agent.providers.youtube_upload import YouTubeUploadProvider

    settings = Settings(
        environment=Environment.TEST,
        database_path=tmp_path / "agent.db",
        artifact_dir=tmp_path / "artifacts",
    )
    container = build_container(settings)

    assert isinstance(
        container.resolve(YouTubeOAuthProvider), MockYouTubeOAuthProvider
    )
    assert isinstance(
        container.resolve(YouTubeChannelProvider), MockYouTubeChannelProvider
    )
    assert isinstance(
        container.resolve(YouTubeUploadProvider), MockYouTubeUploadProvider
    )
    assert isinstance(
        container.resolve(PublishMetadataMapper), PublishMetadataMapper
    )
    assert isinstance(container.resolve(ShortsPublisher), ShortsPublisher)
    assert isinstance(container.resolve(LongFormPublisher), LongFormPublisher)
    assert isinstance(container.resolve(PublishDuplicateGuard), PublishDuplicateGuard)
    executor = container.resolve(PublishExecutor)
    assert isinstance(executor, PublishExecutor)
    assert container.resolve(PublishExecutor) is executor
    assert executor._retry_policy.max_attempts == settings.upload_max_attempts
    assert (
        executor._retry_policy.base_delay_seconds
        == settings.upload_retry_base_delay_seconds
    )
    assert isinstance(container.resolve(PublishResultService), PublishResultService)
    assert isinstance(container.resolve(PublishAuditService), PublishAuditService)


def test_container_resolves_the_analytics_services(tmp_path: Path) -> None:
    from ai_youtube_agent.content.analytics_freshness import AnalyticsFreshnessService
    from ai_youtube_agent.content.channel_metrics import ChannelMetricsSync
    from ai_youtube_agent.content.ctr import CTRSync
    from ai_youtube_agent.content.longform_metrics import LongFormMetricsService
    from ai_youtube_agent.content.retention import RetentionSync
    from ai_youtube_agent.content.shorts_metrics import ShortsMetricsService
    from ai_youtube_agent.content.subscriber_change import SubscriberChangeService
    from ai_youtube_agent.content.video_metrics import VideoMetricsSync
    from ai_youtube_agent.content.watch_time import WatchTimeService
    from ai_youtube_agent.providers.analytics import AnalyticsProvider
    from ai_youtube_agent.providers.analytics_cache import AnalyticsCache
    from ai_youtube_agent.providers.mock_analytics import MockAnalyticsProvider
    from ai_youtube_agent.providers.mock_revenue import MockRevenueProvider
    from ai_youtube_agent.providers.revenue import RevenueProvider

    settings = Settings(
        environment=Environment.TEST,
        database_path=tmp_path / "agent.db",
        artifact_dir=tmp_path / "artifacts",
    )
    container = build_container(settings)

    provider = container.resolve(AnalyticsProvider)
    assert isinstance(provider, MockAnalyticsProvider)
    assert container.resolve(AnalyticsProvider) is provider
    assert isinstance(container.resolve(AnalyticsCache), AnalyticsCache)
    assert isinstance(container.resolve(ChannelMetricsSync), ChannelMetricsSync)
    assert isinstance(container.resolve(VideoMetricsSync), VideoMetricsSync)
    assert isinstance(container.resolve(ShortsMetricsService), ShortsMetricsService)
    assert isinstance(
        container.resolve(LongFormMetricsService), LongFormMetricsService
    )
    assert isinstance(container.resolve(WatchTimeService), WatchTimeService)
    assert isinstance(container.resolve(RetentionSync), RetentionSync)
    assert isinstance(container.resolve(CTRSync), CTRSync)
    assert isinstance(
        container.resolve(SubscriberChangeService), SubscriberChangeService
    )
    assert isinstance(
        container.resolve(AnalyticsFreshnessService), AnalyticsFreshnessService
    )
    rev_provider = container.resolve(RevenueProvider)
    assert isinstance(rev_provider, MockRevenueProvider)
    assert container.resolve(RevenueProvider) is rev_provider
    assert not (tmp_path / "agent.db").exists()


def test_container_resolves_the_economics_services(tmp_path: Path) -> None:
    from ai_youtube_agent.content.budget_guard import BudgetGuard
    from ai_youtube_agent.content.contribution_margin import (
        ContributionMarginCalculator,
    )
    from ai_youtube_agent.content.cost_ledger import CostLedger
    from ai_youtube_agent.content.estimated_revenue import EstimatedRevenueSync
    from ai_youtube_agent.content.final_revenue import FinalRevenueSync
    from ai_youtube_agent.content.llm_cost import LLMCostRecorder
    from ai_youtube_agent.content.render_cost import RenderCostRecorder
    from ai_youtube_agent.content.revenue_sources import (
        RevenueSourceService,
        RevenueStreamRegistry,
    )
    from ai_youtube_agent.content.storage_api_cost import StorageApiCostRecorder
    from ai_youtube_agent.content.tts_cost import TTSCostService

    settings = Settings(
        environment=Environment.TEST,
        database_path=tmp_path / "agent.db",
        artifact_dir=tmp_path / "artifacts",
    )
    container = build_container(settings)

    assert isinstance(
        container.resolve(EstimatedRevenueSync), EstimatedRevenueSync
    )
    assert isinstance(container.resolve(FinalRevenueSync), FinalRevenueSync)
    assert isinstance(
        container.resolve(RevenueStreamRegistry), RevenueStreamRegistry
    )
    assert isinstance(
        container.resolve(RevenueSourceService), RevenueSourceService
    )
    assert isinstance(container.resolve(CostLedger), CostLedger)
    assert isinstance(container.resolve(LLMCostRecorder), LLMCostRecorder)
    assert isinstance(container.resolve(TTSCostService), TTSCostService)
    assert isinstance(container.resolve(RenderCostRecorder), RenderCostRecorder)
    assert isinstance(
        container.resolve(StorageApiCostRecorder), StorageApiCostRecorder
    )
    assert isinstance(
        container.resolve(ContributionMarginCalculator),
        ContributionMarginCalculator,
    )
    assert isinstance(container.resolve(BudgetGuard), BudgetGuard)
    assert not (tmp_path / "agent.db").exists()


def test_container_resolves_the_community_comment_services(tmp_path: Path) -> None:
    from ai_youtube_agent.content.auto_reply import AutoReplyGuard, AutoReplyService
    from ai_youtube_agent.content.comment_audit import CommentAuditService
    from ai_youtube_agent.content.comment_classifier import CommentClassifier
    from ai_youtube_agent.content.comment_sync import CommentSync
    from ai_youtube_agent.content.duplicate_reply_guard import DuplicateReplyGuard
    from ai_youtube_agent.content.reply_approval import ReplyApprovalService
    from ai_youtube_agent.content.reply_draft import ReplyDraftService
    from ai_youtube_agent.providers.comments import CommentProvider
    from ai_youtube_agent.providers.mock_comments import MockCommentProvider

    settings = Settings(
        environment=Environment.TEST,
        database_path=tmp_path / "agent.db",
        artifact_dir=tmp_path / "artifacts",
    )
    container = build_container(settings)

    provider = container.resolve(CommentProvider)
    assert isinstance(provider, MockCommentProvider)
    assert container.resolve(CommentProvider) is provider
    assert isinstance(container.resolve(CommentSync), CommentSync)
    assert isinstance(container.resolve(CommentClassifier), CommentClassifier)
    assert isinstance(container.resolve(ReplyDraftService), ReplyDraftService)
    assert isinstance(container.resolve(DuplicateReplyGuard), DuplicateReplyGuard)
    assert isinstance(container.resolve(ReplyApprovalService), ReplyApprovalService)
    assert isinstance(container.resolve(AutoReplyGuard), AutoReplyGuard)
    assert isinstance(container.resolve(AutoReplyService), AutoReplyService)
    assert isinstance(container.resolve(CommentAuditService), CommentAuditService)
    assert not (tmp_path / "agent.db").exists()


def test_container_resolves_the_command_center_dashboard_services(
    tmp_path: Path,
) -> None:
    from ai_youtube_agent.content.dashboard_ai_center import AICenterScreenService
    from ai_youtube_agent.content.dashboard_alerts import AlertsScreenService
    from ai_youtube_agent.content.dashboard_analytics import AnalyticsScreenService
    from ai_youtube_agent.content.dashboard_approvals import ApprovalCenterService
    from ai_youtube_agent.content.dashboard_channel import ChannelOverviewService
    from ai_youtube_agent.content.dashboard_comments import CommentsScreenService
    from ai_youtube_agent.content.dashboard_content import ContentScreenService
    from ai_youtube_agent.content.dashboard_home import HomeScreenService
    from ai_youtube_agent.content.dashboard_revenue import RevenueScreenService
    from ai_youtube_agent.content.dashboard_settings import SettingsScreenService
    from ai_youtube_agent.content.dashboard_shell import DashboardShellService
    from ai_youtube_agent.content.dashboard_video_detail import VideoDetailService

    settings = Settings(
        environment=Environment.TEST,
        database_path=tmp_path / "agent.db",
        artifact_dir=tmp_path / "artifacts",
    )
    container = build_container(settings)

    assert isinstance(
        container.resolve(DashboardShellService), DashboardShellService
    )
    assert isinstance(container.resolve(HomeScreenService), HomeScreenService)
    assert isinstance(
        container.resolve(ChannelOverviewService), ChannelOverviewService
    )
    assert isinstance(container.resolve(ContentScreenService), ContentScreenService)
    assert isinstance(container.resolve(VideoDetailService), VideoDetailService)
    assert isinstance(
        container.resolve(AnalyticsScreenService), AnalyticsScreenService
    )
    assert isinstance(container.resolve(RevenueScreenService), RevenueScreenService)
    assert isinstance(
        container.resolve(CommentsScreenService), CommentsScreenService
    )
    assert isinstance(
        container.resolve(AICenterScreenService), AICenterScreenService
    )
    assert isinstance(
        container.resolve(ApprovalCenterService), ApprovalCenterService
    )
    assert isinstance(
        container.resolve(SettingsScreenService), SettingsScreenService
    )
    assert isinstance(
        container.resolve(AlertsScreenService), AlertsScreenService
    )
    assert not (tmp_path / "agent.db").exists()


def test_container_resolves_orchestrator_job_queue(
    tmp_path: Path,
) -> None:
    from ai_youtube_agent.pipeline.job_queue import JobQueue, SqliteJobQueue

    settings = Settings(
        environment=Environment.TEST,
        database_path=tmp_path / "agent.db",
        artifact_dir=tmp_path / "artifacts",
    )
    container = build_container(settings)

    queue = container.resolve(JobQueue)
    assert isinstance(queue, SqliteJobQueue)
    assert container.resolve(JobQueue) is queue

    from ai_youtube_agent.pipeline.job_scheduler import JobScheduler

    scheduler = container.resolve(JobScheduler)
    assert isinstance(scheduler, JobScheduler)
    assert container.resolve(JobScheduler) is scheduler

    from ai_youtube_agent.pipeline.pipeline_runner import PipelineRunner

    runner = container.resolve(PipelineRunner)
    assert isinstance(runner, PipelineRunner)
    assert container.resolve(PipelineRunner) is runner

    from ai_youtube_agent.pipeline.approval_wait import ApprovalWaitService

    wait_service = container.resolve(ApprovalWaitService)
    assert isinstance(wait_service, ApprovalWaitService)
    assert container.resolve(ApprovalWaitService) is wait_service
    assert not (tmp_path / "agent.db").exists()
