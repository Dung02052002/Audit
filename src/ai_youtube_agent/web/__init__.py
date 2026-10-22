"""Web: the local web app of the Command Center (APP-01), context X3.

Q4 was answered for APP-01: the Command Center is a web app served by
FastAPI. It is static HTML, vanilla JavaScript ES modules and CSS in
``static/`` (shipped inside the package), mounted at ``/app``; ``/`` redirects
there. The app reads the HTTP API and sends only one user action, starting a
background script-to-video run. It never publishes, approves or uploads, and
it owns no domain rules.
"""

import mimetypes
from pathlib import Path
from typing import Final

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

STATIC_DIR: Final = Path(__file__).resolve().parent / "static"
APP_PATH: Final = "/app"


def install_web_app(app: FastAPI) -> None:
    """Mount the static web app at ``/app`` and redirect ``/`` to it."""
    # The Windows registry may map .js to text/plain, which browsers refuse
    # for ES modules.
    mimetypes.add_type("text/javascript", ".js")
    mimetypes.add_type("text/css", ".css")

    @app.get("/", include_in_schema=False)
    def open_web_app() -> RedirectResponse:
        return RedirectResponse(f"{APP_PATH}/", status_code=307)

    app.mount(APP_PATH, StaticFiles(directory=STATIC_DIR, html=True), name="web")
