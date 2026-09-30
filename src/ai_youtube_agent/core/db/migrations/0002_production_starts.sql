-- C-036 Daily Limit Gate (Prompt Pack v8, prompt #036).
-- One row each time a content item starts production (draft -> generating),
-- so the daily limit gate can count production per channel, type and UTC day.
-- Append-only, like audit_events.

CREATE TABLE production_starts (
    id TEXT PRIMARY KEY,
    content_item_id TEXT NOT NULL REFERENCES content_items (id),
    channel_id TEXT NOT NULL REFERENCES channels (id),
    content_type TEXT NOT NULL CHECK (content_type IN ('shorts', 'longform')),
    started_at TEXT NOT NULL CHECK (started_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')
) STRICT;

CREATE INDEX production_starts_by_channel
    ON production_starts (channel_id, content_type, started_at);

CREATE TRIGGER production_starts_no_update
BEFORE UPDATE ON production_starts
BEGIN
    SELECT RAISE(ABORT, 'production_starts is append-only');
END;

CREATE TRIGGER production_starts_no_delete
BEFORE DELETE ON production_starts
BEGIN
    SELECT RAISE(ABORT, 'production_starts is append-only');
END;
