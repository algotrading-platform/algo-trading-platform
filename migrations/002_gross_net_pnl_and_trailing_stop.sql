-- ============================================================
-- migrations/002_gross_net_pnl_and_trailing_stop.sql
--
-- Run this once against your Postgres DB before deploying the new
-- rms.py / paper_trader.py / db.py / dashboard.py.
--
-- Adds:
--   charges, net_pnl                — Gross vs Net P&L (Jwala Jul 11)
--   peak_price, initial_stop_distance — trailing stop state for
--                                        Volume Spike positions
--
-- Safe to run on a table that already has open/closed positions —
-- all four columns default to NULL/0 and existing rows are left
-- alone (net_pnl for old CLOSED rows will show as 0 until you
-- backfill, see note at the bottom).
-- ============================================================

ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS charges NUMERIC DEFAULT 0;
ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS net_pnl NUMERIC DEFAULT 0;
ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS peak_price NUMERIC;
ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS initial_stop_distance NUMERIC;

-- Optional: backfill net_pnl for already-CLOSED trades so historical
-- rows aren't stuck showing net_pnl=0 forever. This applies the same
-- estimate the app uses going forward, retroactively. Skip this if
-- you'd rather leave old trades as pnl-only and only have net_pnl
-- populated from here on.
--
-- UPDATE paper_positions
-- SET net_pnl = pnl  -- placeholder; the real backfill needs the
--                     -- Python charges.py formula, not raw SQL, since
--                     -- it branches on side (BUY vs SELL) for which
--                     -- leg is buy/sell. Ask if you want a one-off
--                     -- Python backfill script instead of doing this
--                     -- by hand in SQL.
-- WHERE status = 'CLOSED';