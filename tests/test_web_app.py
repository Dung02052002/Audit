"""Tests for the APP-01 static web app served by FastAPI at /app."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import ai_youtube_agent
from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.main import create_app
from ai_youtube_agent.web import STATIC_DIR

IMPORT_PATTERN = re.compile(r"""^\s*import\s[^;]*?from\s+["']([^"']+)["']""", re.M)
JS_FILES = sorted(STATIC_DIR.rglob("*.js"))


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        environment=Environment.TEST,
        database_path=tmp_path / "web.db",
        artifact_dir=tmp_path / "artifacts",
    )
    return TestClient(create_app(build_container(settings)))


def test_the_root_redirects_to_the_app(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/app/"


def test_the_app_page_is_served(client: TestClient) -> None:
    response = client.get("/app/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'src="js/app.js"' in response.text
    assert 'type="module"' in response.text
    assert 'lang="vi"' in response.text


@pytest.mark.parametrize(
    ("path", "media_type"),
    [
        ("/app/js/app.js", "text/javascript"),
        ("/app/js/views/run.js", "text/javascript"),
        ("/app/css/app.css", "text/css"),
        ("/app/css/design_system.css", "text/css"),
    ],
)
def test_assets_have_browser_media_types(
    client: TestClient, path: str, media_type: str
) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(media_type)


def test_a_missing_asset_is_404(client: TestClient) -> None:
    response = client.get("/app/missing.js")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "request.not_found"


def test_every_imported_module_exists() -> None:
    assert JS_FILES
    for source in JS_FILES:
        for target in IMPORT_PATTERN.findall(source.read_text(encoding="utf-8")):
            assert target.startswith("./") or target.startswith("../"), target
            assert (source.parent / target).resolve().is_file(), (source, target)


def test_the_page_references_existing_files() -> None:
    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    for reference in re.findall(r'(?:src|href)="((?:css|js)/[^"]+)"', page):
        assert (STATIC_DIR / reference).is_file(), reference


@pytest.mark.parametrize("source", JS_FILES, ids=lambda path: path.name)
def test_scripts_never_inject_html_or_offer_publishing(source: Path) -> None:
    text = source.read_text(encoding="utf-8")

    for forbidden in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "/publish",
        "/approvals",
        "/edit-loop",
    ):
        assert forbidden not in text, forbidden


def test_the_static_files_live_inside_the_package() -> None:
    package = Path(ai_youtube_agent.__file__).resolve().parent

    assert STATIC_DIR.is_relative_to(package)
    assert (STATIC_DIR / "index.html").is_file()
