"""Migration runner (Prompt Pack v8, prompt #029).

Migrations are numbered SQL files in ``migrations/``, named like
``0001_initial_schema.sql``. Versions start at 1 and have no gaps. They are
forward-only: a mistake is fixed by a new migration, never by editing an
applied file.

``migrate`` brings a database up to date:

1. It records applied migrations in ``schema_migrations`` (version, name,
   sha256 of the file and UTC ``applied_at``) and refuses to run if an applied
   file has changed, an applied version has no file, or the database has
   tables it did not create.
2. Before applying pending migrations to a database that already has a
   version, it copies the database with the SQLite backup API to
   ``<database folder>/backups/<name>.<UTC time>.v<version>.db``. Backups are
   never deleted automatically.
3. It applies each pending migration in its own transaction. On any error the
   transaction is rolled back, so the database stays at the last good version.

To undo a migration that has already committed, restore a backup with
``restore_backup``. Migrations run only when the application starts
(``bootstrap.prepare_database``), never on import. A migration file must not
contain transaction control (``BEGIN``, ``COMMIT``, ``ROLLBACK``, ``END``) or
``PRAGMA`` statements: the runner owns both.
"""

import hashlib
import re
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

from ai_youtube_agent.core.db.codec import format_datetime
from ai_youtube_agent.core.errors import ApplicationError

MIGRATION_FILE_PATTERN = re.compile(r"^(\d{4})_([a-z0-9]+(?:_[a-z0-9]+)*)\.sql$")
FORBIDDEN_STATEMENTS = ("BEGIN", "COMMIT", "ROLLBACK", "END", "PRAGMA")
BACKUP_STAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"
Clock = Callable[[], datetime]

