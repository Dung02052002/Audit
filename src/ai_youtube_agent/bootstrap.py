"""Composition root: the only module that chooses implementations.

Every other module depends on interfaces and gets them from the container.
Later prompts register their provider interfaces here, choosing the real or
mock implementation from ``Settings``, and add a ``CheckKind.PROVIDER`` health
check for each provider to the ``HealthRegistry``.
"""

from ai_youtube_agent.core.audit import AuditLog, AuditSink, InMemoryAuditSink
from ai_youtube_agent.core.config import Settings, get_settings
from ai_youtube_agent.core.di import Container
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.core.health import CheckKind, HealthCheck, HealthRegistry


def build_container(settings: Settings | None = None) -> Container:
    container = Container()
    container.register_instance(Settings, settings or get_settings())
    container.register(FeatureFlags, lambda c: c.resolve(Settings).flags)
    container.register(HealthRegistry, _build_health_registry)
    # In memory until persistence (#029) provides a database sink.
    container.register(AuditSink, lambda _: InMemoryAuditSink())
    container.register(AuditLog, lambda c: AuditLog(c.resolve(AuditSink)))
    return container


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
