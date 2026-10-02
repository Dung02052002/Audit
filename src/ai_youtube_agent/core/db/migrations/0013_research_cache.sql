-- E-061 Research Cache (Prompt Pack v8, prompt #061).
-- Successful provider answers, one row per search key or normalised fetch
-- URL. Rows are never deleted: a stale row stays until a refresh replaces its
-- payload and cached_at. Failures are never stored.

CREATE TABLE research_cache (
    kind TEXT NOT NULL CHECK (kind IN ('search', 'fetch')),
    key TEXT NOT NULL,
    provider TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json) AND json_type(payload_json) = 'object'),
    cached_at TEXT NOT NULL CHECK (cached_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    PRIMARY KEY (kind, key)
) STRICT;
