"""Database access (Prompt Pack v8, prompt #030).

``Database`` opens SQLite connections for one file, with foreign keys on and
manual transactions. ``transaction()`` yields a connection inside
``BEGIN IMMEDIATE`` and commits when the block ends, or rolls back if it
raises, so writes through several repositories are atomic::

    with database.transaction() as connection:
        ApprovalRequestRepository(connection).add(request)

Repositories take that connection. They never commit on their own.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ai_youtube_agent.core.db.migrate import connect
from ai_youtube_agent.core.errors import ApplicationError


class PersistenceError(ApplicationError):
    default_code = "application.persistence"


class RecordNotFoundError(PersistenceError):
    default_code = "application.record_not_found"


class ConcurrencyError(PersistenceError):
    """The stored row changed since it was read, so the update was refused."""

    default_code = "application.concurrency_conflict"


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        return connect(self.path)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
        finally:
            connection.close()
