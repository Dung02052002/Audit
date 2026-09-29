"""Composition root: the only module that chooses implementations.

Every other module depends on interfaces and gets them from the container.
Later prompts register their provider interfaces here, choosing the real or
mock implementation from ``Settings``, and add a ``CheckKind.PROVIDER`` health
check for each provider to the ``HealthRegistry``.

``prepare_database`` applies the database migrations (#029). The application
calls it once at startup, from the FastAPI lifespan in ``main.create_app``.
"""

from ai_youtube_agent.core.audit import AuditLog, AuditSink, InMemoryAuditSink
from ai_youtube_agent.core.config import Settings, get_settings
from ai_youtube_agent.core.db.migrate import MigrationReport, migrate
from ai_youtube_agent.core.di import Container
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.core.health import CheckKind, HealthCheck, HealthRegistry
from ai_youtube_agent.core.log import get_logger

logger = get_logger(__name__)


def build_container(settings: Settings | None = None) -> Container:
    container = Container()
    container.register_instance(Settings, settings or get_settings())
    container.register(FeatureFlags, lambda c: c.resolve(Settings).flags)
    container.register(HealthRegistry, _build_health_registry)
    # In memory until persistence (#029) provides a database sink.
    container.register(AuditSink, lambda _: InMemoryAuditSink())
    container.register(AuditLog, lambda c: AuditLog(c.resolve(AuditSink)))
    return container


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
    return registry
