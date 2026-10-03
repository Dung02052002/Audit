-- F-072 Script Validator (Prompt Pack v8, prompt #072).
-- One row per script validation run, at most one per script version: the
-- content item of the script, the method used (free text, 'script-rules-v1'
-- for now), the words and the estimated seconds of the script, whether the
-- language could be checked (language_checked, 1 or 0) and how many language
-- signal words were read (at most 20000), the strategy version the rules read
-- and the one the script was written from (when it has one), the number of
-- findings and how many are WARN and FAIL, who asked and when. A status for
-- the script is derived from the counts, not stored: no finding is a pass,
-- and a run whose language was not checked is not a language pass.
-- One finding row per thing the rules found: its status (a finding is never a
-- pass), the code of the rule that gave it, the section it points to, a count
-- or a length (actual, minimum, maximum: seconds, sections, words or
-- sentences, by the code), a score from 0 to 1 and a count (matched), each
-- when the rule has one. No script text and no banned phrase is stored.
-- Rows are never changed: a result is a record, not a gate, and no override
-- exists yet (a later task may add a separate table for overrides).
-- The code is open text here, with only a length check: the closed list of
-- codes lives in Python (ValidationCode), so a later rule version can add a
-- code without rebuilding the table, as SQLite cannot change a CHECK in
-- place and migrations only move forward. 'method' is open for the same
-- reason. The limits on language hits and findings are the Python constants
-- MAX_WORDS and MAX_FINDINGS.

CREATE TABLE script_validations (
    id TEXT PRIMARY KEY,
    script_id TEXT NOT NULL UNIQUE REFERENCES scripts (id),
    content_item_id TEXT NOT NULL REFERENCES content_items (id),
    method TEXT NOT NULL CHECK (length(trim(method)) BETWEEN 1 AND 100),
    words INTEGER NOT NULL CHECK (words >= 0),
    seconds INTEGER NOT NULL CHECK (seconds >= 0),
    language_checked INTEGER NOT NULL CHECK (language_checked IN (0, 1)),
    language_hits INTEGER NOT NULL CHECK (language_hits BETWEEN 0 AND 20000),
    strategy_version INTEGER NOT NULL CHECK (strategy_version >= 1),
    script_strategy_version INTEGER CHECK (script_strategy_version IS NULL OR script_strategy_version >= 1),
    findings_count INTEGER NOT NULL CHECK (findings_count BETWEEN 0 AND 210),
    warn_count INTEGER NOT NULL CHECK (warn_count >= 0),
    fail_count INTEGER NOT NULL CHECK (fail_count >= 0),
    requested_by_kind TEXT NOT NULL CHECK (requested_by_kind IN ('user', 'system', 'ai')),
    requested_by_id TEXT NOT NULL,
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    CHECK (warn_count + fail_count = findings_count)
) STRICT;

CREATE INDEX script_validations_by_item ON script_validations (content_item_id, created_at);

CREATE TABLE script_validation_findings (
    id TEXT PRIMARY KEY,
    validation_id TEXT NOT NULL REFERENCES script_validations (id),
    status TEXT NOT NULL CHECK (status IN ('pass', 'warn', 'fail')),
    code TEXT NOT NULL CHECK (length(trim(code)) BETWEEN 1 AND 100),
    section_index INTEGER CHECK (section_index IS NULL OR section_index >= 0),
    actual INTEGER CHECK (actual IS NULL OR actual >= 0),
    minimum INTEGER CHECK (minimum IS NULL OR minimum >= 0),
    maximum INTEGER CHECK (maximum IS NULL OR maximum >= 0),
    score REAL CHECK (score IS NULL OR score BETWEEN 0 AND 1),
    matched INTEGER CHECK (matched IS NULL OR matched >= 0),
    CHECK (status <> 'pass'),
    CHECK (minimum IS NULL OR maximum IS NULL OR minimum <= maximum)
) STRICT;

CREATE INDEX script_validation_findings_by_validation ON script_validation_findings (validation_id);
