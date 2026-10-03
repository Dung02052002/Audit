-- F-064 Script Model (Prompt Pack v8, prompt #064).
-- A script version now holds ordered sections (kind, optional title, text,
-- optional seconds) as JSON, a duration target copied from the strategy
-- format, who made it and why, the version it was made from and the strategy
-- version and research report it used. The text column keeps the sections'
-- text joined by blank lines. A claim may name its section.

ALTER TABLE scripts ADD COLUMN sections_json TEXT CHECK (sections_json IS NULL OR (json_valid(sections_json) AND json_type(sections_json) = 'array'));
ALTER TABLE scripts ADD COLUMN duration_min_seconds INTEGER CHECK (duration_min_seconds BETWEEN 1 AND 14400);
ALTER TABLE scripts ADD COLUMN duration_max_seconds INTEGER CHECK (duration_max_seconds BETWEEN 1 AND 14400);
ALTER TABLE scripts ADD COLUMN created_by_kind TEXT CHECK (created_by_kind IN ('user', 'system', 'ai'));
ALTER TABLE scripts ADD COLUMN created_by_id TEXT;
ALTER TABLE scripts ADD COLUMN reason TEXT CHECK (reason IS NULL OR length(reason) <= 500);
-- Checked at commit, so the versions of one transaction may come in any order.
ALTER TABLE scripts ADD COLUMN parent_id TEXT REFERENCES scripts (id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE scripts ADD COLUMN strategy_version INTEGER CHECK (strategy_version >= 1);
ALTER TABLE scripts ADD COLUMN research_report_id TEXT REFERENCES research_reports (id);

ALTER TABLE claims ADD COLUMN section_index INTEGER CHECK (section_index BETWEEN 0 AND 99);

-- A script stored before this migration becomes one body section, and every
-- later version gets the previous version of its item as its parent.
UPDATE scripts
SET sections_json = json_array(json_object('kind', 'body', 'title', NULL, 'text', text, 'seconds', NULL))
WHERE sections_json IS NULL;
UPDATE scripts
SET parent_id = (
    SELECT p.id FROM scripts AS p
    WHERE p.content_item_id = scripts.content_item_id AND p.version = scripts.version - 1
)
WHERE version > 1;
