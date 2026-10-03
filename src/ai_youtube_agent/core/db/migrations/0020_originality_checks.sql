-- F-071 Originality Check (Prompt Pack v8, prompt #071).
-- One row per originality check run, at most one per script version: the
-- content item of the script, the method used (free text,
-- 'originality-rules-v1' for now), the number of words read (the words of the
-- script outside CTA sections, at most 20000), the number of prior scripts
-- compared (at most 50), the number of findings and how many are WARN and
-- FAIL, who asked and when. A status for the script is derived from the
-- counts, not stored: no finding is a pass.
-- One finding row per thing the rules found: its status (a finding is never a
-- pass), the code of the rule that gave it, the prior script and its content
-- item when the finding is about one (both or neither), the section it points
-- to, a score from 0 to 1 and a count, each when the rule has one. No script
-- text is stored, only ids, scores and counts.
-- Rows are never changed: a result is a record, not a gate, and no override
-- exists yet (a later task may add a separate table for overrides).
-- The code is open text here, with only a length check: the closed list of
-- codes lives in Python (OriginalityCode), so a later rule version can add a
-- code without rebuilding the table, as SQLite cannot change a CHECK in
-- place and migrations only move forward. 'method' is open for the same
-- reason. The limits on words, priors and findings are the Python constants
-- MAX_WORDS, MAX_PRIORS and MAX_FINDINGS.

CREATE TABLE originality_checks (
    id TEXT PRIMARY KEY,
    script_id TEXT NOT NULL UNIQUE REFERENCES scripts (id),
    content_item_id TEXT NOT NULL REFERENCES content_items (id),
    method TEXT NOT NULL CHECK (length(trim(method)) BETWEEN 1 AND 100),
    words INTEGER NOT NULL CHECK (words BETWEEN 0 AND 20000),
    priors_count INTEGER NOT NULL CHECK (priors_count BETWEEN 0 AND 50),
    findings_count INTEGER NOT NULL CHECK (findings_count BETWEEN 0 AND 103),
    warn_count INTEGER NOT NULL CHECK (warn_count >= 0),
    fail_count INTEGER NOT NULL CHECK (fail_count >= 0),
    requested_by_kind TEXT NOT NULL CHECK (requested_by_kind IN ('user', 'system', 'ai')),
    requested_by_id TEXT NOT NULL,
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    CHECK (warn_count + fail_count = findings_count)
) STRICT;

CREATE INDEX originality_checks_by_item ON originality_checks (content_item_id, created_at);

CREATE TABLE originality_findings (
    id TEXT PRIMARY KEY,
    check_id TEXT NOT NULL REFERENCES originality_checks (id),
    status TEXT NOT NULL CHECK (status IN ('pass', 'warn', 'fail')),
    code TEXT NOT NULL CHECK (length(trim(code)) BETWEEN 1 AND 100),
    prior_script_id TEXT REFERENCES scripts (id),
    prior_content_item_id TEXT REFERENCES content_items (id),
    section_index INTEGER CHECK (section_index IS NULL OR section_index >= 0),
    score REAL CHECK (score IS NULL OR score BETWEEN 0 AND 1),
    matched INTEGER CHECK (matched IS NULL OR matched >= 0),
    CHECK (status <> 'pass'),
    CHECK ((prior_script_id IS NULL) = (prior_content_item_id IS NULL))
) STRICT;

CREATE INDEX originality_findings_by_check ON originality_findings (check_id);
