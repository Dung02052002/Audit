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

from ai_youtube_agent.content.asset_registry import AssetRegistry
from ai_youtube_agent.content.audio_artifact import AudioArtifactRecorder
from ai_youtube_agent.content.audio_cost import AudioCostRecorder
from ai_youtube_agent.content.audio_validation import AudioValidator
from ai_youtube_agent.content.broll_manager import BrollManager
from ai_youtube_agent.content.channel_settings import ChannelSettings
from ai_youtube_agent.content.claim_extractor import ClaimExtractor
from ai_youtube_agent.content.disclosure_decider import DisclosureDecider
from ai_youtube_agent.content.evidence_matcher import EvidenceMatcher
from ai_youtube_agent.content.fact_checker import FactChecker
from ai_youtube_agent.content.hook_generator import HookGenerator
from ai_youtube_agent.content.longform_audio_mix import LongFormAudioMixer
from ai_youtube_agent.content.longform_bundle import LongFormBundleBuilder
from ai_youtube_agent.content.longform_metadata import LongFormMetadataRecorder
from ai_youtube_agent.content.longform_render import LongFormRenderService
from ai_youtube_agent.content.longform_script_generator import LongFormScriptGenerator
from ai_youtube_agent.content.longform_service import LongFormProjectService
from ai_youtube_agent.content.longform_subtitles import LongFormSubtitleRecorder
from ai_youtube_agent.content.longform_thumbnail import LongFormThumbnailRecorder
from ai_youtube_agent.content.qc_harness import VideoArtifactTestHarness
from ai_youtube_agent.content.originality_checker import OriginalityChecker
from ai_youtube_agent.content.policy_report import PolicyReporter
from ai_youtube_agent.content.provenance_recorder import ProvenanceRecorder
from ai_youtube_agent.content.report_generator import ResearchReportGenerator
from ai_youtube_agent.content.rights_report import RightsReporter
from ai_youtube_agent.content.rights_risk_engine import RightsRiskEngine
from ai_youtube_agent.content.script_validator import ScriptValidator
from ai_youtube_agent.content.script_versioner import ScriptVersioner
from ai_youtube_agent.content.shorts_audio_mix import ShortsAudioMixer
from ai_youtube_agent.content.shorts_bundle import ShortsBundleBuilder
from ai_youtube_agent.content.shorts_metadata import ShortsMetadataRecorder
from ai_youtube_agent.content.shorts_render import ShortsRenderService
from ai_youtube_agent.content.shorts_script_generator import ShortsScriptGenerator
from ai_youtube_agent.content.shorts_service import ShortsProjectService
from ai_youtube_agent.content.shorts_subtitles import ShortsSubtitleRecorder
from ai_youtube_agent.content.shorts_thumbnail import ShortsThumbnailRecorder
from ai_youtube_agent.content.source_collector import SourceCollector
from ai_youtube_agent.content.source_dedup import SourceDeduplicator
from ai_youtube_agent.content.strategy_settings import StrategySettings
from ai_youtube_agent.content.topic_extractor import TopicExtractor
from ai_youtube_agent.content.topic_scorer import TopicScorer
from ai_youtube_agent.core.artifact_storage import ArtifactStorage, LocalFileStorage
from ai_youtube_agent.core.audit import AuditLog, AuditSink, InMemoryAuditSink
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
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.core.health import CheckKind, HealthCheck, HealthRegistry
from ai_youtube_agent.core.log import get_logger
from ai_youtube_agent.core.publish_gate import PublishGate
from ai_youtube_agent.providers.ffmpeg_video_render import FfmpegVideoRenderer
from ai_youtube_agent.providers.mock_research import MockResearchProvider
from ai_youtube_agent.providers.mock_speech_synthesis import MockSpeechSynthesizer
from ai_youtube_agent.providers.mock_text_generation import MockTextGenerator
from ai_youtube_agent.providers.mock_video_render import MockVideoRenderer
from ai_youtube_agent.providers.research import ResearchProvider
from ai_youtube_agent.providers.research_cache import ResearchCache
from ai_youtube_agent.providers.speech_synthesis import SpeechSynthesizer
from ai_youtube_agent.providers.text_generation import TextGenerator
from ai_youtube_agent.providers.video_render import VideoRenderer

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
            c.resolve(Settings),
        ),
    )
    container.register(
        ShortsBundleBuilder,
        lambda c: ShortsBundleBuilder(
            c.resolve(Database),
            c.resolve(ArtifactStorage),
            c.resolve(AuditLog),
            c.resolve(Settings),
            c.resolve(RightsReporter),
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
        PublishGate, lambda c: PublishGate(c.resolve(Database), c.resolve(Settings))
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


def prepare_database(settings: Settings) -> MigrationReport:
    """Bring the database up to date. Called once when the application starts."""
    report = migrate(settings.database_path)
    logger.info(
        "database ready",
        extra={
            "fields": {
                "database_path": str(settings.database_path),
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
