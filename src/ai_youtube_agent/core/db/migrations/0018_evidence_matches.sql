-- F-069 Evidence Matcher (Prompt Pack v8, prompt #069).
-- One row per evidence matching run, at most one per claim extraction run:
-- the research report searched (NULL when the script version has none, and
-- then no link is made), the method used (free text, 'rules-v1' for now), the
-- number of claims, claims with and without a link, the links made and the
-- links whose numbers or dates differ, who asked and when. Rows are never
-- changed.
-- An evidence row gains the run that made it, the research report claim whose
-- evidence matched, its score and how its numbers compare; all stay NULL for
-- evidence stored before this migration.

CREATE TABLE evidence_matches (
    id TEXT PRIMARY KEY,
    extraction_id TEXT NOT NULL UNIQUE REFERENCES claim_extractions (id),
    script_id TEXT NOT NULL REFERENCES scripts (id),
    content_item_id TEXT NOT NULL REFERENCES content_items (id),
    research_report_id TEXT REFERENCES research_reports (id),
    method TEXT NOT NULL CHECK (length(trim(method)) BETWEEN 1 AND 100),
    claims_count INTEGER NOT NULL CHECK (claims_count BETWEEN 0 AND 200),
    matched INTEGER NOT NULL CHECK (matched >= 0),
    unmatched INTEGER NOT NULL CHECK (unmatched >= 0),
    links INTEGER NOT NULL CHECK (links BETWEEN 0 AND 600),
    numbers_differ INTEGER NOT NULL CHECK (numbers_differ >= 0),
    requested_by_kind TEXT NOT NULL CHECK (requested_by_kind IN ('user', 'system', 'ai')),
    requested_by_id TEXT NOT NULL,
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    CHECK (matched + unmatched = claims_count),
    CHECK (links BETWEEN matched AND 3 * matched),
    CHECK (numbers_differ <= links),
    CHECK (research_report_id IS NOT NULL OR links = 0)
) STRICT;

CREATE INDEX evidence_matches_by_item ON evidence_matches (content_item_id, created_at);

ALTER TABLE evidence ADD COLUMN match_id TEXT REFERENCES evidence_matches (id);
ALTER TABLE evidence ADD COLUMN research_claim_id TEXT CHECK (research_claim_id IS NULL OR trim(research_claim_id) <> '');
ALTER TABLE evidence ADD COLUMN score REAL CHECK (score IS NULL OR score BETWEEN 0 AND 1);
ALTER TABLE evidence ADD COLUMN numbers TEXT CHECK (numbers IS NULL OR numbers IN ('agree', 'differ', 'none'));

CREATE INDEX evidence_by_claim ON evidence (claim_id);
CREATE UNIQUE INDEX evidence_match_links ON evidence (match_id, claim_id, source_ref) WHERE match_id IS NOT NULL;
