-- ============================================================
-- Supabase Table Setup
-- Run this once in Supabase SQL Editor
-- Project Settings → SQL Editor → New Query → Paste → Run
-- ============================================================


-- ============================================================
-- 1. SIGNALS TABLE
-- Stores every BUY/SELL signal logged by the scheduler
-- ============================================================

CREATE TABLE IF NOT EXISTS signals (
    id          BIGSERIAL PRIMARY KEY,
    timestamp   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    stock       TEXT        NOT NULL,
    timeframe   TEXT        NOT NULL,
    signal      TEXT        NOT NULL CHECK (signal IN ('BUY', 'SELL', 'HOLD')),
    rsi         NUMERIC(6,2),
    price       NUMERIC(12,2),
    strategy    TEXT        NOT NULL DEFAULT 'RSI Reversal'
);

-- Index for fast dashboard queries
CREATE INDEX IF NOT EXISTS idx_signals_stock_tf
    ON signals (stock, timeframe);

CREATE INDEX IF NOT EXISTS idx_signals_timestamp
    ON signals (timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_signals_strategy
    ON signals (strategy);

-- Auto-delete signals older than 7 days (keeps DB clean)
-- This runs as a Supabase cron job (set up in Dashboard → Database → Cron)
-- SELECT cron.schedule('cleanup-signals', '0 0 * * *',
--   $$DELETE FROM signals WHERE timestamp < NOW() - INTERVAL '7 days'$$);


-- ============================================================
-- 2. ALERT STATES TABLE
-- Tracks last known signal per stock+timeframe for deduplication
-- Replaces last_signals.csv
-- ============================================================

CREATE TABLE IF NOT EXISTS alert_states (
    id          BIGSERIAL PRIMARY KEY,
    stock       TEXT        NOT NULL,
    timeframe   TEXT        NOT NULL,
    signal      TEXT        NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (stock, timeframe)  -- one row per stock+timeframe
);

CREATE INDEX IF NOT EXISTS idx_alert_states_stock_tf
    ON alert_states (stock, timeframe);


-- ============================================================
-- 3. BACKTEST RESULTS TABLE
-- Stores backtest summary per symbol+timeframe+strategy
-- Replaces backtest_results.csv
-- ============================================================

CREATE TABLE IF NOT EXISTS backtest_results (
    id          BIGSERIAL PRIMARY KEY,
    symbol      TEXT        NOT NULL,
    name        TEXT,
    timeframe   TEXT        NOT NULL,
    category    TEXT,
    strategy    TEXT        NOT NULL DEFAULT 'RSI Reversal',
    trades      INTEGER     DEFAULT 0,
    pnl         NUMERIC(12,2) DEFAULT 0,
    pnl_pct     NUMERIC(8,2)  DEFAULT 0,
    win_rate    NUMERIC(6,1)  DEFAULT 0,
    wins        INTEGER     DEFAULT 0,
    losses      INTEGER     DEFAULT 0,
    period      TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (symbol, timeframe, strategy)
);

CREATE INDEX IF NOT EXISTS idx_backtest_symbol_tf
    ON backtest_results (symbol, timeframe);


-- ============================================================
-- VERIFY — run after setup to check tables exist
-- ============================================================

SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN ('signals', 'alert_states', 'backtest_results');