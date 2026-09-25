import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", ["core", "content", "providers", "pipeline"])
def test_backend_area_is_importable_package(name: str) -> None:
    module = importlib.import_module(f"ai_youtube_agent.{name}")

    assert module.__doc__


@pytest.mark.parametrize("folder", ["dashboard", "docs", "tests"])
def test_top_level_folder_exists(folder: str) -> None:
    assert (REPO_ROOT / folder).is_dir()
