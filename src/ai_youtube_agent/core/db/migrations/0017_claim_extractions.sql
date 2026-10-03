-- F-068 Claim Extractor (Prompt Pack v8, prompt #068).
-- One row per claim extraction run, at most one per script version: the
-- method used (free text, 'rules-v1' for now, so another extractor needs no
-- migration), the number of claims, the sentences skipped per reason and
-- dropped per limit, who asked and when. Runs that found no claim are stored
-- too. Rows are never changed.
-- A claim gains its kind and the run that found it; both stay NULL for claims
-- stored before this migration.

CREATE TABLE claim_extractions (
    id TEXT PRIMARY KEY,
    script_id TEXT NOT NULL UNIQUE REFERENCES scripts (id),
    content_item_id TEXT NOT NULL REFERENCES content_items (id),
    method TEXT NOT NULL CHECK (length(trim(method)) BETWEEN 1 AND 100),
    claims_count INTEGER NOT NULL CHECK (claims_count BETWEEN 0 AND 200),
    skipped_cta INTEGER NOT NULL CHECK (skipped_cta >= 0),
    skipped_question INTEGER NOT NULL CHECK (skipped_question >= 0),
    skipped_opinion INTEGER NOT NULL CHECK (skipped_opinion >= 0),
    skipped_no_signal INTEGER NOT NULL CHECK (skipped_no_signal >= 0),
    dropped_duplicates INTEGER NOT NULL CHECK (dropped_duplicates >= 0),
    dropped_over_cap INTEGER NOT NULL CHECK (dropped_over_cap >= 0),
    dropped_too_long INTEGER NOT NULL CHECK (dropped_too_long >= 0),
    requested_by_kind TEXT NOT NULL CHECK (requested_by_kind IN ('user', 'system', 'ai')),
    requested_by_id TEXT NOT NULL,
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')
) STRICT;

CREATE INDEX claim_extractions_by_item ON claim_extractions (content_item_id, created_at);

ALTER TABLE claims ADD COLUMN kind TEXT CHECK (kind IS NULL OR kind IN ('numeric', 'date', 'entity', 'comparison', 'absolute'));
ALTER TABLE claims ADD COLUMN extraction_id TEXT REFERENCES claim_extractions (id);

CREATE INDEX claims_by_extraction ON claims (extraction_id);
