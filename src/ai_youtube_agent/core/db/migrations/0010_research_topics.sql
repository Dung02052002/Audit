-- E-058 Topic Extractor (Prompt Pack v8, prompt #058).
-- The candidate topics of a finished research request, written once per
-- request and never changed: each topic's rank, label and key phrases, and one
-- evidence row per supporting source (which field held the phrase, its text).

CREATE TABLE topic_extractions (
    request_id TEXT PRIMARY KEY REFERENCES research_requests (id),
    extracted_at TEXT NOT NULL CHECK (extracted_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')
) STRICT;

CREATE TABLE research_topics (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES topic_extractions (request_id),
    rank INTEGER NOT NULL CHECK (rank BETWEEN 1 AND 20),
    label TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 100),
    keyphrases_json TEXT NOT NULL CHECK (json_valid(keyphrases_json) AND json_type(keyphrases_json) = 'array'),
    UNIQUE (request_id, rank)
) STRICT;

CREATE TABLE topic_evidence (
    topic_id TEXT NOT NULL REFERENCES research_topics (id),
    source_id TEXT NOT NULL REFERENCES sources (id),
    position INTEGER NOT NULL CHECK (position >= 1),
    field TEXT NOT NULL CHECK (field IN ('title', 'note', 'quote')),
    text TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 1000),
    PRIMARY KEY (topic_id, source_id),
    UNIQUE (topic_id, position)
) STRICT;
