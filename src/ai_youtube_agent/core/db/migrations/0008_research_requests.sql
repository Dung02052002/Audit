-- E-056 Source Collector (Prompt Pack v8, prompt #056).
-- A research request, its status and limits, the failures it met, and the
-- sources it collected (in collection order, with the query and rank that
-- found each). A request changes only through optimistic updates; collected
-- links are added once, when the request finishes.

CREATE TABLE research_requests (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels (id),
    queries_json TEXT NOT NULL CHECK (json_valid(queries_json) AND json_type(queries_json) = 'array'),
    language TEXT,
    market TEXT CHECK (market IS NULL OR length(market) = 2),
    max_sources INTEGER NOT NULL CHECK (max_sources BETWEEN 1 AND 100),
    max_results_per_query INTEGER NOT NULL CHECK (max_results_per_query BETWEEN 1 AND 50),
    max_per_domain INTEGER NOT NULL CHECK (max_per_domain BETWEEN 1 AND 20),
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'partial', 'failed')),
    requested_by_kind TEXT NOT NULL CHECK (requested_by_kind IN ('user', 'system', 'ai')),
    requested_by_id TEXT NOT NULL,
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    updated_at TEXT NOT NULL CHECK (updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    started_at TEXT CHECK (started_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    finished_at TEXT CHECK (finished_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    failures_json TEXT NOT NULL CHECK (json_valid(failures_json) AND json_type(failures_json) = 'array'),
    CHECK (updated_at >= created_at)
) STRICT;

CREATE TABLE research_request_sources (
    request_id TEXT NOT NULL REFERENCES research_requests (id),
    source_id TEXT NOT NULL REFERENCES sources (id),
    position INTEGER NOT NULL CHECK (position >= 1),
    query TEXT NOT NULL,
    rank INTEGER NOT NULL CHECK (rank >= 1),
    PRIMARY KEY (request_id, source_id),
    UNIQUE (request_id, position)
) STRICT;

CREATE INDEX research_requests_by_channel ON research_requests (channel_id, created_at);
