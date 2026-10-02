-- E-055 Source Model (Prompt Pack v8, prompt #055).
-- One row per research source, identified by its normalised URL. Sources are
-- immutable records: add only. Evidence notes are a JSON array of
-- {"note", "quote"} objects. Evidence.source_ref (B-017) holds sources.id.

CREATE TABLE sources (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    final_url TEXT NOT NULL,
    normalized_url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
    provider TEXT NOT NULL,
    published_at TEXT CHECK (published_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    retrieved_at TEXT NOT NULL CHECK (retrieved_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    evidence_notes_json TEXT NOT NULL CHECK (json_valid(evidence_notes_json) AND json_type(evidence_notes_json) = 'array')
) STRICT;
