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
health check.
"""

from ai_youtube_agent.content.channel_settings import ChannelSettings
from ai_youtube_agent.content.source_collector import SourceCollector
from ai_youtube_agent.content.source_dedup import SourceDeduplicator
from ai_youtube_agent.content.strategy_settings import StrategySettings
from ai_youtube_agent.core.audit import AuditLog, AuditSink, InMemoryAuditSink
from ai_youtube_agent.core.config import (
    Environment,
    ResearchProviderKind,
    Settings,
    get_settings,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.migrate import MigrationReport, migrate
from ai_youtube_agent.core.db.repositories.audit import SqliteAuditSink
from ai_youtube_agent.core.di import Container
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.core.health import CheckKind, HealthCheck, HealthRegistry
from ai_youtube_agent.core.log import get_logger
from ai_youtube_agent.providers.mock_research import MockResearchProvider
from ai_youtube_agent.providers.research import ResearchProvider

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
    return container


def _build_research_provider(container: Container) -> ResearchProvider:
    kind = container.resolve(Settings).research_provider
    if kind is ResearchProviderKind.MOCK:
        return MockResearchProvider()
    raise ValueError(f"unknown research provider {kind!r}")


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
    return registry
