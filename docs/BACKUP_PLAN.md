# Backup & Recovery Plan (S-220)

This document specifies the backup architecture and recovery procedures for `ai_youtube_agent` implemented in `src/ai_youtube_agent/core/backup_plan.py` and verified in `src/ai_youtube_agent/core/restore_verifier.py` (`S-221`).

## 1. Critical State Inventory

1. **SQLite Database (`Settings.database_path`, default `data/ai_youtube_agent.db`)**
   - Contains all channels, strategy profiles, voice profiles, content items, scripts, claims, evidence, rights records, QC results, approval requests, publish jobs, analytics snapshots, revenue & cost records, comments, sessions, AI jobs, checkpoints, and immutable `audit_events`.
2. **Artifact Storage Tree (`Settings.artifact_dir`, default `data/artifacts/`)**
   - Contains immutable versioned media and metadata files (`v1.wav`, `v1.srt`, `v1.mp4`, `v1.png`, `v1.json`) referenced by `artifacts.uri` and bound to human approvals via SHA-256 digests.
3. **Pre-Migration Automatic Backups (`data/backups/`)**
   - Created automatically by `core/db/migrate.py` before applying any pending SQL migration.

## 2. Backup Bundle Format

Each backup created by `BackupManager.create_backup()` produces an immutable directory `bkp_<UTC_TIMESTAMP>_<LABEL>/` containing:

- `database.sqlite3`: Consistent online snapshot captured via `sqlite3.Connection.backup()`.
- `artifacts/`: Full copy of all committed artifact files preserving relative directory paths (`<content_item_id>/<kind>/v<version>.<ext>`).
- `manifest.json`: Verifiable inventory recording:
  - `backup_id` and UTC `created_at`
  - `database_entry` (`relative_path`, `sha256`, `size_bytes`)
  - `artifact_entries` (`relative_path`, `sha256`, `size_bytes` for every artifact file)
  - `table_row_counts` across all SQLite tables

## 3. Restore & Verification Procedure (`S-221`)

`RestoreVerifier.restore_and_verify()` performs:

1. **Pre-Restore Manifest Verification**: Recomputes SHA-256 and byte size for `database.sqlite3` and every file in `artifacts/` before touching target paths. Any mismatch raises `RestoreVerificationError` (`domain.restore_checksum_mismatch`).
2. **Database & Artifact Restoration**: Restores the SQLite database via `sqlite3.Connection.backup()` and copies verified artifact files into the target `artifact_dir`.
3. **Post-Restore Integrity Checks**:
   - Executes `PRAGMA integrity_check` and `PRAGMA foreign_key_check` on the restored database.
   - Compares restored table row counts against `manifest.table_row_counts`.
   - Verifies every row in the `artifacts` table has its corresponding file present in the restored `artifact_dir` with matching SHA-256 checksum and byte size.
