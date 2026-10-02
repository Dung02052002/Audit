-- E-057 Source Deduplication (Prompt Pack v8, prompt #057).
-- Sources gain the simhash of their fetched text (16 lower-case hex digits,
-- NULL for sources from before #057 or without text). The page text itself is
-- not stored. source_duplicates records, per finished research request, which
-- collected source duplicates which kept source, why and how similar they are.
-- Results are written once per request and never changed.

ALTER TABLE sources
    ADD COLUMN content_fingerprint TEXT CHECK (
        content_fingerprint IS NULL
        OR (length(content_fingerprint) = 16 AND content_fingerprint NOT GLOB '*[^0-9a-f]*')
    );

CREATE TABLE source_deduplications (
    request_id TEXT PRIMARY KEY REFERENCES research_requests (id),
    deduplicated_at TEXT NOT NULL CHECK (deduplicated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')
) STRICT;

CREATE TABLE source_duplicates (
    request_id TEXT NOT NULL REFERENCES source_deduplications (request_id),
    source_id TEXT NOT NULL REFERENCES sources (id),
    duplicate_of TEXT NOT NULL REFERENCES sources (id),
    reason TEXT NOT NULL CHECK (reason IN ('fingerprint', 'title')),
    similarity REAL NOT NULL CHECK (similarity BETWEEN 0 AND 1),
    PRIMARY KEY (request_id, source_id),
    CHECK (source_id <> duplicate_of)
) STRICT;
