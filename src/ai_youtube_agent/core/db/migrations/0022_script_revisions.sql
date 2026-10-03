-- F-073 Script Versioning (Prompt Pack v8, prompt #073).
-- One row per script revision, at most one per script version: the diff
-- metadata of a version (2 or later) against its parent version. Version 1 has
-- no parent and so no revision. A row holds the script and its parent, the
-- content item, the method used (free text, 'script-diff-v1' for now), the
-- version number, how many aligned sections were added, removed, changed and
-- unchanged (entries_count is their sum, at most 200: every old section
-- removed and every new section added), the words and the estimated seconds of
-- the parent (before) and of the version (after), the SHA-256 of the whole
-- script of the version, who asked and when.
-- One entry row per aligned section: how it changed (added, removed, changed
-- or unchanged), its kind, its index in the parent (old_index, none for an
-- added section) and in the version (new_index, none for a removed one), flags
-- for what changed in a changed section (text, title, estimated seconds), the
-- change in words, and optionally the SHA-256 of the section. No script text
-- is stored.
-- Rows are never changed: a revision is derived data, a record, not a gate.
-- 'method' and 'change' are open text here, with only a length check: the
-- closed lists live in Python (ChangeKind), so a later method can add a value
-- without rebuilding the table, as SQLite cannot change a CHECK in place and
-- migrations only move forward. The checks on a known change value are
-- written as 'change <> value OR ...', so an unknown value is only held to the
-- length check. The limit on entries is the Python constant MAX_ENTRIES and
-- the section indexes are held to MAX_SECTIONS (100). The version is held to
-- MAX_VERSION (1,000,000), as in scripts.

CREATE TABLE script_revisions (
    id TEXT PRIMARY KEY,
    script_id TEXT NOT NULL UNIQUE REFERENCES scripts (id),
    parent_script_id TEXT NOT NULL REFERENCES scripts (id),
    content_item_id TEXT NOT NULL REFERENCES content_items (id),
    method TEXT NOT NULL CHECK (length(trim(method)) BETWEEN 1 AND 100),
    version INTEGER NOT NULL CHECK (version BETWEEN 2 AND 1000000),
    added_count INTEGER NOT NULL CHECK (added_count >= 0),
    removed_count INTEGER NOT NULL CHECK (removed_count >= 0),
    changed_count INTEGER NOT NULL CHECK (changed_count >= 0),
    unchanged_count INTEGER NOT NULL CHECK (unchanged_count >= 0),
    entries_count INTEGER NOT NULL CHECK (entries_count BETWEEN 0 AND 200),
    words_before INTEGER NOT NULL CHECK (words_before >= 0),
    words_after INTEGER NOT NULL CHECK (words_after >= 0),
    seconds_before INTEGER NOT NULL CHECK (seconds_before >= 0),
    seconds_after INTEGER NOT NULL CHECK (seconds_after >= 0),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
    requested_by_kind TEXT NOT NULL CHECK (requested_by_kind IN ('user', 'system', 'ai')),
    requested_by_id TEXT NOT NULL,
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    CHECK (script_id <> parent_script_id),
    CHECK (added_count + removed_count + changed_count + unchanged_count = entries_count)
) STRICT;

CREATE INDEX script_revisions_by_item ON script_revisions (content_item_id, created_at);

CREATE TABLE script_revision_sections (
    id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES script_revisions (id),
    change TEXT NOT NULL CHECK (length(trim(change)) BETWEEN 1 AND 100),
    kind TEXT NOT NULL CHECK (kind IN ('hook', 'intro', 'body', 'chapter', 'outro', 'cta')),
    old_index INTEGER CHECK (old_index IS NULL OR old_index BETWEEN 0 AND 99),
    new_index INTEGER CHECK (new_index IS NULL OR new_index BETWEEN 0 AND 99),
    text_changed INTEGER NOT NULL CHECK (text_changed IN (0, 1)),
    title_changed INTEGER NOT NULL CHECK (title_changed IN (0, 1)),
    seconds_changed INTEGER NOT NULL CHECK (seconds_changed IN (0, 1)),
    words_delta INTEGER NOT NULL,
    sha256 TEXT CHECK (sha256 IS NULL OR (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*')),
    CHECK (change <> 'added' OR (old_index IS NULL AND new_index IS NOT NULL AND words_delta >= 0)),
    CHECK (change <> 'removed' OR (old_index IS NOT NULL AND new_index IS NULL AND words_delta <= 0)),
    CHECK (change NOT IN ('changed', 'unchanged') OR (old_index IS NOT NULL AND new_index IS NOT NULL)),
    CHECK (change NOT IN ('added', 'removed', 'unchanged') OR text_changed + title_changed + seconds_changed = 0),
    CHECK (change <> 'unchanged' OR words_delta = 0),
    CHECK (change <> 'changed' OR text_changed + title_changed + seconds_changed >= 1),
    CHECK (change <> 'changed' OR text_changed = 1 OR words_delta = 0),
    UNIQUE (revision_id, old_index),
    UNIQUE (revision_id, new_index)
) STRICT;

CREATE INDEX script_revision_sections_by_revision ON script_revision_sections (revision_id);
