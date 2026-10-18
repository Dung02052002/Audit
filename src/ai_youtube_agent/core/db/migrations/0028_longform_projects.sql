-- J-105 LongForm Project (Prompt Pack v8, prompt #105).
-- One row per LongForm content item (the LongFormProject entity of J-105):
-- binds the item to the exact script version and audio artifact it is produced
-- from, the LongForm configuration taken from the channel strategy, and the
-- timeline (scenes, chapters, captions, music) that the later LongForm tasks
-- fill in. Mutable through a version counter (optimistic lock).

CREATE TABLE longform_projects (
    id TEXT PRIMARY KEY,
    content_item_id TEXT NOT NULL UNIQUE REFERENCES content_items (id),
    script_id TEXT NOT NULL REFERENCES scripts (id),
    script_version INTEGER NOT NULL CHECK (script_version >= 1),
    audio_artifact_id TEXT NOT NULL REFERENCES artifacts (id),
    audio_version INTEGER NOT NULL CHECK (audio_version >= 1),
    audio_sha256 TEXT NOT NULL CHECK (length(audio_sha256) = 64 AND audio_sha256 NOT GLOB '*[^0-9a-f]*'),
    audio_duration_ms INTEGER NOT NULL CHECK (audio_duration_ms >= 1),
    strategy_version INTEGER NOT NULL CHECK (strategy_version >= 1),
    format_json TEXT NOT NULL CHECK (json_valid(format_json) AND json_type(format_json) = 'object' AND json_type(format_json, '$.max_seconds') IS 'integer'),
    timeline_json TEXT NOT NULL CHECK (json_valid(timeline_json) AND json_type(timeline_json) = 'object' AND json_type(timeline_json, '$.schema_version') IS 'integer' AND json_type(timeline_json, '$.scenes') IS 'array' AND json_type(timeline_json, '$.chapters') IS 'array'),
    status TEXT NOT NULL CHECK (status IN ('draft', 'planned', 'ready')),
    version INTEGER NOT NULL CHECK (version >= 1),
    created_by_kind TEXT NOT NULL CHECK (created_by_kind IN ('user', 'system', 'ai')),
    created_by_id TEXT NOT NULL CHECK (length(trim(created_by_id)) >= 1),
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    updated_by_kind TEXT NOT NULL CHECK (updated_by_kind IN ('user', 'system', 'ai')),
    updated_by_id TEXT NOT NULL CHECK (length(trim(updated_by_id)) >= 1),
    updated_at TEXT NOT NULL CHECK (updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    CHECK (updated_at >= created_at),
    CHECK (json_type(timeline_json, '$.duration_ms') IS 'integer' AND json_extract(timeline_json, '$.duration_ms') IS audio_duration_ms),
    CHECK (audio_duration_ms <= json_extract(format_json, '$.max_seconds') * 1000),
    CHECK ((status = 'draft') = (json_array_length(timeline_json, '$.scenes') = 0))
) STRICT;