SCHEMA_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version >= 1),
    name TEXT NOT NULL,
    checksum TEXT NOT NULL CHECK (length(checksum) = 64),
    applied_at TEXT NOT NULL
) STRICT
"""


class MigrationError(ApplicationError):
    default_code = "application.migration_failed"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str

    @classmethod
    def from_text(cls, filename: str, text: str) -> "Migration":
        match = MIGRATION_FILE_PATTERN.match(filename)
        if not match:
            raise MigrationError(
                f"migration file {filename!r} must be named like "
                "'0001_initial_schema.sql'"
            )
        version = int(match.group(1))
        if version < 1:
            raise MigrationError("migration versions start at 1")
        checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return cls(version, match.group(2), text, checksum)


@dataclass(frozen=True)
class MigrationReport:
    applied: tuple[int, ...]
    current_version: int
    backup_path: Path | None


def default_migrations() -> tuple[Migration, ...]:
    return load_migrations(files("ai_youtube_agent.core.db") / "migrations")


def load_migrations(directory: Traversable | Path) -> tuple[Migration, ...]:
    migrations = []
    for entry in directory.iterdir():
        if not entry.name.endswith(".sql"):
            continue
        text = entry.read_bytes().decode("utf-8")
        migrations.append(Migration.from_text(entry.name, text))
    migrations.sort(key=lambda migration: migration.version)
    versions = [migration.version for migration in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(
            f"migration versions must run 1, 2, 3 without gaps or repeats: {versions}"
        )
    return tuple(migrations)


def connect(database_path: Path) -> sqlite3.Connection:
    """Open a connection with foreign keys on and manual transactions."""
    connection = sqlite3.connect(database_path, isolation_level=None)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def current_version(database_path: Path) -> int:
    """Return the applied version, 0 for a missing or unmigrated database."""
    if not Path(database_path).is_file():
        return 0
    connection = connect(database_path)
    try:
        if not _has_table(connection, "schema_migrations"):
            return 0
        row = connection.execute("SELECT max(version) FROM schema_migrations")
        return row.fetchone()[0] or 0
    finally:
        connection.close()


def migrate(
    database_path: Path | str,
    *,
    migrations: Iterable[Migration] | None = None,
    clock: Clock | None = None,
) -> MigrationReport:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    migrations = tuple(default_migrations() if migrations is None else migrations)
    connection = connect(path)
    try:
        applied = _applied(connection)
        _verify(applied, migrations)
        version = max(applied, default=0)
        pending = [m for m in migrations if m.version not in applied]
        backup_path = None
        if pending and version > 0:
            backup_path = backup_database(connection, path, version, clock=clock)
        for migration in pending:
            _apply(connection, migration, clock)
            version = migration.version
    finally:
        connection.close()
    return MigrationReport(
        applied=tuple(m.version for m in pending),
        current_version=version,
        backup_path=backup_path,
    )


def backup_database(
    connection: sqlite3.Connection,
    database_path: Path,
    version: int,
    *,
    clock: Clock | None = None,
) -> Path:
    stamp = _now(clock).strftime(BACKUP_STAMP_FORMAT)
    backups = database_path.parent / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    target = backups / f"{database_path.stem}.{stamp}.v{version:04d}.db"
    if target.exists():
        raise MigrationError(f"backup {target} already exists")
    destination = sqlite3.connect(target)
    try:
        connection.backup(destination)
    finally:
        destination.close()
    return target


def restore_backup(backup_path: Path, database_path: Path) -> None:
    """Replace the contents of ``database_path`` with a backup."""
    if not backup_path.is_file():
        raise MigrationError(f"backup {backup_path} does not exist")
    source = sqlite3.connect(backup_path)
    try:
        if not _has_table(source, "schema_migrations"):
            raise MigrationError(f"{backup_path} is not a migrated database")
        destination = sqlite3.connect(database_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()


def _applied(connection: sqlite3.Connection) -> dict[int, str]:
    if not _has_table(connection, "schema_migrations"):
        tables = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if tables:
            raise MigrationError(
                "the database has tables but no schema_migrations table, so it "
                "was not created by these migrations"
            )
        connection.execute(SCHEMA_MIGRATIONS_TABLE)
        return {}
    rows = connection.execute("SELECT version, checksum FROM schema_migrations")
    return dict(rows.fetchall())


def _verify(applied: dict[int, str], migrations: tuple[Migration, ...]) -> None:
    known = {migration.version: migration for migration in migrations}
    for version, checksum in sorted(applied.items()):
        migration = known.get(version)
        if migration is None:
            raise MigrationError(
                f"the database has migration {version}, but there is no file for it"
            )
        if migration.checksum != checksum:
            raise MigrationError(
                f"migration {version} ({migration.name}) was changed after it "
                "was applied; add a new migration instead"
            )


def _apply(
    connection: sqlite3.Connection, migration: Migration, clock: Clock | None
) -> None:
    statements = list(_statements(migration))
    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
            "VALUES (?, ?, ?, ?)",
            (
                migration.version,
                migration.name,
                migration.checksum,
                format_datetime(_now(clock)),
            ),
        )
        connection.execute("COMMIT")
    except Exception as error:
        connection.execute("ROLLBACK")
        raise MigrationError(
            f"migration {migration.version} ({migration.name}) failed and was "
            f"rolled back: {error}"
        ) from error


def _statements(migration: Migration) -> Iterator[str]:
    buffer = ""
    for line in migration.sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            buffer = ""
            first = _first_keyword(statement)
            if first in FORBIDDEN_STATEMENTS:
                raise MigrationError(
                    f"migration {migration.version} must not use {first}; "
                    "the runner controls transactions and pragmas"
                )
            yield statement
    if _first_keyword(buffer):
        raise MigrationError(
            f"migration {migration.version} ends with an incomplete statement"
        )


def _first_keyword(statement: str) -> str:
    for line in statement.splitlines():
        line = line.strip()
        if line and not line.startswith("--"):
            return line.split()[0].rstrip(";").upper()
    return ""


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _now(clock: Clock | None) -> datetime:
    return clock() if clock else datetime.now(UTC)
