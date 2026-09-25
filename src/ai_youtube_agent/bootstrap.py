"""Composition root: the only module that chooses implementations.

Every other module depends on interfaces and gets them from the container.
Later prompts register their provider interfaces here, choosing the real or
mock implementation from ``Settings``.
"""

from ai_youtube_agent.core.config import Settings, get_settings
from ai_youtube_agent.core.di import Container
from ai_youtube_agent.core.flags import FeatureFlags


def build_container(settings: Settings | None = None) -> Container:
    container = Container()
    container.register_instance(Settings, settings or get_settings())
    container.register(FeatureFlags, lambda c: c.resolve(Settings).flags)
    return container
