-- G-081 AI Disclosure Rule (Prompt Pack v8, prompt #081).
-- One row per decision whether a content item needs an AI disclosure (the
-- DisclosureRecord entity of G-081). A content item has any number of decisions
-- and the newest one (by created_at, then insertion order) is the current one.
-- A row holds the item and its channel, the rule set that decided (its id and
-- its stored version, which must read <id>-rules-v<n> with n a positive integer
-- without a leading zero), the decision (required
-- or not_required), the rationale (a JSON array with at least one entry, one per
-- rule), the sources the decision rests on (a JSON object: the facts declared
-- true and the ids of the generated visual assets), who decided (user, system or
-- ai) and when. That the rationale is a non-empty array and the sources an
-- object is checked here; that the decision matches the rationale, that the
-- entries and the sources have the right shape and that the time is UTC are
-- checked by the entity in Python. The rules themselves
-- (content/disclosure_rule.py) are code, not SQL.
-- There is no unique key: the history may repeat an earlier decision after a
-- different one. Rows are never changed or removed. This migration changes no
-- other table: content_items and channels are only referenced.

CREATE TABLE disclosure_decisions (
    id TEXT PRIMARY KEY,
    content_item_id TEXT NOT NULL REFERENCES content_items (id),
    channel_id TEXT NOT NULL REFERENCES channels (id),
    rule_set_id TEXT NOT NULL CHECK (length(trim(rule_set_id)) BETWEEN 1 AND 50),
    rule_set_version TEXT NOT NULL CHECK (length(trim(rule_set_version)) BETWEEN 1 AND 50),
    decision TEXT NOT NULL CHECK (decision IN ('required', 'not_required')),
    rationale_json TEXT NOT NULL CHECK (json_valid(rationale_json) AND json_type(rationale_json) = 'array' AND json_array_length(rationale_json) >= 1),
    sources_json TEXT NOT NULL CHECK (json_valid(sources_json) AND json_type(sources_json) = 'object'),
    decided_by_kind TEXT NOT NULL CHECK (decided_by_kind IN ('user', 'system', 'ai')),
    decided_by_id TEXT NOT NULL CHECK (length(trim(decided_by_id)) >= 1),
    created_at TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    CHECK (
        substr(rule_set_version, 1, length(rule_set_id) + 8) = rule_set_id || '-rules-v'
        AND substr(rule_set_version, length(rule_set_id) + 9) GLOB '[1-9]*'
        AND substr(rule_set_version, length(rule_set_id) + 9) NOT GLOB '*[^0-9]*'
    )
) STRICT;

CREATE INDEX disclosure_decisions_by_item ON disclosure_decisions (content_item_id, created_at);
