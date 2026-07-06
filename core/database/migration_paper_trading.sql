-- ============================================================
-- migration_paper_trading.sql
--
-- Creates the paper_positions table for the paper-trading engine.
-- Run once against the Azure PostgreSQL DB (same DB as signals).
--
-- Safe to re-run: uses IF NOT EXISTS.
-- ============================================================

CREATE TABLE IF NOT EXISTS paper_positions (
    id            SERIAL PRIMARY KEY,

    symbol        TEXT        NOT NULL,
    side          TEXT        NOT NULL,          -- 'BUY' | 'SELL'
    quantity      INTEGER     NOT NULL,
    entry_price   NUMERIC(12,2) NOT NULL,
    stop_loss     NUMERIC(12,2) NOT NULL,
    target        NUMERIC(12,2) NOT NULL,

    strategy      TEXT        NOT NULL,
    timeframe     TEXT        NOT NULL,
    risk_amount   NUMERIC(12,2) DEFAULT 0,
    order_id      TEXT        DEFAULT '',        -- sandbox order id

    status        TEXT        NOT NULL DEFAULT 'OPEN',  -- 'OPEN' | 'CLOSED'

    exit_price    NUMERIC(12,2),
    exit_reason   TEXT,                          -- 'signal' | 'stop' | 'target' | 'manual'
    pnl           NUMERIC(12,2),

    opened_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at     TIMESTAMPTZ
);

-- Fast lookups for "is this symbol open?" and the open-count cap
CREATE INDEX IF NOT EXISTS idx_paper_positions_status_symbol
    ON paper_positions (status, symbol);

-- Fast lookups for the closed-trades scorecard by date
CREATE INDEX IF NOT EXISTS idx_paper_positions_status_closed
    ON paper_positions (status, closed_at);