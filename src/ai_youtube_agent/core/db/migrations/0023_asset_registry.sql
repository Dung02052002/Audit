-- G-076 Asset Registry (Prompt Pack v8, prompt #076).
-- One row per asset a channel may use in its videos (the Asset entity of
-- G-075) and one row per use of an asset by a content item of the same channel.
-- An asset holds the channel, the kind and the category (both closed lists in
-- Python, AssetKind and AssetCategory), the title (1-200), the source (1-500), an
-- optional artifact the system generated, an optional licence reference
-- (1-500), attribution (1-500) and owner (1-200), and the creation time.
-- source_key and title_key hold the normalised source and title (NFC of the case-folded
-- NFC of the stripped text): a channel has at most one asset for a pair of
-- keys, and one asset for an artifact (a partial unique index). The keys are
-- computed in Python, SQL only holds them non-empty.
-- The category rules of Asset repeat in SQL: licensed needs a licence reference,
-- user_owned needs an owner, only generated may have an artifact, and a
-- generated asset has a provider as its source, not 'user' (compared with
-- lower(trim(source)), so only ASCII case is folded here, Python folds all).
-- The limits are the Python constants MAX_TITLE, MAX_SOURCE, MAX_LICENSE_REF,
-- MAX_ATTRIBUTION and MAX_OWNER of content/asset.py, and MAX_PURPOSE of
-- content/asset_usage.py.
-- An asset usage links an asset to a content item with an optional purpose
-- (1-200), who attached it and when. A pair of asset and content item has at
-- most one usage. That both belong to one channel is checked by the registry in
-- Python, not in SQL.
-- Rows are never changed or removed. This migration changes no other table:
-- rights_records.asset_ref stays an opaque text that the registry fills with
-- the asset id.

CREATE TABLE assets (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels (id),
    kind TEXT NOT NULL CHECK (kind IN ('image', 'video_clip', 'audio', 'music', 'voice', 'font', 'subtitle', 'template', 'other')),
    category TEXT NOT NULL CHECK (category IN ('generated', 'licensed', 'public_domain', 'user_owned', 'unknown')),
    title TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 200),
    source TEXT NOT NULL CHECK (length(trim(source)) BETWEEN 1 AND 500),
    source_key TEXT NOT NULL CHECK (source_key <> ''),
    title_key TEXT NOT NULL CHECK (title_key <> ''),
    artifact_id TEXT REFERENCES artifacts (id),
    license_ref TEXT CHECK (license_ref IS NULL OR length(trim(license_ref)) BETWEEN 1 AND 500),
    attribution TEXT CHECK (attribution IS NULL OR length(trim(attribution)) BETWEEN 1 AND 500),
    owner TEXT CHECK (owner IS NULL OR length(trim(owner)) BETWEEN 1 AND 200),
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    CHECK (category <> 'licensed' OR license_ref IS NOT NULL),
    CHECK (category <> 'user_owned' OR owner IS NOT NULL),
    CHECK (category = 'generated' OR artifact_id IS NULL),
    CHECK (category <> 'generated' OR lower(trim(source)) <> 'user'),
    UNIQUE (channel_id, source_key, title_key)
) STRICT;

CREATE UNIQUE INDEX assets_by_artifact ON assets (artifact_id) WHERE artifact_id IS NOT NULL;

CREATE INDEX assets_by_channel ON assets (channel_id, created_at);

CREATE TABLE asset_usages (
    id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES assets (id),
    content_item_id TEXT NOT NULL REFERENCES content_items (id),
    purpose TEXT CHECK (purpose IS NULL OR length(trim(purpose)) BETWEEN 1 AND 200),
    attached_by_kind TEXT NOT NULL CHECK (attached_by_kind IN ('user', 'system', 'ai')),
    attached_by_id TEXT NOT NULL,
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    UNIQUE (asset_id, content_item_id)
) STRICT;

CREATE INDEX asset_usages_by_item ON asset_usages (content_item_id, created_at);
