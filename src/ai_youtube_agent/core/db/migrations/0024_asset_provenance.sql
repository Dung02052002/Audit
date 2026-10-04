-- G-077 Provenance Record (Prompt Pack v8, prompt #077).
-- One row per entry of the source and licence history of an asset (the
-- Provenance entity of G-077). An asset has any number of entries and the
-- newest one (by created_at, then insertion order) is the current one.
-- An entry holds an optional source URL, the optional time the file was
-- retrieved, an optional licence name, licence URL and licence reference, an
-- optional attribution, owner and proof, an optional SHA-256 checksum, who
-- recorded it (user, system or ai) and when. At least one of the details must be
-- set. URLs are at most 2048 characters and start with http or https. The
-- checksum is 64 lowercase hexadecimal characters. That a URL has no
-- whitespace and no user name or password, that the time of retrieval is not
-- after the time of recording, and the rules of the asset category (a licensed
-- asset needs a licence name or reference, a user_owned asset needs an owner)
-- are checked by the recorder in Python, not in SQL.
-- The limits are the Python constants MAX_URL_LENGTH, MAX_LICENSE_NAME,
-- MAX_LICENSE_REF, MAX_ATTRIBUTION, MAX_OWNER and MAX_PROOF of
-- content/provenance.py.
-- There is no unique key: the history may repeat an earlier statement after a
-- different one. Rows are never changed or removed. This migration changes no
-- other table: assets and rights_records are not touched by a provenance record.

CREATE TABLE asset_provenance (
    id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES assets (id),
    source_url TEXT CHECK (source_url IS NULL OR (length(source_url) BETWEEN 1 AND 2048 AND source_url LIKE 'http%://%')),
    retrieved_at TEXT CHECK (retrieved_at IS NULL OR retrieved_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    license_name TEXT CHECK (license_name IS NULL OR length(trim(license_name)) BETWEEN 1 AND 200),
    license_url TEXT CHECK (license_url IS NULL OR (length(license_url) BETWEEN 1 AND 2048 AND license_url LIKE 'http%://%')),
    license_ref TEXT CHECK (license_ref IS NULL OR length(trim(license_ref)) BETWEEN 1 AND 500),
    attribution TEXT CHECK (attribution IS NULL OR length(trim(attribution)) BETWEEN 1 AND 500),
    owner TEXT CHECK (owner IS NULL OR length(trim(owner)) BETWEEN 1 AND 200),
    file_sha256 TEXT CHECK (file_sha256 IS NULL OR (length(file_sha256) = 64 AND file_sha256 NOT GLOB '*[^0-9a-f]*')),
    proof TEXT CHECK (proof IS NULL OR length(trim(proof)) BETWEEN 1 AND 1000),
    recorded_by_kind TEXT NOT NULL CHECK (recorded_by_kind IN ('user', 'system', 'ai')),
    recorded_by_id TEXT NOT NULL,
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    CHECK (
        source_url IS NOT NULL OR retrieved_at IS NOT NULL OR license_name IS NOT NULL
        OR license_url IS NOT NULL OR license_ref IS NOT NULL OR attribution IS NOT NULL
        OR owner IS NOT NULL OR file_sha256 IS NOT NULL OR proof IS NOT NULL
    )
) STRICT;

CREATE INDEX asset_provenance_by_asset ON asset_provenance (asset_id, created_at);
