-- E-060 Research Report (Prompt Pack v8, prompt #060).
-- One structured report per finished research request, stored as JSON with
-- its schema version and written once. The overall uncertainty is a column
-- so reports can be listed by it.

CREATE TABLE research_reports (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE REFERENCES research_requests (id),
    channel_id TEXT NOT NULL REFERENCES channels (id),
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    uncertainty TEXT NOT NULL CHECK (uncertainty IN ('low', 'medium', 'high')),
    report_json TEXT NOT NULL CHECK (json_valid(report_json) AND json_type(report_json) = 'object'),
    generated_at TEXT NOT NULL CHECK (generated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')
) STRICT;
