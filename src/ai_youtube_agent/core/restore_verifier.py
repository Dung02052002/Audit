"""Backup Restore & Non-Production Verification (Prompt Pack v8, prompt #221), area X4 Security & Recovery.

Restores a backup bundle into an isolated target database and artifact directory
and runs comprehensive integrity, foreign-key, row-count, and SHA-256 checksum
verification.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from ai_youtube_agent.core.audit import Actor, ActorKind, AuditLog, AuditResult, EntityRef
from ai_youtube_agent.core.backup_plan import (
    ARTIFACTS_SUBDIR,
    BackupManager,
    BackupManifest,
    _inspect_table_counts,
    compute_file_sha256,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import ArtifactRepository
from ai_youtube_agent.core.errors import DomainError

Clock = Callable[[], datetime]
SYSTEM_RESTORE_ACTOR: Final[Actor] = Actor(ActorKind.SYSTEM, "restore_verifier")


class RestoreVerificationError(DomainError):
    """Raised when a backup bundle fails pre-restore or post-restore verification."""

    def __init__(
        self,
        detail: str,
        *,
        code: str = "domain.restore_verification_error",
        user_message: str = "Backup restoration or verification failed.",
    ) -> None:
        super().__init__(detail, code=code, user_message=user_message)


@dataclass(frozen=True)
class RestoreVerificationReport:
    """Detailed verification report for a restored backup bundle."""

    backup_id: str
    restored_at: datetime
    restored_database_path: Path
    restored_artifact_dir: Path
    sqlite_integrity_ok: bool
    foreign_keys_ok: bool
    row_counts_matched: bool
    verified_artifact_files: int
    verified_db_artifact_rows: int

    @property
    def passed(self) -> bool:
        return (
            self.sqlite_integrity_ok
            and self.foreign_keys_ok
            and self.row_counts_matched
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "backup_id": self.backup_id,
            "restored_at": self.restored_at.isoformat(),
            "restored_database_path": str(self.restored_database_path),
            "restored_artifact_dir": str(self.restored_artifact_dir),
            "passed": self.passed,
            "sqlite_integrity_ok": self.sqlite_integrity_ok,
            "foreign_keys_ok": self.foreign_keys_ok,
            "row_counts_matched": self.row_counts_matched,
            "verified_artifact_files": self.verified_artifact_files,
            "verified_db_artifact_rows": self.verified_db_artifact_rows,
        }


class RestoreVerifier:
    """Executes and verifies backup restoration in non-production / recovery environments (#221)."""

    def __init__(
        self,
        *,
        audit: AuditLog | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._audit = audit
        self._clock: Clock = clock or (lambda: datetime.now(UTC))

    def verify_bundle(self, bundle_dir: Path) -> BackupManifest:
        """Verify all files in ``bundle_dir`` match ``manifest.json`` SHA-256 digests."""
        bundle_path = Path(bundle_dir)
        manifest = BackupManager.load_manifest(bundle_path)

        db_file = bundle_path / manifest.database_entry.relative_path
        if not db_file.is_file():
            raise RestoreVerificationError(
                f"Database snapshot missing in bundle: {db_file}",
                code="domain.restore_file_missing",
            )
        db_sha, db_size = compute_file_sha256(db_file)
        if (
            db_sha != manifest.database_entry.sha256
            or db_size != manifest.database_entry.size_bytes
        ):
            raise RestoreVerificationError(
                "Database snapshot SHA-256 or byte size mismatch against manifest.",
                code="domain.restore_checksum_mismatch",
            )

        for entry in manifest.artifact_entries:
            art_file = bundle_path / entry.relative_path
            if not art_file.is_file():
                raise RestoreVerificationError(
                    f"Artifact file missing in bundle: {entry.relative_path}",
                    code="domain.restore_file_missing",
                )
            sha, size = compute_file_sha256(art_file)
            if sha != entry.sha256 or size != entry.size_bytes:
                raise RestoreVerificationError(
                    f"Artifact SHA-256 mismatch for {entry.relative_path}.",
                    code="domain.restore_checksum_mismatch",
                )

        return manifest

    def restore_and_verify(
        self,
        bundle_dir: Path,
        *,
        target_database_path: Path,
        target_artifact_dir: Path,
        actor: Actor = SYSTEM_RESTORE_ACTOR,
    ) -> RestoreVerificationReport:
        """Verify bundle, restore DB + artifacts to target paths, and validate restored state."""
        bundle_path = Path(bundle_dir)
        manifest = self.verify_bundle(bundle_path)
        now = self._clock()

        target_db = Path(target_database_path)
        target_art = Path(target_artifact_dir)
        target_db.parent.mkdir(parents=True, exist_ok=True)
        target_art.mkdir(parents=True, exist_ok=True)

        # 1. Restore SQLite database via online backup API
        src_db = bundle_path / manifest.database_entry.relative_path
        src_conn = sqlite3.connect(str(src_db))
        dest_conn = sqlite3.connect(str(target_db))
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
            src_conn.close()

        # 2. Restore artifact files and re-verify checksums on target disk
        prefix = f"{ARTIFACTS_SUBDIR}/"
        for entry in manifest.artifact_entries:
            rel_inside = (
                entry.relative_path[len(prefix) :]
                if entry.relative_path.startswith(prefix)
                else entry.relative_path
            )
            src_file = bundle_path / entry.relative_path
            dest_file = target_art / rel_inside
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest_file)
            sha, size = compute_file_sha256(dest_file)
            if sha != entry.sha256 or size != entry.size_bytes:
                raise RestoreVerificationError(
                    f"Restored artifact checksum mismatch at {dest_file}",
                    code="domain.restore_checksum_mismatch",
                )

        # 3. Run SQLite integrity_check, foreign_key_check, and row count comparison
        check_conn = sqlite3.connect(str(target_db))
        try:
            integrity_rows = check_conn.execute("PRAGMA integrity_check").fetchall()
            sqlite_integrity_ok = integrity_rows == [("ok",)]
            fk_rows = check_conn.execute("PRAGMA foreign_key_check").fetchall()
            foreign_keys_ok = len(fk_rows) == 0
        finally:
            check_conn.close()

        if not sqlite_integrity_ok or not foreign_keys_ok:
            raise RestoreVerificationError(
                "Restored SQLite database failed PRAGMA integrity_check or foreign_key_check.",
                code="domain.restore_db_integrity_failed",
            )

        restored_counts = _inspect_table_counts(target_db)
        row_counts_matched = restored_counts == manifest.table_row_counts
        if not row_counts_matched:
            raise RestoreVerificationError(
                "Restored table row counts do not match backup manifest.",
                code="domain.restore_row_count_mismatch",
            )

        # 4. Verify stored Artifact rows in restored DB match restored artifact digests
        restored_db = Database(target_db)
        manifest_shas = {e.sha256 for e in manifest.artifact_entries}
        verified_db_rows = 0
        with restored_db.connection() as conn:
            cursor = conn.execute("SELECT id FROM content_items")
            item_ids = [r[0] for r in cursor.fetchall()]
            art_repo = ArtifactRepository(conn)
            for item_id in item_ids:
                for art in art_repo.list_by_content_item(item_id):
                    if art.sha256 not in manifest_shas:
                        raise RestoreVerificationError(
                            f"Artifact row {art.id} (sha256={art.sha256}) missing from restored files.",
                            code="domain.restore_artifact_row_unmatched",
                        )
                    verified_db_rows += 1

        report = RestoreVerificationReport(
            backup_id=manifest.backup_id,
            restored_at=now,
            restored_database_path=target_db,
            restored_artifact_dir=target_art,
            sqlite_integrity_ok=sqlite_integrity_ok,
            foreign_keys_ok=foreign_keys_ok,
            row_counts_matched=row_counts_matched,
            verified_artifact_files=len(manifest.artifact_entries),
            verified_db_artifact_rows=verified_db_rows,
        )

        if self._audit is not None:
            self._audit.record(
                action="backup.restore_verified",
                actor=actor,
                entity=EntityRef("backup", manifest.backup_id),
                result=AuditResult.SUCCESS,
                metadata={
                    "backup_id": manifest.backup_id,
                    "verified_artifact_files": len(manifest.artifact_entries),
                    "verified_db_artifact_rows": verified_db_rows,
                },
            )

        return report
