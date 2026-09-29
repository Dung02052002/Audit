from pathlib import Path

import pytest

from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.migrate import migrate


@pytest.fixture
def database(tmp_path: Path) -> Database:
    """A migrated SQLite database in a temporary folder."""
    path = tmp_path / "test.db"
    migrate(path)
    return Database(path)
