"""Composition root: the only module that chooses implementations.

Every other module depends on interfaces and gets them from the container.
Later prompts register their provider interfaces here, choosing the real or
mock implementation from ``Settings``, and add a ``CheckKind.PROVIDER`` health
check for each provider to the ``HealthRegistry``.

``prepare_database`` applies the database migrations (#029). The application
calls it once at startup, from the FastAPI lifespan in ``main.create_app``.
``Database`` is a singleton for ``Settings.database_path``. The audit sink is
``SqliteAuditSink`` except in the TEST environment, which keeps events in
memory (#030).

``ResearchProvider`` (#054) is chosen by ``Settings.research_provider``; only
the in-memory mock exists, and its ``check`` is the ``research_provider``
health check. Since #061 it is wrapped in ``ResearchCache``.
``TextGenerator`` (#065) is chosen by ``Settings.text_provider``; only the
mock exists, and its ``check`` is the ``text_provider`` health check.
``SpeechSynthesizer`` (#086) is chosen by ``Settings.voice_provider``; only
the mock exists, and its ``check`` is the ``voice_provider`` health check.
``ArtifactStorage`` (#089) is ``LocalFileStorage`` at ``Settings.artifact_dir``
(created on the first write); ``AudioArtifactRecorder`` (#089) stores audio
through it and ``AudioValidator`` (#090) checks the stored audio.
``AudioCostRecorder`` (#092) gets ``Settings.tts_price_per_1k_characters`` and
``Settings.tts_cost_currency``. ``TtsJobRunner`` (#088) is not registered yet:
its only chunk store keeps chunks in memory.
``ShortsProjectService`` (#094) makes and keeps the Shorts project of a content
item over the database. ``ShortsSubtitleRecorder`` (#098) stores its SRT
subtitles through the same ``ArtifactStorage``. ``ShortsAudioMixer`` (#099)
mixes the voice with the music and effects of a project in memory, reading the
files through that storage; it stores nothing.
``VideoRenderer`` (#100) is chosen by ``Settings.render_provider``: the mock
(default) or ``FfmpegVideoRenderer`` with ``Settings.ffmpeg_binary``,
``Settings.render_timeout_seconds`` and ``Settings.render_font_file``; its
``check`` is the ``render_provider`` health check. ``ShortsRenderService``
(#100) renders a ready project through it, with the mix of
``ShortsAudioMixer``, and stores the MP4 through the ``ArtifactStorage``.
``ClaimExtractor`` (#068), ``EvidenceMatcher`` (#069), ``FactChecker``
(#070), ``OriginalityChecker`` (#071), ``ScriptValidator`` (#072),
``ScriptVersioner`` (#073), ``AssetRegistry`` (#076), ``ProvenanceRecorder``
(#077), ``RightsRiskEngine`` (#078), ``DisclosureDecider`` (G-081) and
``RightsReporter`` (G-082) and ``PolicyReporter`` (G-083) use rules only and
need no provider.
``RightsReporter`` gets the levels of ``Settings.rights_block_levels``.
``PolicyReporter`` reports through the default policy rule set catalog.
``PublishGate`` (G-078b) builds the approval, daily limit, rights, policy
(G-084) and idempotency gates over the database, with the rights levels of
``Settings.rights_block_levels`` and the default policy rule set catalog at
``Settings.policy_rule_set_version`` (an unknown version fails when it is
resolved); the kill switch gate joins it later.
"""

from datetime import timedelta

