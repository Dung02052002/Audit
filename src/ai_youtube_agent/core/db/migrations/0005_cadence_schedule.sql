-- D-050 Cadence Settings (Prompt Pack v8, prompt #050).
-- The daily limits stay in their two columns, which DailyLimitGate reads.
-- The channel time zone and the Shorts and LongForm publish preferences are
-- stored as JSON next to them. A profile with a cadence from before #050 has
-- NULL here and reads as UTC with the default preferences. There is no
-- schedule without daily limits.

ALTER TABLE strategy_profiles
    ADD COLUMN cadence_schedule_json TEXT CHECK (
        cadence_schedule_json IS NULL
        OR (json_valid(cadence_schedule_json) AND cadence_shorts_per_day IS NOT NULL)
    );
