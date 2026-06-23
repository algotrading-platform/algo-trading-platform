-- ============================================================
-- migration_multistrategy.sql
--
-- Purpose: allow multiple strategies (RSI Reversal, Volume Spike,
-- Cash-Futures Arbitrage) to run in parallel without their alert
-- states overwriting each other.
--
-- Before: alert_states was unique on (stock, timeframe).
--   → RSI BUY on RELIANCE 5m and Volume Spike BUY on RELIANCE 5m
--     shared ONE row. The second strategy's state clobbered the first,
--     so one strategy's alerts masked the other's.
--
-- After: alert_states is unique on (stock, timeframe, strategy).
--   → Each strategy keeps its own independent transition state.
--
-- Run this ONCE against Azure PostgreSQL (same way the app_config
-- and upstox_tokens migrations were run). It is idempotent and safe
-- to re-run.
-- ============================================================

-- 1. Add the strategy column (defaults to 'RSI Reversal' for existing rows)
ALTER TABLE alert_states
    ADD COLUMN IF NOT EXISTS strategy TEXT NOT NULL DEFAULT 'RSI Reversal';

-- 2. Drop the old 2-column unique constraint if it exists.
--    (Postgres auto-named it alert_states_stock_timeframe_key.)
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'alert_states_stock_timeframe_key'
    ) THEN
        ALTER TABLE alert_states
            DROP CONSTRAINT alert_states_stock_timeframe_key;
    END IF;
END$$;

-- 3. Add the new 3-column unique constraint.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'alert_states_stock_timeframe_strategy_key'
    ) THEN
        ALTER TABLE alert_states
            ADD CONSTRAINT alert_states_stock_timeframe_strategy_key
            UNIQUE (stock, timeframe, strategy);
    END IF;
END$$;

-- 4. Refresh the lookup index to include strategy.
DROP INDEX IF EXISTS idx_alert_states_stock_tf;
CREATE INDEX IF NOT EXISTS idx_alert_states_stock_tf_strat
    ON alert_states (stock, timeframe, strategy);

-- 5. Verify
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'alert_states'
ORDER BY ordinal_position;
