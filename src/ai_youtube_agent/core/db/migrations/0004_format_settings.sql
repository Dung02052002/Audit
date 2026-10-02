-- D-049 Format Settings (Prompt Pack v8, prompt #049).
-- The Shorts and LongForm production defaults of a strategy are one setting,
-- stored as JSON. Existing profiles get NULL: the format is not configured yet
-- and shows in missing_settings until a user saves it.

ALTER TABLE strategy_profiles
    ADD COLUMN format_json TEXT CHECK (json_valid(format_json));
