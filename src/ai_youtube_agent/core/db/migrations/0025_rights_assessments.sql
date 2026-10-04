-- G-078 Rights Risk Engine (Prompt Pack v8, prompt #078).
-- One row per assessment of a rights record by the risk engine (the
-- RightsAssessment entity of G-078). A rights record has any number of
-- assessments and the newest one (by created_at, then insertion order) is the
-- current one. A row holds the record and the content item it judges, the asset
-- (NULL when the asset_ref is not a registered asset of the channel), the level
-- (low, medium or high, never unknown), the stable rule codes that gave the
-- level (a JSON array with at least one code), the version of the rules, the
-- provenance record that was current when the asset was judged (NULL when there
-- was none or the asset is not registered), who assessed (user, system or ai)
-- and when. That the level is not unknown, that a provenance id implies an
-- asset, and that the code array is not empty are checked here and by the
-- entity in Python; that each code is a non-empty, unpadded string is checked in
-- Python only. The rules themselves (content/rights_assessment.py) are code,
-- not SQL.
-- There is no unique key: the history may repeat an earlier outcome after a
-- different one. Rows are never changed or removed. This migration changes no
-- other table: rights_records, assets and asset_provenance are only referenced.

CREATE TABLE rights_assessments (
    id TEXT PRIMARY KEY,
    rights_record_id TEXT NOT NULL REFERENCES rights_records (id),
    content_item_id TEXT NOT NULL REFERENCES content_items (id),
    asset_id TEXT REFERENCES assets (id) CHECK (asset_id IS NULL OR length(trim(asset_id)) >= 1),
    level TEXT NOT NULL CHECK (level IN ('unknown', 'low', 'medium', 'high')),
    rule_codes_json TEXT NOT NULL CHECK (json_valid(rule_codes_json) AND json_type(rule_codes_json) = 'array' AND json_array_length(rule_codes_json) >= 1),
    rules_version TEXT NOT NULL CHECK (length(trim(rules_version)) BETWEEN 1 AND 50),
    provenance_id TEXT REFERENCES asset_provenance (id) CHECK (provenance_id IS NULL OR length(trim(provenance_id)) >= 1),
    assessed_by_kind TEXT NOT NULL CHECK (assessed_by_kind IN ('user', 'system', 'ai')),
    assessed_by_id TEXT NOT NULL CHECK (length(trim(assessed_by_id)) >= 1),
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    CHECK (level <> 'unknown'),
    CHECK (provenance_id IS NULL OR asset_id IS NOT NULL)
) STRICT;

CREATE INDEX rights_assessments_by_record ON rights_assessments (rights_record_id, created_at);
CREATE INDEX rights_assessments_by_item ON rights_assessments (content_item_id, created_at);