from ai_youtube_agent.content.analytics_freshness import AnalyticsFreshnessService
from ai_youtube_agent.content.approval_audit import ApprovalAuditService
from ai_youtube_agent.content.approval_service import ApprovalService
from ai_youtube_agent.content.asset_registry import AssetRegistry
from ai_youtube_agent.content.audio_artifact import AudioArtifactRecorder
from ai_youtube_agent.content.audio_cost import AudioCostRecorder
from ai_youtube_agent.content.audio_validation import AudioValidator
from ai_youtube_agent.content.auto_reply import AutoReplyGuard, AutoReplyService
from ai_youtube_agent.content.broll_manager import BrollManager
from ai_youtube_agent.content.budget_guard import BudgetGuard
from ai_youtube_agent.content.channel_metrics import ChannelMetricsSync
from ai_youtube_agent.content.channel_settings import ChannelSettings
from ai_youtube_agent.content.claim_extractor import ClaimExtractor
from ai_youtube_agent.content.comment_audit import CommentAuditService
from ai_youtube_agent.content.comment_classifier import CommentClassifier
from ai_youtube_agent.content.comment_sync import CommentSync
from ai_youtube_agent.content.contribution_margin import ContributionMarginCalculator
from ai_youtube_agent.content.cost_ledger import CostLedger
from ai_youtube_agent.content.ctr import CTRSync
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
from ai_youtube_agent.content.disclosure_decider import DisclosureDecider
from ai_youtube_agent.content.duplicate_reply_guard import DuplicateReplyGuard
from ai_youtube_agent.content.email_approval import ApprovalEmailService
from ai_youtube_agent.content.estimated_revenue import EstimatedRevenueSync
from ai_youtube_agent.content.evidence_matcher import EvidenceMatcher
from ai_youtube_agent.content.fact_checker import FactChecker
from ai_youtube_agent.content.final_revenue import FinalRevenueSync
from ai_youtube_agent.content.hook_generator import HookGenerator
from ai_youtube_agent.content.llm_cost import LLMCostRecorder
from ai_youtube_agent.content.longform_audio_mix import LongFormAudioMixer
from ai_youtube_agent.content.longform_bundle import LongFormBundleBuilder
from ai_youtube_agent.content.longform_metadata import LongFormMetadataRecorder
from ai_youtube_agent.content.longform_metrics import LongFormMetricsService
from ai_youtube_agent.content.longform_render import LongFormRenderService
from ai_youtube_agent.content.longform_script_generator import LongFormScriptGenerator
from ai_youtube_agent.content.longform_service import LongFormProjectService
from ai_youtube_agent.content.longform_subtitles import LongFormSubtitleRecorder
from ai_youtube_agent.content.longform_thumbnail import LongFormThumbnailRecorder
from ai_youtube_agent.content.originality_checker import OriginalityChecker
from ai_youtube_agent.content.policy_report import PolicyReporter
from ai_youtube_agent.content.preview_artifact import PreviewArtifactService
from ai_youtube_agent.content.preview_edit_loop import PreviewEditLoopService
from ai_youtube_agent.content.preview_metadata import PreviewMetadataService
from ai_youtube_agent.content.preview_player import PreviewPlayerService
from ai_youtube_agent.content.preview_reports import PreviewReportService
from ai_youtube_agent.content.provenance_recorder import ProvenanceRecorder
from ai_youtube_agent.content.qc_disclosure import DisclosureQCChecker
from ai_youtube_agent.content.qc_harness import VideoArtifactTestHarness
from ai_youtube_agent.content.qc_policy import PolicyQCChecker
from ai_youtube_agent.content.qc_report import QCReportBuilder
from ai_youtube_agent.content.qc_rights import RightsQCChecker
from ai_youtube_agent.content.render_cost import RenderCostRecorder
from ai_youtube_agent.content.reply_approval import ReplyApprovalService
from ai_youtube_agent.content.reply_draft import ReplyDraftService
from ai_youtube_agent.content.report_generator import ResearchReportGenerator
from ai_youtube_agent.content.retention import RetentionSync
from ai_youtube_agent.content.revenue_sources import (
    RevenueSourceService,
    RevenueStreamRegistry,
)
from ai_youtube_agent.content.rights_report import RightsReporter
from ai_youtube_agent.content.rights_risk_engine import RightsRiskEngine
from ai_youtube_agent.content.scene_planner import SceneBreakdownPlanner
from ai_youtube_agent.content.script_validator import ScriptValidator
from ai_youtube_agent.content.script_versioner import ScriptVersioner
from ai_youtube_agent.content.shorts_audio_mix import ShortsAudioMixer
from ai_youtube_agent.content.shorts_bundle import ShortsBundleBuilder
from ai_youtube_agent.content.shorts_metadata import ShortsMetadataRecorder
from ai_youtube_agent.content.shorts_metrics import ShortsMetricsService
from ai_youtube_agent.content.shorts_render import ShortsRenderService
from ai_youtube_agent.content.shorts_script_generator import ShortsScriptGenerator
from ai_youtube_agent.content.shorts_service import ShortsProjectService
from ai_youtube_agent.content.shorts_subtitles import ShortsSubtitleRecorder
from ai_youtube_agent.content.shorts_thumbnail import ShortsThumbnailRecorder
from ai_youtube_agent.content.source_collector import SourceCollector
from ai_youtube_agent.content.source_dedup import SourceDeduplicator
from ai_youtube_agent.content.storage_api_cost import StorageApiCostRecorder
from ai_youtube_agent.content.story_bible import ScriptAnalyzer
from ai_youtube_agent.content.strategy_settings import StrategySettings
from ai_youtube_agent.content.subscriber_change import SubscriberChangeService
from ai_youtube_agent.content.topic_extractor import TopicExtractor
from ai_youtube_agent.content.topic_scorer import TopicScorer
from ai_youtube_agent.content.tts_cost import TTSCostService
from ai_youtube_agent.content.version_comparison import VersionComparisonService
from ai_youtube_agent.content.video_metrics import VideoMetricsSync
from ai_youtube_agent.content.watch_time import WatchTimeService
from ai_youtube_agent.core.artifact_storage import ArtifactStorage, LocalFileStorage
from ai_youtube_agent.core.audit import AuditLog, AuditSink, InMemoryAuditSink
from ai_youtube_agent.core.backup_plan import BackupManager
from ai_youtube_agent.core.config import (
    Environment,
    RenderProviderKind,
    ResearchProviderKind,
    Settings,
    TextProviderKind,
    VoiceProviderKind,
    get_settings,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.migrate import MigrationReport, migrate
from ai_youtube_agent.core.db.repositories.audit import SqliteAuditSink
from ai_youtube_agent.core.di import Container
from ai_youtube_agent.core.failure_injection import FailureInjector
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.core.health import CheckKind, HealthCheck, HealthRegistry
from ai_youtube_agent.core.least_privilege import LeastPrivilegePolicy
from ai_youtube_agent.core.log import get_logger
from ai_youtube_agent.core.publish_gate import PublishGate
from ai_youtube_agent.core.restore_verifier import RestoreVerifier
from ai_youtube_agent.core.secret_boundary import SecretBoundaryValidator
from ai_youtube_agent.core.security_audit import SecurityAuditor
from ai_youtube_agent.core.token_storage import (
    InMemoryTokenVault,
    ManagedTokenStore,
    TokenVault,
)
from ai_youtube_agent.pipeline.approval_wait import ApprovalWaitService
from ai_youtube_agent.pipeline.crash_resume import CrashResumeService
from ai_youtube_agent.pipeline.daily_stop import DailyStopService
from ai_youtube_agent.pipeline.duplicate_guard import PublishDuplicateGuard
from ai_youtube_agent.pipeline.graceful_shutdown import GracefulShutdownService
from ai_youtube_agent.pipeline.job_queue import JobQueue, SqliteJobQueue
from ai_youtube_agent.pipeline.job_scheduler import JobScheduler
from ai_youtube_agent.pipeline.kill_switch_service import KillSwitchService
from ai_youtube_agent.pipeline.longform_publisher import LongFormPublisher
from ai_youtube_agent.pipeline.longform_unlock import LongFormUnlockService
from ai_youtube_agent.pipeline.orchestrator_audit import (
    OrchestratorAuditRecorder,
    OrchestratorAuditService,
)
from ai_youtube_agent.pipeline.pilot_runner import ShortsPilotRunner
from ai_youtube_agent.pipeline.pipeline_runner import PipelineRunner
from ai_youtube_agent.pipeline.production_audit import ProductionReadinessAuditor
from ai_youtube_agent.pipeline.publish_audit import PublishAuditService
from ai_youtube_agent.pipeline.retry_manager import JobRetryPolicy, RetryManager
from ai_youtube_agent.pipeline.publish_executor import PublishExecutor
from ai_youtube_agent.pipeline.publish_metadata_mapper import PublishMetadataMapper
from ai_youtube_agent.pipeline.publish_result import PublishResultService
from ai_youtube_agent.pipeline.script_to_video import ScriptToVideoService
from ai_youtube_agent.pipeline.session_manager import SessionManager
from ai_youtube_agent.pipeline.shorts_publisher import ShortsPublisher
from ai_youtube_agent.pipeline.upload_retry import UploadRetryPolicy
from ai_youtube_agent.providers.analytics import AnalyticsProvider
from ai_youtube_agent.providers.analytics_cache import AnalyticsCache
from ai_youtube_agent.providers.approval_email import ApprovalEmailProvider
from ai_youtube_agent.providers.comments import CommentProvider
from ai_youtube_agent.providers.ffmpeg_video_render import FfmpegVideoRenderer
from ai_youtube_agent.providers.image_generation import ImageGenerator
from ai_youtube_agent.providers.mock_analytics import MockAnalyticsProvider
from ai_youtube_agent.providers.mock_approval_email import MockApprovalEmailProvider
from ai_youtube_agent.providers.mock_comments import MockCommentProvider
from ai_youtube_agent.providers.mock_image_generation import (
    DeterministicSceneImageGenerator,
)
from ai_youtube_agent.providers.mock_research import MockResearchProvider
from ai_youtube_agent.providers.mock_revenue import MockRevenueProvider
from ai_youtube_agent.providers.mock_speech_synthesis import MockSpeechSynthesizer
from ai_youtube_agent.providers.mock_text_generation import MockTextGenerator
from ai_youtube_agent.providers.mock_video_generation import (
    UnconfiguredVideoClipGenerator,
)
from ai_youtube_agent.providers.mock_video_render import MockVideoRenderer
from ai_youtube_agent.providers.mock_youtube_channel import MockYouTubeChannelProvider
from ai_youtube_agent.providers.mock_youtube_oauth import MockYouTubeOAuthProvider
from ai_youtube_agent.providers.mock_youtube_upload import MockYouTubeUploadProvider
from ai_youtube_agent.providers.research import ResearchProvider
from ai_youtube_agent.providers.research_cache import ResearchCache
from ai_youtube_agent.providers.revenue import RevenueProvider
from ai_youtube_agent.providers.speech_synthesis import SpeechSynthesizer
from ai_youtube_agent.providers.text_generation import TextGenerator
from ai_youtube_agent.providers.video_generation import VideoClipGenerator
from ai_youtube_agent.providers.video_render import VideoRenderer
from ai_youtube_agent.providers.youtube_channel import YouTubeChannelProvider
from ai_youtube_agent.providers.youtube_oauth import YouTubeOAuthProvider
from ai_youtube_agent.providers.youtube_upload import YouTubeUploadProvider

logger = get_logger(__name__)


def build_container(settings: Settings | None = None) -> Container:
    container = Container()
    container.register_instance(Settings, settings or get_settings())
    container.register(FeatureFlags, lambda c: c.resolve(Settings).flags)
    container.register(HealthRegistry, _build_health_registry)
    container.register(Database, lambda c: Database(c.resolve(Settings).database_path))
    container.register(AuditSink, _build_audit_sink)
    container.register(AuditLog, lambda c: AuditLog(c.resolve(AuditSink)))
    container.register(
        ChannelSettings,
        lambda c: ChannelSettings(c.resolve(Database), c.resolve(AuditLog)),
    )
    container.register(
        StrategySettings,
        lambda c: StrategySettings(c.resolve(Database), c.resolve(AuditLog)),
    )
    container.register(ResearchProvider, _build_research_provider)
    container.register(
        SourceCollector,
        lambda c: SourceCollector(
            c.resolve(Database), c.resolve(ResearchProvider), c.resolve(AuditLog)
        ),
    )
    container.register(
        SourceDeduplicator, lambda c: SourceDeduplicator(c.resolve(Database))
    )
    container.register(
        TopicExtractor,
        lambda c: TopicExtractor(c.resolve(Database), c.resolve(SourceDeduplicator)),
    )
    container.register(
        TopicScorer,
        lambda c: TopicScorer(
            c.resolve(Database),
            c.resolve(SourceDeduplicator),
            c.resolve(TopicExtractor),
        ),
    )
    container.register(
        ResearchReportGenerator,
        lambda c: ResearchReportGenerator(
            c.resolve(Database), c.resolve(SourceDeduplicator), c.resolve(TopicScorer)
        ),
    )
    container.register(TextGenerator, _build_text_generator)
    container.register(
        HookGenerator,
        lambda c: HookGenerator(
            c.resolve(Database), c.resolve(TextGenerator), c.resolve(AuditLog)
        ),
    )
    container.register(
        ShortsScriptGenerator,
        lambda c: ShortsScriptGenerator(
            c.resolve(Database), c.resolve(TextGenerator), c.resolve(AuditLog)
        ),
    )
    container.register(
        LongFormScriptGenerator,
        lambda c: LongFormScriptGenerator(
            c.resolve(Database),
            c.resolve(TextGenerator),
            c.resolve(AuditLog),
            c.resolve(FeatureFlags),
        ),
    )
    container.register(
        ClaimExtractor,
        lambda c: ClaimExtractor(c.resolve(Database), c.resolve(AuditLog)),
    )
    container.register(
        EvidenceMatcher,
        lambda c: EvidenceMatcher(c.resolve(Database), c.resolve(AuditLog)),
    )
    container.register(
        FactChecker,
        lambda c: FactChecker(c.resolve(Database), c.resolve(AuditLog)),
    )
    container.register(
        OriginalityChecker,
        lambda c: OriginalityChecker(c.resolve(Database), c.resolve(AuditLog)),
    )
    container.register(
        ScriptValidator,
        lambda c: ScriptValidator(c.resolve(Database), c.resolve(AuditLog)),
    )
    container.register(
        ScriptVersioner,
        lambda c: ScriptVersioner(c.resolve(Database), c.resolve(AuditLog)),
    )
    container.register(
        AssetRegistry,
        lambda c: AssetRegistry(c.resolve(Database), c.resolve(AuditLog)),
    )
    container.register(
        ProvenanceRecorder,
        lambda c: ProvenanceRecorder(c.resolve(Database), c.resolve(AuditLog)),
    )
    container.register(
        RightsRiskEngine,
        lambda c: RightsRiskEngine(c.resolve(Database), c.resolve(AuditLog)),
    )
    container.register(
        DisclosureDecider,
        lambda c: DisclosureDecider(c.resolve(Database), c.resolve(AuditLog)),
    )
    container.register(
        RightsReporter,
        lambda c: RightsReporter(c.resolve(Database), c.resolve(Settings)),
    )
    container.register(PolicyReporter, lambda c: PolicyReporter(c.resolve(Database)))
    container.register(SpeechSynthesizer, _build_speech_synthesizer)
    container.register(
        ArtifactStorage,
        lambda c: LocalFileStorage(c.resolve(Settings).artifact_dir),
    )
    container.register(
        AudioArtifactRecorder,
        lambda c: AudioArtifactRecorder(
            c.resolve(Database), c.resolve(ArtifactStorage), c.resolve(AuditLog)
        ),
    )
    container.register(
        AudioValidator,
        lambda c: AudioValidator(
            c.resolve(Database), c.resolve(ArtifactStorage), c.resolve(AuditLog)
        ),
    )
    container.register(
        AudioCostRecorder,
        lambda c: AudioCostRecorder(
            c.resolve(Database),
            c.resolve(AuditLog),
            price_per_1k=c.resolve(Settings).tts_price_per_1k_characters,
            currency=c.resolve(Settings).tts_cost_currency,
        ),
    )
    container.register(
        ShortsProjectService,
        lambda c: ShortsProjectService(c.resolve(Database), c.resolve(AuditLog)),
    )
    container.register(
        ShortsSubtitleRecorder,
        lambda c: ShortsSubtitleRecorder(
            c.resolve(Database), c.resolve(ArtifactStorage), c.resolve(AuditLog)
        ),
    )
    container.register(
        ShortsAudioMixer,
        lambda c: ShortsAudioMixer(
            c.resolve(Database), c.resolve(ArtifactStorage), c.resolve(AuditLog)
        ),
    )
    container.register(VideoRenderer, _build_video_renderer)
    container.register(
        ShortsRenderService,
        lambda c: ShortsRenderService(
            c.resolve(Database),
            c.resolve(ArtifactStorage),
            c.resolve(AuditLog),
            c.resolve(VideoRenderer),
            c.resolve(ShortsAudioMixer),
        ),
    )
    container.register(
        ShortsThumbnailRecorder,
        lambda c: ShortsThumbnailRecorder(
            c.resolve(Database), c.resolve(ArtifactStorage), c.resolve(AuditLog)
        ),
    )
    container.register(
        ShortsMetadataRecorder,
        lambda c: ShortsMetadataRecorder(
            c.resolve(Database),
            c.resolve(ArtifactStorage),
            c.resolve(AuditLog),
        ),
    )
    container.register(
        ShortsBundleBuilder,
        lambda c: ShortsBundleBuilder(
            c.resolve(Database),
            c.resolve(ArtifactStorage),
            c.resolve(RightsReporter),
            c.resolve(PolicyReporter),
            c.resolve(AuditLog),
        ),
    )
    container.register(
        LongFormProjectService,
        lambda c: LongFormProjectService(
            c.resolve(Database), c.resolve(AuditLog), c.resolve(FeatureFlags)
        ),
    )
    container.register(
        BrollManager,
        lambda c: BrollManager(
            c.resolve(Database), c.resolve(AuditLog), c.resolve(FeatureFlags)
        ),
    )
    container.register(
        LongFormSubtitleRecorder,
        lambda c: LongFormSubtitleRecorder(
            c.resolve(Database),
            c.resolve(ArtifactStorage),
            c.resolve(AuditLog),
            c.resolve(FeatureFlags),
        ),
    )
    container.register(
        LongFormAudioMixer,
        lambda c: LongFormAudioMixer(
            c.resolve(Database),
            c.resolve(ArtifactStorage),
            c.resolve(AuditLog),
            c.resolve(FeatureFlags),
        ),
    )
    container.register(
        LongFormRenderService,
        lambda c: LongFormRenderService(
            c.resolve(Database),
            c.resolve(ArtifactStorage),
            c.resolve(AuditLog),
            c.resolve(VideoRenderer),
            c.resolve(LongFormAudioMixer),
            flags=c.resolve(FeatureFlags),
        ),
    )
    container.register(
        LongFormThumbnailRecorder,
        lambda c: LongFormThumbnailRecorder(
            c.resolve(Database),
            c.resolve(ArtifactStorage),
            c.resolve(AuditLog),
            flags=c.resolve(FeatureFlags),
        ),
    )
    container.register(
        LongFormMetadataRecorder,
        lambda c: LongFormMetadataRecorder(
            c.resolve(Database),
            c.resolve(ArtifactStorage),
            c.resolve(AuditLog),
            flags=c.resolve(FeatureFlags),
        ),
    )
    container.register(
        LongFormBundleBuilder,
        lambda c: LongFormBundleBuilder(
            c.resolve(Database),
            c.resolve(ArtifactStorage),
            c.resolve(AuditLog),
            c.resolve(Settings),
            rights_reporter=c.resolve(RightsReporter),
            policy_reporter=c.resolve(PolicyReporter),
            flags=c.resolve(FeatureFlags),
        ),
    )
    container.register(
        VideoArtifactTestHarness,
        lambda c: VideoArtifactTestHarness(
            c.resolve(Database),
            c.resolve(ArtifactStorage),
            c.resolve(AuditLog),
        ),
    )
    container.register(
        RightsQCChecker,
        lambda c: RightsQCChecker(
            c.resolve(Database),
            c.resolve(Settings),
            reporter=c.resolve(RightsReporter),
        ),
    )
    container.register(
        PolicyQCChecker,
        lambda c: PolicyQCChecker(
            c.resolve(Database),
            storage=c.resolve(ArtifactStorage),
            reporter=c.resolve(PolicyReporter),
        ),
    )
    container.register(
        DisclosureQCChecker,
        lambda c: DisclosureQCChecker(
            c.resolve(Database),
            storage=c.resolve(ArtifactStorage),
        ),
    )
    container.register(
        QCReportBuilder,
        lambda c: QCReportBuilder(
            c.resolve(Database),
            c.resolve(ArtifactStorage),
            c.resolve(AuditLog),
            c.resolve(Settings),
            rights_checker=c.resolve(RightsQCChecker),
            policy_checker=c.resolve(PolicyQCChecker),
            disclosure_checker=c.resolve(DisclosureQCChecker),
        ),
    )
    container.register(
        PreviewArtifactService,
        lambda c: PreviewArtifactService(
            c.resolve(Database), c.resolve(ArtifactStorage)
        ),
    )
    container.register(
        PreviewPlayerService,
        lambda c: PreviewPlayerService(
            c.resolve(Database),
            c.resolve(ArtifactStorage),
            c.resolve(PreviewArtifactService),
        ),
    )
    container.register(
        PreviewMetadataService,
        lambda c: PreviewMetadataService(
            c.resolve(Database),
            c.resolve(ArtifactStorage),
            c.resolve(PreviewArtifactService),
        ),
    )
    container.register(
        PreviewReportService,
        lambda c: PreviewReportService(
            c.resolve(Database),
            c.resolve(ArtifactStorage),
            c.resolve(Settings),
            rights_reporter=c.resolve(RightsReporter),
            policy_reporter=c.resolve(PolicyReporter),
        ),
    )
    container.register(
        VersionComparisonService,
        lambda c: VersionComparisonService(
            c.resolve(Database), c.resolve(ArtifactStorage)
        ),
    )
    container.register(
        PreviewEditLoopService,
        lambda c: PreviewEditLoopService(
            c.resolve(Database),
            c.resolve(ArtifactStorage),
            c.resolve(AuditLog),
            c.resolve(Settings),
            qc_builder=c.resolve(QCReportBuilder),
            preview_service=c.resolve(PreviewArtifactService),
            comparison_service=c.resolve(VersionComparisonService),
        ),
    )
    container.register(
        ApprovalEmailProvider, lambda _: MockApprovalEmailProvider()
    )
    container.register(
        ApprovalEmailService,
        lambda c: ApprovalEmailService(
            c.resolve(Database),
            c.resolve(ApprovalEmailProvider),
            c.resolve(AuditLog),
        ),
    )
    container.register(
        ApprovalService,
        lambda c: ApprovalService(
            c.resolve(Database),
            c.resolve(AuditLog),
            email_service=c.resolve(ApprovalEmailService),
            ttl=timedelta(hours=c.resolve(Settings).approval_ttl_hours),
        ),
    )
    container.register(
        ApprovalAuditService,
        lambda c: ApprovalAuditService(
            c.resolve(Database), c.resolve(AuditSink), c.resolve(AuditLog)
        ),
    )
    container.register(
        PublishGate, lambda c: PublishGate(c.resolve(Database), c.resolve(Settings))
    )
    container.register(
        YouTubeOAuthProvider, lambda _: MockYouTubeOAuthProvider()
    )
    container.register(
        YouTubeChannelProvider,
        lambda c: MockYouTubeChannelProvider(
            oauth_provider=c.resolve(YouTubeOAuthProvider)
        ),
    )
    container.register(
        YouTubeUploadProvider, lambda _: MockYouTubeUploadProvider()
    )
    container.register(
        PublishMetadataMapper, lambda _: PublishMetadataMapper()
    )
    container.register(AnalyticsProvider, lambda _: MockAnalyticsProvider())
    container.register(
        AnalyticsCache,
        lambda c: AnalyticsCache(c.resolve(AnalyticsProvider)),
    )
    container.register(
        VideoMetricsSync,
        lambda c: VideoMetricsSync(
            c.resolve(Database),
            c.resolve(AnalyticsProvider),
            c.resolve(YouTubeOAuthProvider),
            c.resolve(PublishResultService),
            c.resolve(AuditLog),
        ),
    )
    container.register(
        ChannelMetricsSync,
        lambda c: ChannelMetricsSync(
            c.resolve(Database),
            c.resolve(AnalyticsProvider),
            c.resolve(YouTubeOAuthProvider),
            c.resolve(AuditLog),
        ),
    )
    container.register(
        ShortsMetricsService,
        lambda c: ShortsMetricsService(
            c.resolve(Database), c.resolve(PublishResultService)
        ),
    )
    container.register(
        LongFormMetricsService,
        lambda c: LongFormMetricsService(
            c.resolve(Database), c.resolve(PublishResultService)
        ),
    )
    container.register(
        WatchTimeService, lambda c: WatchTimeService(c.resolve(Database))
    )
    container.register(
        RetentionSync,
        lambda c: RetentionSync(
            c.resolve(Database),
            c.resolve(AnalyticsProvider),
            c.resolve(YouTubeOAuthProvider),
            c.resolve(PublishResultService),
            c.resolve(AuditLog),
        ),
    )
    container.register(
        CTRSync,
        lambda c: CTRSync(
            c.resolve(Database),
            c.resolve(AnalyticsProvider),
            c.resolve(YouTubeOAuthProvider),
            c.resolve(PublishResultService),
            c.resolve(AuditLog),
        ),
    )
    container.register(
        SubscriberChangeService,
        lambda c: SubscriberChangeService(
            c.resolve(Database),
            c.resolve(AnalyticsProvider),
            c.resolve(YouTubeOAuthProvider),
            c.resolve(AuditLog),
        ),
    )
    container.register(
        AnalyticsFreshnessService,
        lambda c: AnalyticsFreshnessService(c.resolve(Database)),
    )
    container.register(RevenueProvider, lambda _: MockRevenueProvider())
    container.register(
        EstimatedRevenueSync,
        lambda c: EstimatedRevenueSync(
            c.resolve(Database),
            c.resolve(RevenueProvider),
            c.resolve(YouTubeOAuthProvider),
            c.resolve(AuditLog),
            results=c.resolve(PublishResultService),
        ),
    )
    container.register(
        FinalRevenueSync,
        lambda c: FinalRevenueSync(
            c.resolve(Database),
            c.resolve(RevenueProvider),
            c.resolve(YouTubeOAuthProvider),
            c.resolve(AuditLog),
            results=c.resolve(PublishResultService),
        ),
    )
    container.register(RevenueStreamRegistry, lambda _: RevenueStreamRegistry())
    container.register(
        RevenueSourceService,
        lambda c: RevenueSourceService(
            c.resolve(Database),
            c.resolve(AuditLog),
            registry=c.resolve(RevenueStreamRegistry),
            results=c.resolve(PublishResultService),
        ),
    )
    container.register(
        CostLedger,
        lambda c: CostLedger(c.resolve(Database), c.resolve(AuditLog)),
    )
    container.register(
        LLMCostRecorder,
        lambda c: LLMCostRecorder(c.resolve(CostLedger)),
    )
    container.register(
        TTSCostService,
        lambda c: TTSCostService(
            c.resolve(CostLedger),
            c.resolve(AudioCostRecorder),
            price_per_1k=c.resolve(Settings).tts_price_per_1k_characters,
            currency=c.resolve(Settings).tts_cost_currency,
        ),
    )
    container.register(
        RenderCostRecorder,
        lambda c: RenderCostRecorder(c.resolve(CostLedger)),
    )
    container.register(
        StorageApiCostRecorder,
        lambda c: StorageApiCostRecorder(c.resolve(CostLedger)),
    )
    container.register(
        ContributionMarginCalculator,
        lambda c: ContributionMarginCalculator(
            c.resolve(Database),
            c.resolve(RevenueSourceService),
            c.resolve(CostLedger),
            publish_results=c.resolve(PublishResultService),
        ),
    )
    container.register(
        BudgetGuard,
        lambda c: BudgetGuard(c.resolve(Database), c.resolve(AuditLog)),
    )
    container.register(CommentProvider, lambda _: MockCommentProvider())
    container.register(
        CommentSync,
        lambda c: CommentSync(
            c.resolve(Database),
            c.resolve(CommentProvider),
            c.resolve(YouTubeOAuthProvider),
            c.resolve(AuditLog),
        ),
    )
    container.register(
        CommentClassifier,
        lambda c: CommentClassifier(c.resolve(Database), c.resolve(AuditLog)),
    )
    container.register(
        ReplyDraftService,
        lambda c: ReplyDraftService(
            c.resolve(Database), c.resolve(TextGenerator), c.resolve(AuditLog)
        ),
    )
    container.register(
        DuplicateReplyGuard,
        lambda c: DuplicateReplyGuard(c.resolve(Database), c.resolve(AuditLog)),
    )
    container.register(
        ReplyApprovalService,
        lambda c: ReplyApprovalService(
            c.resolve(Database),
            c.resolve(CommentProvider),
            c.resolve(YouTubeOAuthProvider),
            c.resolve(AuditLog),
            duplicate_guard=c.resolve(DuplicateReplyGuard),
        ),
    )
    container.register(
        AutoReplyGuard,
        lambda c: AutoReplyGuard(c.resolve(FeatureFlags), c.resolve(AuditLog)),
    )
    container.register(
        AutoReplyService,
        lambda c: AutoReplyService(
            c.resolve(Database),
            c.resolve(ReplyDraftService),
            c.resolve(ReplyApprovalService),
            c.resolve(AuditLog),
            flags=c.resolve(FeatureFlags),
        ),
    )
    container.register(
        CommentAuditService,
        lambda c: CommentAuditService(c.resolve(Database), c.resolve(AuditSink)),
    )
    container.register(
        PublishDuplicateGuard, lambda c: PublishDuplicateGuard(c.resolve(Database))
    )
    container.register(
        PublishAuditService,
        lambda c: PublishAuditService(
            c.resolve(Database),
            c.resolve(AuditSink),
            c.resolve(ApprovalAuditService),
        ),
    )
    container.register(
        PublishResultService,
        lambda c: PublishResultService(c.resolve(Database), c.resolve(AuditSink)),
    )
    container.register(
        PublishExecutor,
        lambda c: PublishExecutor(
            c.resolve(Database),
            c.resolve(AuditLog),
            c.resolve(PublishGate),
            c.resolve(YouTubeOAuthProvider),
            c.resolve(YouTubeChannelProvider),
            c.resolve(YouTubeUploadProvider),
            retry_policy=UploadRetryPolicy(
                max_attempts=c.resolve(Settings).upload_max_attempts,
                base_delay_seconds=c.resolve(Settings).upload_retry_base_delay_seconds,
            ),
        ),
    )
    container.register(
        ShortsPublisher,
        lambda c: ShortsPublisher(
            c.resolve(Database),
            c.resolve(ArtifactStorage),
            c.resolve(PublishExecutor),
            mapper=c.resolve(PublishMetadataMapper),
            guard=c.resolve(PublishDuplicateGuard),
            flags=c.resolve(FeatureFlags),
        ),
    )
    container.register(
        LongFormPublisher,
        lambda c: LongFormPublisher(
            c.resolve(Database),
            c.resolve(ArtifactStorage),
            c.resolve(PublishExecutor),
            mapper=c.resolve(PublishMetadataMapper),
            guard=c.resolve(PublishDuplicateGuard),
            flags=c.resolve(FeatureFlags),
        ),
    )
    container.register(
        DashboardShellService,
        lambda c: DashboardShellService(c.resolve(Database)),
    )
    container.register(
        HomeScreenService,
        lambda c: HomeScreenService(
            c.resolve(Database),
            health_registry=c.resolve(HealthRegistry),
            flags=c.resolve(FeatureFlags),
            budget_guard=c.resolve(BudgetGuard),
            shell_service=c.resolve(DashboardShellService),
        ),
    )
    container.register(
        ChannelOverviewService,
        lambda c: ChannelOverviewService(
            c.resolve(Database),
            shell_service=c.resolve(DashboardShellService),
        ),
    )
    container.register(
        ContentScreenService,
        lambda c: ContentScreenService(
            c.resolve(Database),
            shell_service=c.resolve(DashboardShellService),
        ),
    )
    container.register(
        VideoDetailService,
        lambda c: VideoDetailService(
            c.resolve(Database),
            c.resolve(ArtifactStorage),
            c.resolve(Settings),
            shell_service=c.resolve(DashboardShellService),
            artifact_service=c.resolve(PreviewArtifactService),
            player_service=c.resolve(PreviewPlayerService),
            metadata_service=c.resolve(PreviewMetadataService),
            report_service=c.resolve(PreviewReportService),
            comparison_service=c.resolve(VersionComparisonService),
        ),
    )
    container.register(
        AnalyticsScreenService,
        lambda c: AnalyticsScreenService(
            c.resolve(Database),
            shell_service=c.resolve(DashboardShellService),
        ),
    )
    container.register(
        RevenueScreenService,
        lambda c: RevenueScreenService(
            c.resolve(Database),
            audit_log=c.resolve(AuditLog),
            revenue_sources=c.resolve(RevenueSourceService),
            cost_ledger=c.resolve(CostLedger),
            margin_calculator=c.resolve(ContributionMarginCalculator),
            shell_service=c.resolve(DashboardShellService),
        ),
    )
    container.register(
        CommentsScreenService,
        lambda c: CommentsScreenService(
            c.resolve(Database),
            flags=c.resolve(FeatureFlags),
            reply_approval_service=c.resolve(ReplyApprovalService),
            shell_service=c.resolve(DashboardShellService),
        ),
    )
    container.register(
        AICenterScreenService,
        lambda c: AICenterScreenService(
            c.resolve(Database),
            settings=c.resolve(Settings),
            cost_ledger=c.resolve(CostLedger),
            shell_service=c.resolve(DashboardShellService),
        ),
    )
    container.register(
        ApprovalCenterService,
        lambda c: ApprovalCenterService(
            c.resolve(Database),
            approval_service=c.resolve(ApprovalService),
            shell_service=c.resolve(DashboardShellService),
            ttl=timedelta(hours=c.resolve(Settings).approval_ttl_hours),
        ),
    )
    container.register(
        SettingsScreenService,
        lambda c: SettingsScreenService(
            c.resolve(Database),
            settings=c.resolve(Settings),
            flags=c.resolve(FeatureFlags),
            shell_service=c.resolve(DashboardShellService),
        ),
    )
    container.register(
        AlertsScreenService,
        lambda c: AlertsScreenService(
            c.resolve(Database),
            budget_guard=c.resolve(BudgetGuard),
            health_registry=c.resolve(HealthRegistry),
            shell_service=c.resolve(DashboardShellService),
        ),
    )
    container.register(
        KillSwitchService,
        lambda c: KillSwitchService(
            c.resolve(AuditLog),
            database=c.resolve(Database),
        ),
    )
    container.register(
        JobQueue,
        lambda c: SqliteJobQueue(c.resolve(Database)),
    )
    container.register(
        JobScheduler,
        lambda c: JobScheduler(
            c.resolve(Database),
            c.resolve(JobQueue),
            c.resolve(AuditLog),
            budget_guard=c.resolve(BudgetGuard),
            kill_switch=c.resolve(KillSwitchService),
            flags=c.resolve(FeatureFlags),
        ),
    )
    container.register(
        PipelineRunner,
        lambda c: PipelineRunner(
            c.resolve(Database),
            c.resolve(Settings),
            c.resolve(AuditLog),
            flags=c.resolve(FeatureFlags),
            publish_gate=c.resolve(PublishGate),
            qc_builder=c.resolve(QCReportBuilder),
            shorts_publisher=c.resolve(ShortsPublisher),
            longform_publisher=c.resolve(LongFormPublisher),
            kill_switch=c.resolve(KillSwitchService),
        ),
    )
    container.register(
        ApprovalWaitService,
        lambda c: ApprovalWaitService(
            c.resolve(Database),
            c.resolve(AuditLog),
            queue=c.resolve(JobQueue),
        ),
    )
    container.register(
        RetryManager,
        lambda c: RetryManager(
            c.resolve(Database),
            c.resolve(AuditLog),
            policy=JobRetryPolicy(
                max_attempts=c.resolve(Settings).job_max_attempts,
                base_delay_seconds=float(c.resolve(Settings).job_retry_base_delay_seconds),
                max_delay_seconds=float(c.resolve(Settings).job_retry_max_delay_seconds),
            ),
            queue=c.resolve(JobQueue),
        ),
    )
    container.register(
        SessionManager,
        lambda c: SessionManager(
            c.resolve(Database),
            c.resolve(AuditLog),
        ),
    )
    container.register(
        DailyStopService,
        lambda c: DailyStopService(
            c.resolve(Database),
            c.resolve(AuditLog),
        ),
    )
    container.register(
        CrashResumeService,
        lambda c: CrashResumeService(
            c.resolve(Database),
            c.resolve(AuditLog),
            queue=c.resolve(JobQueue),
            max_attempts=c.resolve(Settings).job_max_attempts,
        ),
    )
    container.register(
        GracefulShutdownService,
        lambda c: GracefulShutdownService(
            c.resolve(Database),
            c.resolve(JobQueue),
            c.resolve(AuditLog),
        ),
    )
    container.register(
        OrchestratorAuditRecorder,
        lambda c: OrchestratorAuditRecorder(c.resolve(AuditLog)),
    )
    container.register(
        OrchestratorAuditService,
        lambda c: OrchestratorAuditService(
            c.resolve(Database),
            c.resolve(AuditSink),
        ),
    )
    container.register(
        SecretBoundaryValidator,
        lambda _c: SecretBoundaryValidator(),
    )
    container.register(
        TokenVault,
        lambda _c: InMemoryTokenVault(),
    )
    container.register(
        ManagedTokenStore,
        lambda c: ManagedTokenStore(
            c.resolve(TokenVault),
            c.resolve(YouTubeOAuthProvider),
            c.resolve(AuditLog),
        ),
    )
    container.register(
        LeastPrivilegePolicy,
        lambda _c: LeastPrivilegePolicy(),
    )
    container.register(
        BackupManager,
        lambda c: BackupManager(
            c.resolve(Settings).database_path,
            c.resolve(Settings).artifact_dir,
            c.resolve(Settings).database_path.parent / "backups",
            audit=c.resolve(AuditLog),
        ),
    )
    container.register(
        RestoreVerifier,
        lambda c: RestoreVerifier(audit=c.resolve(AuditLog)),
    )
    container.register(
        FailureInjector,
        lambda _c: FailureInjector(),
    )
    container.register(
        SecurityAuditor,
        lambda _c: SecurityAuditor(),
    )
    container.register(
        ShortsPilotRunner,
        lambda c: ShortsPilotRunner(
            c.resolve(Database),
            c.resolve(AuditLog),
            shorts_publisher=c.resolve(ShortsPublisher),
        ),
    )
    container.register(
        LongFormUnlockService,
        lambda c: LongFormUnlockService(
            c.resolve(Database),
            c.resolve(AuditLog),
            pilot_runner=c.resolve(ShortsPilotRunner),
            kill_switch=c.resolve(KillSwitchService),
            security_auditor=c.resolve(SecurityAuditor),
            settings=c.resolve(Settings),
        ),
    )
    container.register(
        ProductionReadinessAuditor,
        lambda c: ProductionReadinessAuditor(
            c.resolve(Settings),
            audit_log=c.resolve(AuditLog),
            security_auditor=c.resolve(SecurityAuditor),
            health_registry=c.resolve(HealthRegistry),
            kill_switch=c.resolve(KillSwitchService),
        ),
    )
    container.register(
        ImageGenerator,
        lambda _c: DeterministicSceneImageGenerator(),
    )
    container.register(
        VideoClipGenerator,
        lambda _c: UnconfiguredVideoClipGenerator(),
    )
    container.register(
        ScriptAnalyzer,
        lambda _c: ScriptAnalyzer(),
    )
    container.register(
        SceneBreakdownPlanner,
        lambda _c: SceneBreakdownPlanner(),
    )
    container.register(
        ScriptToVideoService,
        lambda c: ScriptToVideoService(
            c.resolve(Database),
            c.resolve(ArtifactStorage),
            c.resolve(AuditLog),
            speech_synthesizer=c.resolve(SpeechSynthesizer),
            image_generator=c.resolve(ImageGenerator),
            video_clip_generator=c.resolve(VideoClipGenerator),
            video_renderer=c.resolve(VideoRenderer),
            script_analyzer=c.resolve(ScriptAnalyzer),
            scene_planner=c.resolve(SceneBreakdownPlanner),
            asset_registry=c.resolve(AssetRegistry),
            provenance_recorder=c.resolve(ProvenanceRecorder),
            rights_engine=c.resolve(RightsRiskEngine),
            disclosure_decider=c.resolve(DisclosureDecider),
            audio_recorder=c.resolve(AudioArtifactRecorder),
            audio_validator=c.resolve(AudioValidator),
            broll_manager=c.resolve(BrollManager),
            shorts_service=c.resolve(ShortsProjectService),
            shorts_subtitles=c.resolve(ShortsSubtitleRecorder),
            shorts_thumbnail=c.resolve(ShortsThumbnailRecorder),
            shorts_metadata=c.resolve(ShortsMetadataRecorder),
            shorts_render=c.resolve(ShortsRenderService),
            shorts_bundle=c.resolve(ShortsBundleBuilder),
            longform_service=c.resolve(LongFormProjectService),
            longform_subtitles=c.resolve(LongFormSubtitleRecorder),
            longform_thumbnail=c.resolve(LongFormThumbnailRecorder),
            longform_metadata=c.resolve(LongFormMetadataRecorder),
            longform_render=c.resolve(LongFormRenderService),
            longform_bundle=c.resolve(LongFormBundleBuilder),
            qc_builder=c.resolve(QCReportBuilder),
            preview_service=c.resolve(PreviewArtifactService),
            approval_service=c.resolve(ApprovalService),
        ),
    )
    return container


def _build_research_provider(container: Container) -> ResearchProvider:
    kind = container.resolve(Settings).research_provider
    if kind is ResearchProviderKind.MOCK:
        provider: ResearchProvider = MockResearchProvider()
    else:
        raise ValueError(f"unknown research provider {kind!r}")
    # #061: every provider is used through the SQLite research cache.
    return ResearchCache(provider, container.resolve(Database))


def _build_text_generator(container: Container) -> TextGenerator:
    kind = container.resolve(Settings).text_provider
    if kind is TextProviderKind.MOCK:
        return MockTextGenerator()
    raise ValueError(f"unknown text provider {kind!r}")


def _build_speech_synthesizer(container: Container) -> SpeechSynthesizer:
    kind = container.resolve(Settings).voice_provider
    if kind is VoiceProviderKind.MOCK:
        return MockSpeechSynthesizer()
    raise ValueError(f"unknown voice provider {kind!r}")


def _build_video_renderer(container: Container) -> VideoRenderer:
    settings = container.resolve(Settings)
    kind = settings.render_provider
    if kind is RenderProviderKind.MOCK:
        return MockVideoRenderer()
    if kind is RenderProviderKind.FFMPEG:
        return FfmpegVideoRenderer(
            binary=settings.ffmpeg_binary,
            timeout_s=settings.render_timeout_seconds,
            font_file=settings.render_font_file,
        )
    raise ValueError(f"unknown render provider {kind!r}")


def _build_audit_sink(container: Container) -> AuditSink:
    # Tests keep events in memory; the application stores them in SQLite (#030).
    if container.resolve(Settings).environment is Environment.TEST:
        return InMemoryAuditSink()
    return SqliteAuditSink(container.resolve(Database))


def prepare_database(settings: Settings | Container) -> MigrationReport:
    """Bring the database up to date. Called once when the application starts."""
    resolved_settings = settings.resolve(Settings) if isinstance(settings, Container) else settings
    report = migrate(resolved_settings.database_path)
    logger.info(
        "database ready",
        extra={
            "fields": {
                "database_path": str(resolved_settings.database_path),
                "version": report.current_version,
                "applied": list(report.applied),
                "backup_path": str(report.backup_path) if report.backup_path else None,
            }
        },
    )
    return report


def _build_health_registry(container: Container) -> HealthRegistry:
    registry = HealthRegistry()
    registry.register(
        HealthCheck(
            "settings", CheckKind.APPLICATION, lambda: container.resolve(Settings)
        )
    )
    registry.register(
        HealthCheck(
            "feature_flags",
            CheckKind.APPLICATION,
            lambda: container.resolve(FeatureFlags),
        )
    )
    registry.register(
        HealthCheck(
            "research_provider",
            CheckKind.PROVIDER,
            lambda: container.resolve(ResearchProvider).check(),
        )
    )
    registry.register(
        HealthCheck(
            "text_provider",
            CheckKind.PROVIDER,
            lambda: container.resolve(TextGenerator).check(),
        )
    )
    registry.register(
        HealthCheck(
            "voice_provider",
            CheckKind.PROVIDER,
            lambda: container.resolve(SpeechSynthesizer).check(),
        )
    )
    registry.register(
        HealthCheck(
            "render_provider",
            CheckKind.PROVIDER,
            lambda: container.resolve(VideoRenderer).check(),
        )
    )
    return registry
