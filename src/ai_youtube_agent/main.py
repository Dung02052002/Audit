from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ai_youtube_agent import __version__
from ai_youtube_agent.bootstrap import build_container, prepare_database
from ai_youtube_agent.content.channel_api import router as channel_router
from ai_youtube_agent.core.config import Settings
from ai_youtube_agent.core.di import Container
from ai_youtube_agent.core.health import HealthRegistry
from ai_youtube_agent.core.http import install_error_handlers, provide
from ai_youtube_agent.core.log import configure_logging


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
    install_error_handlers(app)
    app.get("/health")(health)
    app.include_router(channel_router)
    return app


app = create_app()
