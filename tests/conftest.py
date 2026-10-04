import os
import shutil
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.migrate import migrate


@pytest.fixture(scope="session")
def template_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A fully migrated database, built once per test session.

    Tests never open it: they get a copy (``database``, ``database_copy``).
    It is read-only so a stray write fails loudly. Tests that exercise the
    migration chain itself must not use it; they migrate a fresh file.
    """
    path = tmp_path_factory.mktemp("template") / "template.db"
    migrate(path)
    os.chmod(path, stat.S_IREAD)
    return path


@pytest.fixture
def database_copy(template_database: Path) -> Callable[[Path], Path]:
    """Copy the migrated template to ``path`` and return ``path``."""

    def copy(path: Path) -> Path:
        shutil.copyfile(template_database, path)
        return path

    return copy


@pytest.fixture
def database(tmp_path: Path, database_copy: Callable[[Path], Path]) -> Database:
    """A migrated SQLite database in a temporary folder."""
    return Database(database_copy(tmp_path / "test.db"))
