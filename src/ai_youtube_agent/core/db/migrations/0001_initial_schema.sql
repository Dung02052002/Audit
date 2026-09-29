-- 0001 initial schema (Prompt Pack v8, prompt #029).
--
-- One STRICT table per domain entity from B-013 to B-028, plus child tables for
-- lists that reference other rows or need constraints, and the append-only
-- audit log from A-011.
--
-- Conventions:
-- * Ids are TEXT. Foreign keys link only entities this project owns; opaque
--   references (asset_ref, source_ref, subject_id, ref, YouTube ids) have none.
-- * Datetimes are TEXT 'YYYY-MM-DDTHH:MM:SS.ffffffZ' (UTC, fixed width), see
--   core/db/codec.py. Decimals are TEXT in plain notation, never REAL.
--   SQLite turns a bound number into text (rounding floats) before any CHECK
--   runs, so writers must pass money through codec.format_decimal.
-- * An actor is two columns, <role>_kind and <role>_id.
-- * JSON TEXT holds only value objects and flexible payloads.
-- * Enum CHECK lists must match the Python enums; a test compares them.

CREATE TABLE channels (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL CHECK (trim(title) <> ''),
    youtube_channel_id TEXT NOT NULL UNIQUE,
    youtube_handle TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'paused', 'disconnected', 'archived')),
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    updated_at TEXT NOT NULL CHECK (updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    CHECK (updated_at >= created_at)
) STRICT;

CREATE TABLE strategy_profiles (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL UNIQUE REFERENCES channels (id),
    market_country TEXT NOT NULL CHECK (length(market_country) = 2),
    primary_language TEXT NOT NULL,
    secondary_languages_json TEXT NOT NULL CHECK (json_valid(secondary_languages_json) AND json_type(secondary_languages_json) = 'array'),
    audience_json TEXT NOT NULL CHECK (json_valid(audience_json)),
    niche_json TEXT NOT NULL CHECK (json_valid(niche_json)),
    brand_json TEXT NOT NULL CHECK (json_valid(brand_json)),
    cadence_shorts_per_day INTEGER NOT NULL CHECK (cadence_shorts_per_day >= 0),
    cadence_longform_per_day INTEGER NOT NULL CHECK (cadence_longform_per_day >= 0),
    budget_currency TEXT NOT NULL CHECK (length(budget_currency) = 3),
    budget_daily_limit TEXT NOT NULL CHECK (budget_daily_limit GLOB '[0-9]*' AND budget_daily_limit NOT GLOB '*[^0-9.]*' AND budget_daily_limit NOT GLOB '*.*.*' AND budget_daily_limit NOT GLOB '*.'),
    budget_monthly_limit TEXT NOT NULL CHECK (budget_monthly_limit GLOB '[0-9]*' AND budget_monthly_limit NOT GLOB '*[^0-9.]*' AND budget_monthly_limit NOT GLOB '*.*.*' AND budget_monthly_limit NOT GLOB '*.'),
    monetization_json TEXT NOT NULL CHECK (json_valid(monetization_json)),
    version INTEGER NOT NULL CHECK (version >= 1),
    updated_by_kind TEXT NOT NULL CHECK (updated_by_kind = 'user'),
    updated_by_id TEXT NOT NULL,
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    updated_at TEXT NOT NULL CHECK (updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    CHECK (updated_at >= created_at)
) STRICT;

CREATE TABLE voice_profiles (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels (id),
    provider TEXT NOT NULL,
    voice_id TEXT NOT NULL,
    language TEXT NOT NULL,
    speaking_style TEXT,
    version INTEGER NOT NULL CHECK (version >= 1),
    updated_by_kind TEXT NOT NULL CHECK (updated_by_kind = 'user'),
    updated_by_id TEXT NOT NULL,
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    updated_at TEXT NOT NULL CHECK (updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    CHECK (updated_at >= created_at)
) STRICT;

CREATE TABLE content_items (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels (id),
    strategy_profile_id TEXT NOT NULL REFERENCES strategy_profiles (id),
    strategy_version INTEGER NOT NULL CHECK (strategy_version >= 1),
    content_type TEXT NOT NULL CHECK (content_type IN ('shorts', 'longform')),
    title TEXT NOT NULL CHECK (trim(title) <> ''),
    status TEXT NOT NULL CHECK (status IN ('draft', 'generating', 'testing', 'preview_ready', 'awaiting_approval', 'approved', 'publishing', 'published', 'rejected', 'failed')),
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    updated_at TEXT NOT NULL CHECK (updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    CHECK (updated_at >= created_at)
) STRICT;

CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    content_item_id TEXT NOT NULL REFERENCES content_items (id),
    kind TEXT NOT NULL CHECK (kind IN ('video', 'audio', 'subtitles', 'thumbnail', 'metadata')),
    version INTEGER NOT NULL CHECK (version >= 1),
    uri TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 1),
    media_type TEXT NOT NULL,
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    UNIQUE (content_item_id, kind, version)
) STRICT;

CREATE TABLE scripts (
    id TEXT PRIMARY KEY,
    content_item_id TEXT NOT NULL REFERENCES content_items (id),
    version INTEGER NOT NULL CHECK (version >= 1),
    text TEXT NOT NULL CHECK (trim(text) <> ''),
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    UNIQUE (content_item_id, version)
) STRICT;

CREATE TABLE claims (
    id TEXT PRIMARY KEY,
    script_id TEXT NOT NULL REFERENCES scripts (id),
    text TEXT NOT NULL CHECK (trim(text) <> ''),
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')
) STRICT;

CREATE TABLE evidence (
    id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims (id),
    source_ref TEXT NOT NULL,
    excerpt TEXT,
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')
) STRICT;

CREATE TABLE audio_metadata (
    id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL UNIQUE REFERENCES artifacts (id),
    script_id TEXT NOT NULL REFERENCES scripts (id),
    voice_profile_id TEXT NOT NULL REFERENCES voice_profiles (id),
    voice_profile_version INTEGER NOT NULL CHECK (voice_profile_version >= 1),
    provider TEXT NOT NULL,
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 1),
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')
) STRICT;

