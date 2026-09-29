from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, TypeVar

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from ai_youtube_agent import __version__
from ai_youtube_agent.bootstrap import build_container, prepare_database
from ai_youtube_agent.core.config import Settings
from ai_youtube_agent.core.di import Container
from ai_youtube_agent.core.health import HealthRegistry
from ai_youtube_agent.core.log import configure_logging

T = TypeVar("T")


def provide(interface: type[T]) -> Any:
    """FastAPI dependency that resolves ``interface`` from the app's container.

    Use it as ``service: Annotated[Service, provide(Service)]``.
    """

    def resolve(request: Request) -> T:
        return request.app.state.container.resolve(interface)

    return Depends(resolve)


def health(
    registry: Annotated[HealthRegistry, provide(HealthRegistry)],
) -> JSONResponse:
    report = registry.run(__version__)
    return JSONResponse(report.as_dict(), status_code=report.http_status)


def create_app(container: Container | None = None) -> FastAPI:
    container = container or build_container()
    settings = container.resolve(Settings)
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Migrations run when the server starts, never on import (#029).
        prepare_database(settings)
        yield

    app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)
    app.state.container = container
    app.get("/health")(health)
    return app


app = create_app()
