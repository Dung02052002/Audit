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
``ClaimExtractor`` (#068), ``EvidenceMatcher`` (#069), ``FactChecker``
(#070), ``OriginalityChecker`` (#071), ``ScriptValidator`` (#072),
``ScriptVersioner`` (#073), ``AssetRegistry`` (#076), ``ProvenanceRecorder``
(#077) and ``RightsRiskEngine`` (#078) use rules only and need no provider.
``PublishGate`` (G-078b) builds the approval, daily limit, rights and
idempotency gates over the database, with the rights levels of
``Settings.rights_block_levels``; the policy and kill switch gates join it later.
"""

from ai_youtube_agent.content.asset_registry import AssetRegistry
from ai_youtube_agent.content.channel_settings import ChannelSettings
from ai_youtube_agent.content.claim_extractor import ClaimExtractor
from ai_youtube_agent.content.evidence_matcher import EvidenceMatcher
from ai_youtube_agent.content.fact_checker import FactChecker
from ai_youtube_agent.content.hook_generator import HookGenerator
from ai_youtube_agent.content.longform_script_generator import LongFormScriptGenerator
from ai_youtube_agent.content.originality_checker import OriginalityChecker
from ai_youtube_agent.content.provenance_recorder import ProvenanceRecorder
from ai_youtube_agent.content.report_generator import ResearchReportGenerator
from ai_youtube_agent.content.rights_risk_engine import RightsRiskEngine
from ai_youtube_agent.content.script_validator import ScriptValidator
from ai_youtube_agent.content.script_versioner import ScriptVersioner
from ai_youtube_agent.content.shorts_script_generator import ShortsScriptGenerator
from ai_youtube_agent.content.source_collector import SourceCollector
from ai_youtube_agent.content.source_dedup import SourceDeduplicator
from ai_youtube_agent.content.strategy_settings import StrategySettings
from ai_youtube_agent.content.topic_extractor import TopicExtractor
from ai_youtube_agent.content.topic_scorer import TopicScorer
from ai_youtube_agent.core.audit import AuditLog, AuditSink, InMemoryAuditSink
from ai_youtube_agent.core.config import (
    Environment,
    ResearchProviderKind,
    Settings,
    TextProviderKind,
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
from ai_youtube_agent.providers.mock_research import MockResearchProvider
from ai_youtube_agent.providers.mock_text_generation import MockTextGenerator
from ai_youtube_agent.providers.research import ResearchProvider
from ai_youtube_agent.providers.research_cache import ResearchCache
from ai_youtube_agent.providers.text_generation import TextGenerator

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
    return registry
