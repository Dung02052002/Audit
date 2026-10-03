-- E-062 Research Failure Recovery (Prompt Pack v8, prompt #062).
-- A running research request is saved after each collected source or
-- failure: queries_done counts the queries fully handled, lease_expires_at is
-- renewed on every save (only running requests have one) and retries counts
-- the runs that retried a finished request's failures. Failures in
-- failures_json may now also carry query, rank and round.

ALTER TABLE research_requests ADD COLUMN queries_done INTEGER NOT NULL DEFAULT 0 CHECK (queries_done BETWEEN 0 AND 10);
ALTER TABLE research_requests ADD COLUMN lease_expires_at TEXT CHECK (lease_expires_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z');
ALTER TABLE research_requests ADD COLUMN retries INTEGER NOT NULL DEFAULT 0 CHECK (retries >= 0);

-- A request left running before this migration saved no progress: its lease
-- is already over, so it can be resumed from the start.
UPDATE research_requests SET lease_expires_at = updated_at WHERE status = 'running';
UPDATE research_requests SET queries_done = json_array_length(queries_json)
WHERE status IN ('completed', 'partial', 'failed');
