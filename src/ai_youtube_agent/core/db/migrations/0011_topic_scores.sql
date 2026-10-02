-- E-059 Topic Scoring (Prompt Pack v8, prompt #059).
-- The transparent scores of a request's topics, written once per request and
-- never changed: each signal with its inputs, the strategy version used and
-- which strategy inputs were missing.

CREATE TABLE topic_scorings (
    request_id TEXT PRIMARY KEY REFERENCES topic_extractions (request_id),
    scored_at TEXT NOT NULL CHECK (scored_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    strategy_version INTEGER CHECK (strategy_version >= 1),
    missing_inputs_json TEXT NOT NULL CHECK (json_valid(missing_inputs_json) AND json_type(missing_inputs_json) = 'array')
) STRICT;

CREATE TABLE topic_scores (
    topic_id TEXT PRIMARY KEY REFERENCES research_topics (id),
    request_id TEXT NOT NULL REFERENCES topic_scorings (request_id),
    position INTEGER NOT NULL CHECK (position >= 1),
    relevance REAL NOT NULL CHECK (relevance BETWEEN 0 AND 1),
    novelty REAL NOT NULL CHECK (novelty BETWEEN 0 AND 1),
    support REAL NOT NULL CHECK (support BETWEEN 0 AND 1),
    score REAL NOT NULL CHECK (score BETWEEN 0 AND 1),
    topic_words_json TEXT NOT NULL CHECK (json_valid(topic_words_json) AND json_type(topic_words_json) = 'array'),
    matched_words_json TEXT NOT NULL CHECK (json_valid(matched_words_json) AND json_type(matched_words_json) = 'array'),
    seen_in_json TEXT NOT NULL CHECK (json_valid(seen_in_json) AND json_type(seen_in_json) = 'array'),
    support_count INTEGER NOT NULL CHECK (support_count >= 1),
    kept_sources INTEGER NOT NULL CHECK (kept_sources >= support_count),
    UNIQUE (request_id, position)
) STRICT;