CREATE TABLE rights_records (
    id TEXT PRIMARY KEY,
    content_item_id TEXT NOT NULL REFERENCES content_items (id),
    asset_ref TEXT NOT NULL,
    source TEXT NOT NULL,
    license TEXT,
    risk_level TEXT NOT NULL CHECK (risk_level IN ('unknown', 'low', 'medium', 'high')),
    resolution TEXT NOT NULL CHECK (resolution IN ('unresolved', 'resolved')),
    resolved_by_kind TEXT CHECK (resolved_by_kind = 'user'),
    resolved_by_id TEXT,
    resolved_at TEXT CHECK (resolved_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    updated_at TEXT NOT NULL CHECK (updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    CHECK (updated_at >= created_at),
    CHECK ((resolution = 'resolved') = (resolved_by_kind IS NOT NULL AND resolved_by_id IS NOT NULL AND resolved_at IS NOT NULL)),
    CHECK (resolution = 'resolved' OR (resolved_by_kind IS NULL AND resolved_by_id IS NULL AND resolved_at IS NULL))
) STRICT;

CREATE TABLE qc_results (
    id TEXT PRIMARY KEY,
    content_item_id TEXT NOT NULL REFERENCES content_items (id),
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')
) STRICT;

CREATE TABLE qc_result_artifacts (
    qc_result_id TEXT NOT NULL REFERENCES qc_results (id),
    position INTEGER NOT NULL CHECK (position >= 0),
    artifact_id TEXT NOT NULL REFERENCES artifacts (id),
    PRIMARY KEY (qc_result_id, artifact_id),
    UNIQUE (qc_result_id, position)
) STRICT;

CREATE TABLE qc_checks (
    qc_result_id TEXT NOT NULL REFERENCES qc_results (id),
    position INTEGER NOT NULL CHECK (position >= 0),
    name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pass', 'warn', 'fail')),
    detail TEXT,
    PRIMARY KEY (qc_result_id, name),
    UNIQUE (qc_result_id, position)
) STRICT;

CREATE TABLE approval_requests (
    id TEXT PRIMARY KEY,
    content_item_id TEXT NOT NULL REFERENCES content_items (id),
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'changes_requested', 'invalidated', 'expired')),
    requested_by_kind TEXT NOT NULL CHECK (requested_by_kind IN ('user', 'system', 'ai')),
    requested_by_id TEXT NOT NULL,
    qc_result_id TEXT REFERENCES qc_results (id),
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')
) STRICT;

