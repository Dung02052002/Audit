"""The migrated template database that normal tests copy (TOOL-003)."""

import sqlite3
import stat
from contextlib import closing
from pathlib import Path

import pytest

import conftest  # importable: prepend import mode, no tests/__init__.py
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.migrate import default_migrations, migrate


def migration_versions(path: Path) -> list[int]:
    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    return [version for (version,) in rows]


def table_names(database: Database) -> set[str]:
    with closing(database.connect()) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master").fetchall()
    return {name for (name,) in rows}


def test_two_databases_are_distinct_files_and_do_not_share_writes(
    database: Database, tmp_path: Path, database_copy
) -> None:
    other = Database(database_copy(tmp_path / "other.db"))

    with closing(database.connect()) as connection:
        connection.execute("CREATE TABLE only_in_the_first (id INTEGER)")

    assert database.path == tmp_path / "test.db"
    assert other.path != database.path
    assert "only_in_the_first" in table_names(database)
    assert "only_in_the_first" not in table_names(other)


def test_a_write_in_one_test_is_gone_in_the_next_test_a(database: Database) -> None:
    with closing(database.connect()) as connection:
        connection.execute("CREATE TABLE leaked_between_tests (id INTEGER)")


def test_a_write_in_one_test_is_gone_in_the_next_test_b(database: Database) -> None:
    assert "leaked_between_tests" not in table_names(database)


def test_the_template_has_every_migration_recorded(
    template_database: Path,
) -> None:
    expected = [migration.version for migration in default_migrations()]

    assert migration_versions(template_database) == expected
    assert len(expected) >= 22


def test_migrate_on_a_copy_has_nothing_to_apply(tmp_path: Path, database_copy) -> None:
    path = database_copy(tmp_path / "copy.db")

    report = migrate(path)

    assert report.applied == ()
    assert report.current_version == len(default_migrations())
    assert report.backup_path is None
    assert not (tmp_path / "backups").exists()


def test_the_database_fixture_does_not_run_migrations(
    template_database: Path,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Path] = []

    def counting_migrate(path: Path, *args, **kwargs):
        calls.append(path)
        return migrate(path, *args, **kwargs)

    monkeypatch.setattr(conftest, "migrate", counting_migrate)
    monkeypatch.setattr("ai_youtube_agent.core.db.migrate.migrate", counting_migrate)

    # The template is built before the patch (it is a parameter); the real
    # ``database`` fixture is then requested with the patch in place.
    request.getfixturevalue("database")

    assert calls == []


def test_the_database_fixture_is_a_copy_of_the_template(
    database: Database, template_database: Path
) -> None:
    assert database.path != template_database
    assert database.path.read_bytes() == template_database.read_bytes()


def test_the_template_is_read_only(template_database: Path) -> None:
    assert not template_database.stat().st_mode & stat.S_IWRITE
    with closing(sqlite3.connect(template_database)) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE stray (id INTEGER)")
        names = {
            name for (name,) in connection.execute("SELECT name FROM sqlite_master")
        }
    assert "stray" not in names


def test_a_fresh_empty_file_still_migrates_the_whole_chain(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    path.touch()

    report = migrate(path)

    versions = [migration.version for migration in default_migrations()]
    assert report.applied == tuple(versions)
    assert migration_versions(path) == versions
