-- BUDGET-01 Paid AI API budget (user decisions 2026-10-23).
-- paid_api_policy holds at most one row (id 1): the mode for paid AI APIs
-- (free_only or paid_with_per_request_approval), its version (optimistic lock)
-- and the user who saved it. Only a user changes the policy; the AI never
-- changes the budget. Without a row the policy is free_only.
-- paid_api_quotes holds one row per provider call that may cost money: the
-- provider, model, task type and request hash it is bound to, the units, the
-- estimated price (unit price, amount and currency, both null when the price
-- is unknown) and its source, the conditions and unknowns (JSON arrays), the
-- content item and job it was made for, and the decision. A quote is pending
-- until a user approves or rejects it, an approved quote is consumed by
-- exactly one call, and every open quote expires. A request whose price is
-- unknown, or that free_only refuses, is stored blocked with its reason and is
-- never approvable. At most one open (pending or approved) quote exists per
-- request hash. Rows are never removed. This migration changes no other
-- table: content_items and ai_jobs are only referenced.

CREATE TABLE paid_api_policy (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL CHECK (mode IN ('free_only', 'paid_with_per_request_approval')),
    version INTEGER NOT NULL CHECK (version >= 1),
    updated_by_kind TEXT NOT NULL CHECK (updated_by_kind = 'user'),
    updated_by_id TEXT NOT NULL CHECK (length(trim(updated_by_id)) >= 1),
    updated_at TEXT NOT NULL CHECK (updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')
) STRICT;

CREATE TABLE paid_api_quotes (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'consumed', 'rejected', 'expired', 'blocked')),
    task_type TEXT NOT NULL CHECK (task_type IN ('video_clip_generation', 'speech_synthesis', 'image_generation', 'text_generation', 'research')),
    provider TEXT NOT NULL CHECK (length(trim(provider)) BETWEEN 1 AND 80),
    model TEXT NOT NULL CHECK (length(trim(model)) BETWEEN 1 AND 120),
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'),
    unit TEXT NOT NULL CHECK (length(trim(unit)) BETWEEN 1 AND 40),
    unit_count TEXT NOT NULL,
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 1),
    scene_id TEXT,
    unit_price TEXT,
    estimated_amount TEXT,
    currency TEXT CHECK (currency IS NULL OR currency GLOB '[A-Z][A-Z][A-Z]'),
    price_source TEXT,
    conditions_json TEXT NOT NULL CHECK (json_valid(conditions_json) AND json_type(conditions_json) = 'array'),
    unknowns_json TEXT NOT NULL CHECK (json_valid(unknowns_json) AND json_type(unknowns_json) = 'array'),
    blocked_reason TEXT,
    content_item_id TEXT REFERENCES content_items (id),
    job_id TEXT REFERENCES ai_jobs (id),
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    expires_at TEXT NOT NULL CHECK (expires_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    decided_by_kind TEXT CHECK (decided_by_kind IS NULL OR decided_by_kind = 'user'),
    decided_by_id TEXT,
    decided_at TEXT,
    consumed_at TEXT,
    CHECK (expires_at > created_at),
    CHECK ((estimated_amount IS NULL) = (currency IS NULL)),
    CHECK ((decided_by_kind IS NULL) = (decided_at IS NULL)),
    CHECK (status <> 'blocked' OR blocked_reason IS NOT NULL),
    CHECK (status NOT IN ('approved', 'consumed') OR (estimated_amount IS NOT NULL AND decided_at IS NOT NULL)),
    CHECK ((status = 'consumed') = (consumed_at IS NOT NULL))
) STRICT;

CREATE UNIQUE INDEX paid_api_quotes_one_open_per_hash ON paid_api_quotes (request_hash) WHERE status IN ('pending', 'approved');
CREATE INDEX paid_api_quotes_by_item ON paid_api_quotes (content_item_id, created_at);
CREATE INDEX paid_api_quotes_by_job ON paid_api_quotes (job_id, created_at);
