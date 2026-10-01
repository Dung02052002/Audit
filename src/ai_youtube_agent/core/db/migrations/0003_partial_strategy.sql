-- D-044 Market Settings (Prompt Pack v8, prompt #044).
-- A strategy profile is configured one section at a time (#044-#052), and
-- #053 validates missing sections before a run. Every section may now be
-- NULL (not configured yet). A section stored in several columns is either
-- fully set or fully NULL. SQLite cannot drop NOT NULL, so the table is
-- rebuilt; the runner turns foreign keys off for the rebuild and checks them
-- before it commits, so content_items keeps its references.

CREATE TABLE strategy_profiles_v3 (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL UNIQUE REFERENCES channels (id),
    market_country TEXT CHECK (length(market_country) = 2),
    primary_language TEXT,
    secondary_languages_json TEXT CHECK (json_valid(secondary_languages_json) AND json_type(secondary_languages_json) = 'array'),
    audience_json TEXT CHECK (json_valid(audience_json)),
    niche_json TEXT CHECK (json_valid(niche_json)),
    brand_json TEXT CHECK (json_valid(brand_json)),
    cadence_shorts_per_day INTEGER CHECK (cadence_shorts_per_day >= 0),
    cadence_longform_per_day INTEGER CHECK (cadence_longform_per_day >= 0),
    budget_currency TEXT CHECK (length(budget_currency) = 3),
    budget_daily_limit TEXT CHECK (budget_daily_limit GLOB '[0-9]*' AND budget_daily_limit NOT GLOB '*[^0-9.]*' AND budget_daily_limit NOT GLOB '*.*.*' AND budget_daily_limit NOT GLOB '*.'),
    budget_monthly_limit TEXT CHECK (budget_monthly_limit GLOB '[0-9]*' AND budget_monthly_limit NOT GLOB '*[^0-9.]*' AND budget_monthly_limit NOT GLOB '*.*.*' AND budget_monthly_limit NOT GLOB '*.'),
    monetization_json TEXT CHECK (json_valid(monetization_json)),
    version INTEGER NOT NULL CHECK (version >= 1),
    updated_by_kind TEXT NOT NULL CHECK (updated_by_kind = 'user'),
    updated_by_id TEXT NOT NULL,
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    updated_at TEXT NOT NULL CHECK (updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    CHECK (updated_at >= created_at),
    CHECK ((primary_language IS NULL) = (secondary_languages_json IS NULL)),
    CHECK ((cadence_shorts_per_day IS NULL) = (cadence_longform_per_day IS NULL)),
    CHECK ((budget_currency IS NULL) = (budget_daily_limit IS NULL)),
    CHECK ((budget_currency IS NULL) = (budget_monthly_limit IS NULL))
) STRICT;

INSERT INTO strategy_profiles_v3 (
    id, channel_id, market_country, primary_language, secondary_languages_json,
    audience_json, niche_json, brand_json, cadence_shorts_per_day,
    cadence_longform_per_day, budget_currency, budget_daily_limit,
    budget_monthly_limit, monetization_json, version, updated_by_kind,
    updated_by_id, created_at, updated_at
)
SELECT
    id, channel_id, market_country, primary_language, secondary_languages_json,
    audience_json, niche_json, brand_json, cadence_shorts_per_day,
    cadence_longform_per_day, budget_currency, budget_daily_limit,
    budget_monthly_limit, monetization_json, version, updated_by_kind,
    updated_by_id, created_at, updated_at
FROM strategy_profiles;

DROP TABLE strategy_profiles;

ALTER TABLE strategy_profiles_v3 RENAME TO strategy_profiles;
