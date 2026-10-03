-- F-070 Fact Check Result (Prompt Pack v8, prompt #070).
-- One row per fact check run, at most one per evidence matching run: the
-- claim extraction run and research report it read (the report is NULL when
-- the matching run had none), the method used (free text, 'rules-v1' for
-- now), the number of claims and how many are PASS, WARN and FAIL, who asked
-- and when. A script-level status is derived from the counts, not stored.
-- Rows are never changed: a result is a record, not a gate, and no override
-- exists yet (a later task may add a separate table for overrides).
-- One result row per claim of the run: its verdict (pass, warn or fail), the
-- code of the rule that gave it, and a snapshot of what the rule read: the
-- number of links, the best link score, how the numbers of the links compare
-- and the lowest (most certain) uncertainty of the research claims they
-- matched; all three stay NULL when the claim has no link.
-- The code is open text here, with only a length check: the closed list of
-- codes lives in Python (FactCheckCode), so a later rule version can add a
-- code without rebuilding the table, as SQLite cannot change a CHECK in
-- place and migrations only move forward. 'method' is open for the same
-- reason.

CREATE TABLE fact_checks (
    id TEXT PRIMARY KEY,
    match_id TEXT NOT NULL UNIQUE REFERENCES evidence_matches (id),
    extraction_id TEXT NOT NULL REFERENCES claim_extractions (id),
    script_id TEXT NOT NULL REFERENCES scripts (id),
    content_item_id TEXT NOT NULL REFERENCES content_items (id),
    research_report_id TEXT REFERENCES research_reports (id),
    method TEXT NOT NULL CHECK (length(trim(method)) BETWEEN 1 AND 100),
    claims_count INTEGER NOT NULL CHECK (claims_count BETWEEN 0 AND 200),
    pass_count INTEGER NOT NULL CHECK (pass_count >= 0),
    warn_count INTEGER NOT NULL CHECK (warn_count >= 0),
    fail_count INTEGER NOT NULL CHECK (fail_count >= 0),
    requested_by_kind TEXT NOT NULL CHECK (requested_by_kind IN ('user', 'system', 'ai')),
    requested_by_id TEXT NOT NULL,
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    CHECK (pass_count + warn_count + fail_count = claims_count)
) STRICT;

CREATE INDEX fact_checks_by_item ON fact_checks (content_item_id, created_at);

CREATE TABLE fact_check_results (
    id TEXT PRIMARY KEY,
    fact_check_id TEXT NOT NULL REFERENCES fact_checks (id),
    claim_id TEXT NOT NULL REFERENCES claims (id),
    status TEXT NOT NULL CHECK (status IN ('pass', 'warn', 'fail')),
    code TEXT NOT NULL CHECK (length(trim(code)) BETWEEN 1 AND 100),
    links INTEGER NOT NULL CHECK (links BETWEEN 0 AND 3),
    best_score REAL CHECK (best_score IS NULL OR best_score BETWEEN 0 AND 1),
    numbers TEXT CHECK (numbers IS NULL OR numbers IN ('agree', 'differ', 'none')),
    uncertainty TEXT CHECK (uncertainty IS NULL OR uncertainty IN ('low', 'medium', 'high')),
    UNIQUE (fact_check_id, claim_id),
    CHECK (links > 0 OR (best_score IS NULL AND numbers IS NULL AND uncertainty IS NULL)),
    CHECK (links = 0 OR (best_score IS NOT NULL AND numbers IS NOT NULL AND uncertainty IS NOT NULL))
) STRICT;

CREATE INDEX fact_check_results_by_claim ON fact_check_results (claim_id);
