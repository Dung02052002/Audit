from typing import Any, TypeVar

from fastapi import Depends, FastAPI, Request

from ai_youtube_agent import __version__
from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.core.config import Settings
from ai_youtube_agent.core.di import Container
from ai_youtube_agent.core.log import configure_logging

T = TypeVar("T")


def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


def create_app(container: Container | None = None) -> FastAPI:
    container = container or build_container()
    settings = container.resolve(Settings)
    configure_logging(settings.log_level)

    app = FastAPI(title=settings.app_name, version=__version__)
    app.state.container = container
    app.get("/health")(health)
    return app


def provide(interface: type[T]) -> Any:
    """FastAPI dependency that resolves ``interface`` from the app's container.

    Use it as ``service: Annotated[Service, provide(Service)]``.
    """

    def resolve(request: Request) -> T:
        return request.app.state.container.resolve(interface)

    return Depends(resolve)


app = create_app()
