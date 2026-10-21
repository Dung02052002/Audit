"""Critical State & Artifact Backup Plan (Prompt Pack v8, prompt #220), area X4 Security & Recovery.

Creates atomic, checksummed backups of both the SQLite database (using SQLite's
online backup API) and the local artifact file tree, accompanied by a verifiable
``manifest.json`` inventory.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from ai_youtube_agent.core.audit import Actor, ActorKind, AuditLog, AuditResult, EntityRef
from ai_youtube_agent.core.errors import DomainError

Clock = Callable[[], datetime]
MANIFEST_FILENAME: Final[str] = "manifest.json"
DB_BACKUP_FILENAME: Final[str] = "database.sqlite3"
ARTIFACTS_SUBDIR: Final[str] = "artifacts"
SYSTEM_BACKUP_ACTOR: Final[Actor] = Actor(ActorKind.SYSTEM, "backup_manager")


class BackupError(DomainError):
    """Raised when creating or inspecting a backup bundle fails."""

    def __init__(
        self,
        detail: str,
        *,
        code: str = "domain.backup_error",
        user_message: str = "Backup operation failed.",
    ) -> None:
        super().__init__(detail, code=code, user_message=user_message)


@dataclass(frozen=True)
class BackupFileEntry:
    """Checksum and size metadata for one backed-up file."""

    relative_path: str
    sha256: str
    size_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class BackupManifest:
    """Complete manifest of a database + artifact backup bundle."""

    backup_id: str
    created_at: datetime
    database_entry: BackupFileEntry
    artifact_entries: tuple[BackupFileEntry, ...]
    table_row_counts: dict[str, int]

    @property
    def total_artifact_bytes(self) -> int:
        return sum(e.size_bytes for e in self.artifact_entries)

    def as_dict(self) -> dict[str, Any]:
        return {
            "backup_id": self.backup_id,
            "created_at": self.created_at.isoformat(),
            "database_entry": self.database_entry.as_dict(),
            "artifact_entries": [e.as_dict() for e in self.artifact_entries],
            "table_row_counts": dict(self.table_row_counts),
            "total_artifact_bytes": self.total_artifact_bytes,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> BackupManifest:
        db_raw = raw["database_entry"]
        return cls(
            backup_id=raw["backup_id"],
            created_at=datetime.fromisoformat(raw["created_at"]),
            database_entry=BackupFileEntry(
                relative_path=db_raw["relative_path"],
                sha256=db_raw["sha256"],
                size_bytes=int(db_raw["size_bytes"]),
            ),
            artifact_entries=tuple(
                BackupFileEntry(
                    relative_path=item["relative_path"],
                    sha256=item["sha256"],
                    size_bytes=int(item["size_bytes"]),
                )
                for item in raw.get("artifact_entries", [])
            ),
            table_row_counts={
                str(k): int(v) for k, v in raw.get("table_row_counts", {}).items()
            },
        )


def compute_file_sha256(path: Path) -> tuple[str, int]:
    """Return ``(hex_sha256, size_bytes)`` for ``path``."""
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as fh:
        while chunk := fh.read(65536):
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


def _inspect_table_counts(db_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(str(db_path))
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {
            t: int(conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0])
            for t in tables
        }
    finally:
        conn.close()


class BackupManager:
    """Creates and verifies full backups of the SQLite database and artifact directory (#220)."""

    def __init__(
        self,
        database_path: Path,
        artifact_dir: Path,
        backup_root: Path,
        *,
        audit: AuditLog | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._db_path = Path(database_path)
        self._artifact_dir = Path(artifact_dir)
        self._backup_root = Path(backup_root)
        self._audit = audit
        self._clock: Clock = clock or (lambda: datetime.now(UTC))

    def create_backup(
        self,
        *,
        label: str = "scheduled",
        actor: Actor = SYSTEM_BACKUP_ACTOR,
    ) -> tuple[Path, BackupManifest]:
        """Create a consistent backup bundle and return ``(bundle_dir, manifest)``."""
        if not self._db_path.is_file():
            raise BackupError(
                f"Database file does not exist: {self._db_path}",
                code="domain.backup_database_missing",
            )
        cleaned_label = "".join(
            ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in label.strip()
        )
        if not cleaned_label:
            cleaned_label = "backup"

        now = self._clock()
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        backup_id = f"bkp_{stamp}_{cleaned_label}"
        bundle_dir = self._backup_root / backup_id
        if bundle_dir.exists():
            raise BackupError(
                f"Backup directory already exists: {bundle_dir}",
                code="domain.backup_already_exists",
            )
        bundle_dir.mkdir(parents=True, exist_ok=False)

        # 1. Online SQLite backup
        dest_db = bundle_dir / DB_BACKUP_FILENAME
        src_conn = sqlite3.connect(str(self._db_path))
        dest_conn = sqlite3.connect(str(dest_db))
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
            src_conn.close()

        db_sha, db_size = compute_file_sha256(dest_db)
        db_entry = BackupFileEntry(
            relative_path=DB_BACKUP_FILENAME,
            sha256=db_sha,
            size_bytes=db_size,
        )
        table_counts = _inspect_table_counts(dest_db)

        # 2. Copy artifact tree and record SHA-256 per file
        dest_artifacts_root = bundle_dir / ARTIFACTS_SUBDIR
        dest_artifacts_root.mkdir(parents=True, exist_ok=True)
        artifact_entries: list[BackupFileEntry] = []

        if self._artifact_dir.is_dir():
            for src_file in sorted(self._artifact_dir.rglob("*")):
                if not src_file.is_file() or src_file.name.endswith(".tmp"):
                    continue
                rel = src_file.relative_to(self._artifact_dir)
                dest_file = dest_artifacts_root / rel
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dest_file)
                sha, size = compute_file_sha256(dest_file)
                rel_posix = (Path(ARTIFACTS_SUBDIR) / rel).as_posix()
                artifact_entries.append(
                    BackupFileEntry(
                        relative_path=rel_posix,
                        sha256=sha,
                        size_bytes=size,
                    )
                )

        manifest = BackupManifest(
            backup_id=backup_id,
            created_at=now,
            database_entry=db_entry,
            artifact_entries=tuple(artifact_entries),
            table_row_counts=table_counts,
        )
        manifest_path = bundle_dir / MANIFEST_FILENAME
        manifest_path.write_text(
            json.dumps(manifest.as_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )

        if self._audit is not None:
            self._audit.record(
                action="backup.created",
                actor=actor,
                entity=EntityRef("backup", backup_id),
                result=AuditResult.SUCCESS,
                metadata={
                    "backup_id": backup_id,
                    "database_bytes": db_size,
                    "artifact_count": len(artifact_entries),
                    "total_artifact_bytes": manifest.total_artifact_bytes,
                },
            )

        return bundle_dir, manifest

    @staticmethod
    def load_manifest(bundle_dir: Path) -> BackupManifest:
        manifest_path = Path(bundle_dir) / MANIFEST_FILENAME
        if not manifest_path.is_file():
            raise BackupError(
                f"Backup manifest missing at {manifest_path}",
                code="domain.backup_manifest_missing",
            )
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            return BackupManifest.from_dict(raw)
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
            raise BackupError(
                f"Backup manifest is corrupt: {exc}",
                code="domain.backup_manifest_corrupt",
            ) from exc