CREATE TABLE approval_artifacts (
    approval_request_id TEXT NOT NULL REFERENCES approval_requests (id),
    position INTEGER NOT NULL CHECK (position >= 0),
    artifact_id TEXT NOT NULL REFERENCES artifacts (id),
    kind TEXT NOT NULL CHECK (kind IN ('video', 'audio', 'subtitles', 'thumbnail', 'metadata')),
    version INTEGER NOT NULL CHECK (version >= 1),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    PRIMARY KEY (approval_request_id, artifact_id),
    UNIQUE (approval_request_id, kind),
    UNIQUE (approval_request_id, position)
) STRICT;

CREATE TABLE publish_jobs (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    content_item_id TEXT NOT NULL REFERENCES content_items (id),
    approval_request_id TEXT NOT NULL REFERENCES approval_requests (id),
    content_type TEXT NOT NULL CHECK (content_type IN ('shorts', 'longform')),
    status TEXT NOT NULL CHECK (status IN ('queued', 'in_progress', 'succeeded', 'failed')),
    attempts INTEGER NOT NULL CHECK (attempts >= 0),
    last_error TEXT,
    result_youtube_video_id TEXT CHECK (length(result_youtube_video_id) = 11),
    result_published_at TEXT CHECK (result_published_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    updated_at TEXT NOT NULL CHECK (updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    CHECK (updated_at >= created_at),
    CHECK ((status = 'succeeded') = (result_youtube_video_id IS NOT NULL AND result_published_at IS NOT NULL)),
    CHECK (status = 'succeeded' OR (result_youtube_video_id IS NULL AND result_published_at IS NULL)),
    CHECK ((status = 'failed') = (last_error IS NOT NULL))
) STRICT;

CREATE TABLE metric_snapshots (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('channel', 'video')),
    subject_id TEXT NOT NULL,
    source TEXT NOT NULL,
    period_start TEXT NOT NULL CHECK (period_start GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    period_end TEXT NOT NULL CHECK (period_end GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    retrieved_at TEXT NOT NULL CHECK (retrieved_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    CHECK (period_start < period_end),
    CHECK (retrieved_at >= period_start)
) STRICT;

CREATE TABLE metric_values (
    snapshot_id TEXT NOT NULL REFERENCES metric_snapshots (id),
    name TEXT NOT NULL,
    value TEXT NOT NULL CHECK ((value GLOB '[0-9]*' OR value GLOB '-[0-9]*') AND substr(value, 2) NOT GLOB '*[^0-9.]*' AND value NOT GLOB '*.*.*' AND value NOT GLOB '*.'),
    PRIMARY KEY (snapshot_id, name)
) STRICT;

CREATE TABLE revenue_records (
    id TEXT PRIMARY KEY,
    stage TEXT NOT NULL CHECK (stage IN ('estimated', 'final')),
    scope TEXT NOT NULL CHECK (scope IN ('channel', 'video')),
    subject_id TEXT NOT NULL,
    revenue_type TEXT NOT NULL,
    source TEXT NOT NULL,
    amount TEXT NOT NULL CHECK (amount GLOB '[0-9]*' AND amount NOT GLOB '*[^0-9.]*' AND amount NOT GLOB '*.*.*' AND amount NOT GLOB '*.'),
    currency TEXT NOT NULL CHECK (length(currency) = 3),
    period_start TEXT NOT NULL CHECK (period_start GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    period_end TEXT NOT NULL CHECK (period_end GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    retrieved_at TEXT NOT NULL CHECK (retrieved_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    CHECK (period_start < period_end),
    CHECK (retrieved_at >= period_start),
    CHECK (stage = 'estimated' OR retrieved_at >= period_end)
) STRICT;

CREATE TABLE cost_records (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels (id),
    category TEXT NOT NULL CHECK (category IN ('production', 'api', 'tts', 'render', 'storage', 'llm')),
    provider TEXT NOT NULL,
    amount TEXT NOT NULL CHECK (amount GLOB '[0-9]*' AND amount NOT GLOB '*[^0-9.]*' AND amount NOT GLOB '*.*.*' AND amount NOT GLOB '*.'),
    currency TEXT NOT NULL CHECK (length(currency) = 3),
    incurred_at TEXT NOT NULL CHECK (incurred_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    content_item_id TEXT REFERENCES content_items (id),
    ref TEXT
) STRICT;

CREATE TABLE comments (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels (id),
    youtube_comment_id TEXT NOT NULL UNIQUE,
    youtube_video_id TEXT NOT NULL CHECK (length(youtube_video_id) = 11),
    parent_comment_id TEXT,
    author_display_name TEXT NOT NULL,
    text TEXT NOT NULL,
    published_at TEXT NOT NULL CHECK (published_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    CHECK (parent_comment_id IS NULL OR parent_comment_id <> youtube_comment_id)
) STRICT;

CREATE TABLE comment_classifications (
    id TEXT PRIMARY KEY,
    comment_id TEXT NOT NULL REFERENCES comments (id),
    label TEXT NOT NULL CHECK (label IN ('needs_attention', 'moderation', 'reply_candidate', 'no_action')),
    classified_by_kind TEXT NOT NULL CHECK (classified_by_kind IN ('user', 'system', 'ai')),
    classified_by_id TEXT NOT NULL,
    rationale TEXT,
    classified_at TEXT NOT NULL CHECK (classified_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')
) STRICT;

CREATE TABLE reply_drafts (
    id TEXT PRIMARY KEY,
    comment_id TEXT NOT NULL REFERENCES comments (id),
    text TEXT NOT NULL CHECK (trim(text) <> ''),
    status TEXT NOT NULL CHECK (status IN ('draft', 'approved', 'posted', 'failed', 'discarded')),
    created_by_kind TEXT NOT NULL CHECK (created_by_kind IN ('user', 'system', 'ai')),
    created_by_id TEXT NOT NULL,
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    youtube_reply_id TEXT,
    CHECK ((status = 'posted') = (youtube_reply_id IS NOT NULL))
) STRICT;

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    started_by_kind TEXT NOT NULL CHECK (started_by_kind = 'user'),
    started_by_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'stopped')),
    started_at TEXT NOT NULL CHECK (started_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    stopped_at TEXT CHECK (stopped_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    CHECK ((status = 'stopped') = (stopped_at IS NOT NULL)),
    CHECK (stopped_at IS NULL OR stopped_at >= started_at)
) STRICT;

CREATE TABLE ai_jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    content_item_id TEXT REFERENCES content_items (id),
    session_id TEXT REFERENCES sessions (id),
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'waiting', 'succeeded', 'failed', 'cancelled')),
    attempts INTEGER NOT NULL CHECK (attempts >= 0),
    last_error TEXT,
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    updated_at TEXT NOT NULL CHECK (updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    CHECK (updated_at >= created_at),
    CHECK ((status = 'failed') = (last_error IS NOT NULL))
) STRICT;

CREATE TABLE job_checkpoints (
    job_id TEXT NOT NULL REFERENCES ai_jobs (id),
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    step TEXT NOT NULL,
    data_json TEXT NOT NULL CHECK (json_valid(data_json) AND json_type(data_json) = 'object'),
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    PRIMARY KEY (job_id, sequence)
) STRICT;

CREATE TABLE experiments (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels (id),
    content_item_id TEXT REFERENCES content_items (id),
    type TEXT NOT NULL CHECK (type IN ('title', 'thumbnail', 'content')),
    hypothesis TEXT NOT NULL CHECK (trim(hypothesis) <> ''),
    status TEXT NOT NULL CHECK (status IN ('proposed', 'running', 'concluded', 'cancelled')),
    proposed_by_kind TEXT NOT NULL CHECK (proposed_by_kind IN ('user', 'system', 'ai')),
    proposed_by_id TEXT NOT NULL,
    started_by_kind TEXT CHECK (started_by_kind = 'user'),
    started_by_id TEXT,
    cancelled_by_kind TEXT CHECK (cancelled_by_kind = 'user'),
    cancelled_by_id TEXT,
    conclusion_winner_key TEXT,
    conclusion_note TEXT,
    conclusion_metric_snapshot_ids_json TEXT CHECK (json_valid(conclusion_metric_snapshot_ids_json) AND json_type(conclusion_metric_snapshot_ids_json) = 'array'),
    concluded_by_kind TEXT CHECK (concluded_by_kind = 'user'),
    concluded_by_id TEXT,
    concluded_at TEXT CHECK (concluded_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    updated_at TEXT NOT NULL CHECK (updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    CHECK (updated_at >= created_at),
    CHECK ((started_by_kind IS NULL) = (started_by_id IS NULL)),
    CHECK ((cancelled_by_kind IS NULL) = (cancelled_by_id IS NULL)),
    CHECK ((status = 'cancelled') = (cancelled_by_kind IS NOT NULL)),
    CHECK ((status = 'concluded') = (concluded_by_kind IS NOT NULL AND concluded_by_id IS NOT NULL AND concluded_at IS NOT NULL AND conclusion_metric_snapshot_ids_json IS NOT NULL)),
    CHECK (status = 'concluded' OR (conclusion_winner_key IS NULL AND conclusion_note IS NULL AND conclusion_metric_snapshot_ids_json IS NULL AND concluded_by_kind IS NULL AND concluded_by_id IS NULL AND concluded_at IS NULL)),
    CHECK (status NOT IN ('running', 'concluded') OR started_by_kind IS NOT NULL),
    CHECK (status <> 'proposed' OR started_by_kind IS NULL)
) STRICT;

CREATE TABLE experiment_variants (
    experiment_id TEXT NOT NULL REFERENCES experiments (id),
    position INTEGER NOT NULL CHECK (position >= 0),
    key TEXT NOT NULL,
    value TEXT NOT NULL CHECK (trim(value) <> ''),
    PRIMARY KEY (experiment_id, key),
    UNIQUE (experiment_id, position)
) STRICT;

CREATE TABLE audit_events (
    event_id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL CHECK (timestamp GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    action TEXT NOT NULL,
    actor_kind TEXT NOT NULL CHECK (actor_kind IN ('user', 'system', 'ai')),
    actor_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('success', 'failure', 'denied')),
    correlation_id TEXT,
    session_id TEXT,
    job_id TEXT,
    metadata_json TEXT NOT NULL CHECK (json_valid(metadata_json) AND json_type(metadata_json) = 'object')
) STRICT;

CREATE TRIGGER audit_events_no_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;

CREATE TRIGGER audit_events_no_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;

CREATE INDEX content_items_by_channel ON content_items (channel_id, status);
CREATE INDEX artifacts_by_item ON artifacts (content_item_id, kind, version);
CREATE INDEX approval_requests_by_item ON approval_requests (content_item_id, status);
CREATE INDEX publish_jobs_by_item ON publish_jobs (content_item_id);
CREATE INDEX metric_snapshots_by_subject ON metric_snapshots (scope, subject_id, period_start);
CREATE INDEX revenue_records_by_subject ON revenue_records (scope, subject_id, period_start);
CREATE INDEX cost_records_by_channel ON cost_records (channel_id, incurred_at);
CREATE INDEX comments_by_video ON comments (youtube_video_id);
CREATE INDEX ai_jobs_by_status ON ai_jobs (status);
CREATE INDEX audit_events_by_entity ON audit_events (entity_type, entity_id, timestamp);
