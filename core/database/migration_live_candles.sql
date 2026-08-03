-- ============================================================
-- migration_live_candles.sql
--
-- Creates live_candles_1min, fed by the Upstox WebSocket listener
-- (core/marketdata/ws_listener.py). Holds only TODAY's running
-- 1-minute candles per instrument -- history for older days still
-- comes from the existing REST/yfinance path (UpstoxProvider).
--
-- The still-forming minute is upserted repeatedly (same ts) as new
-- ticks arrive, so it always reflects the latest price.
--
-- Safe to re-run: uses IF NOT EXISTS.
-- ============================================================

CREATE TABLE IF NOT EXISTS live_candles_1min (
    instrument_key TEXT          NOT NULL,
    symbol         TEXT          NOT NULL,   -- yfinance-style, e.g. HDFCBANK.NS
    ts             TIMESTAMPTZ   NOT NULL,   -- candle open time, minute-aligned

    open           NUMERIC(12,2) NOT NULL,
    high           NUMERIC(12,2) NOT NULL,
    low            NUMERIC(12,2) NOT NULL,
    close          NUMERIC(12,2) NOT NULL,
    volume         BIGINT        DEFAULT 0,

    updated_at     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    PRIMARY KEY (instrument_key, ts)
);

-- Fast lookups for "today's candles for this symbol" (resampling)
-- and "latest price for this symbol" (paper_trader._current_price)
CREATE INDEX IF NOT EXISTS idx_live_candles_symbol_ts
    ON live_candles_1min (symbol, ts DESC);
