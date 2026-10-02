-- D-051 Budget Settings (Prompt Pack v8, prompt #051).
-- The budget keeps its currency and limit columns, which BudgetGate reads.
-- The alert thresholds (percentages of each limit) are stored as a JSON array
-- next to them. A budget from before #051 has NULL here and reads with the
-- default thresholds 50, 80 and 100. There are no thresholds without a budget.

ALTER TABLE strategy_profiles
    ADD COLUMN budget_alert_thresholds_json TEXT CHECK (
        budget_alert_thresholds_json IS NULL
        OR (
            json_valid(budget_alert_thresholds_json)
            AND json_type(budget_alert_thresholds_json) = 'array'
            AND budget_currency IS NOT NULL
        )
    );
