-- F-065 Hook Generator (Prompt Pack v8, prompt #065).
-- One row per run of the hook generator for a content item: the inputs used
-- (content type, language, strategy version, research report, topic, angle),
-- up to 3 valid candidates and the rejected texts with their issues (JSON),
-- the provider and model, who asked and when. Rows are never changed.

CREATE TABLE hook_generations (
    id TEXT PRIMARY KEY,
    content_item_id TEXT NOT NULL REFERENCES content_items (id),
    content_type TEXT NOT NULL CHECK (content_type IN ('shorts', 'longform')),
    language TEXT NOT NULL,
    strategy_version INTEGER NOT NULL CHECK (strategy_version >= 1),
    research_report_id TEXT NOT NULL REFERENCES research_reports (id),
    topic_id TEXT REFERENCES research_topics (id),
    topic_label TEXT,
    angle TEXT CHECK (angle IS NULL OR length(angle) BETWEEN 1 AND 300),
    candidates_json TEXT NOT NULL CHECK (json_valid(candidates_json) AND json_type(candidates_json) = 'array' AND json_array_length(candidates_json) <= 3),
    rejected_json TEXT NOT NULL CHECK (json_valid(rejected_json) AND json_type(rejected_json) = 'array'),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    requested_by_kind TEXT NOT NULL CHECK (requested_by_kind IN ('user', 'system', 'ai')),
    requested_by_id TEXT NOT NULL,
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    CHECK ((topic_id IS NULL) = (topic_label IS NULL))
) STRICT;

CREATE INDEX hook_generations_by_item ON hook_generations (content_item_id, created_at);
