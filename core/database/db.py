# ============================================================
# core/database/db.py
#
# Central database layer using Azure PostgreSQL.
# Drop-in replacement for the Supabase version.
# All function signatures and return types are identical —
# zero changes needed in any other file.
#
# Environment variables required:
#   DATABASE_URL : postgresql://algoadmin:password@host:5432/postgres?sslmode=require
#
# Fallback (if DATABASE_URL not set):
#   AZURE_DB_HOST     : ariqt-algo-trading-db-001.postgres.database.azure.com
#   AZURE_DB_USER     : algoadmin
#   AZURE_DB_PASSWORD : Trading@2024!
#   AZURE_DB_NAME     : postgres
# ============================================================

import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pandas as pd
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# CONNECTION
# ============================================================

def _get_conn_params() -> dict:
    """
    Returns psycopg2 connection parameters.
    Prefers DATABASE_URL, falls back to individual vars.
    """
    url = os.getenv("DATABASE_URL", "")

    if url:
        return {"dsn": url}

    return {
        "host":     os.getenv("AZURE_DB_HOST",     "ariqt-algo-trading-db-001.postgres.database.azure.com"),
        "port":     int(os.getenv("AZURE_DB_PORT", "5432")),
        "user":     os.getenv("AZURE_DB_USER",     "algoadmin"),
        "password": os.getenv("AZURE_DB_PASSWORD", ""),
        "dbname":   os.getenv("AZURE_DB_NAME",     "postgres"),
        "sslmode":  "require",
    }


@contextmanager
def _get_cursor():
    """
    Context manager that yields a psycopg2 cursor.
    Commits on success, rolls back on error, always closes.
    """
    params = _get_conn_params()
    conn   = None
    try:
        if "dsn" in params:
            conn = psycopg2.connect(params["dsn"], connect_timeout=15)
        else:
            conn = psycopg2.connect(**params, connect_timeout=15)

        conn.autocommit = False
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        yield cur
        conn.commit()
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


# ============================================================
# SIGNALS TABLE
# ============================================================

def insert_signal(
    stock:     str,
    timeframe: str,
    signal:    str,
    rsi:       float,
    price:     float,
    strategy:  str = "RSI Reversal",
) -> bool:
    """
    Insert a new signal row.
    Returns True if inserted, False on error.
    """
    try:
        with _get_cursor() as cur:
            cur.execute("""
                INSERT INTO signals
                    (timestamp, stock, timeframe, signal, rsi, price, strategy)
                VALUES
                    (NOW(), %s, %s, %s, %s, %s, %s)
            """, (
                stock,
                timeframe,
                signal,
                round(float(rsi),   2),
                round(float(price), 2),
                strategy,
            ))
        return True
    except Exception as e:
        print(f"[DB] insert_signal error: {e}")
        return False


def get_signals(
    timeframe: str = None,
    strategy:  str = None,
    days:      int = 7,
) -> pd.DataFrame:
    """
    Fetch signals from last N days.
    Optionally filter by timeframe and strategy.
    Returns DataFrame sorted newest first.
    """
    try:
        # Signals are stored with NOW() (UTC, timestamptz). The cutoff MUST
        # be timezone-aware UTC too — a naive datetime.now() is local IST and
        # would shift the window ~5.5h, silently hiding the most recent rows
        # (this was the "dashboard shows no records" bug).
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        conditions = ["timestamp >= %s"]
        params     = [cutoff]

        if timeframe:
            conditions.append("timeframe = %s")
            params.append(timeframe)
        if strategy:
            conditions.append("strategy = %s")
            params.append(strategy)

        where = " AND ".join(conditions)

        with _get_cursor() as cur:
            cur.execute(
                f"SELECT * FROM signals WHERE {where} ORDER BY timestamp DESC",
                params,
            )
            rows = cur.fetchall()

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame([dict(r) for r in rows])

        # Rename columns to match dashboard expectations
        df = df.rename(columns={
            "timestamp": "Timestamp",
            "stock":     "Stock",
            "timeframe": "Timeframe",
            "signal":    "Signal",
            "rsi":       "RSI",
            "price":     "Price",
            "strategy":  "Strategy",
        })

        df["Timestamp"] = pd.to_datetime(df["Timestamp"], utc=True)
        return df

    except Exception as e:
        print(f"[DB] get_signals error: {e}")
        return pd.DataFrame()


def get_last_signal(stock: str, timeframe: str, strategy: str = None) -> str | None:
    """
    Returns the most recent signal for a stock+timeframe (+strategy).
    Used for deduplication in SignalLogger.

    When strategy is given, dedup is per-strategy so parallel strategies
    (e.g. RSI Reversal and Volume Spike) do not mask each other.
    """
    try:
        with _get_cursor() as cur:
            if strategy is not None:
                cur.execute("""
                    SELECT signal FROM signals
                    WHERE stock = %s AND timeframe = %s AND strategy = %s
                    ORDER BY timestamp DESC
                    LIMIT 1
                """, (stock, timeframe, strategy))
            else:
                cur.execute("""
                    SELECT signal FROM signals
                    WHERE stock = %s AND timeframe = %s
                    ORDER BY timestamp DESC
                    LIMIT 1
                """, (stock, timeframe))
            row = cur.fetchone()

        if row:
            return row["signal"]
        return None

    except Exception as e:
        print(f"[DB] get_last_signal error: {e}")
        return None


def get_last_scan_time() -> str | None:
    """
    Returns how long ago the last scan ran.
    Reads from app_config LAST_SCAN_TIME — updated after every scan.
    Falls back to last signal timestamp if app_config not set.
    """
    import pytz
    IST = pytz.timezone("Asia/Kolkata")

    def _format(last_ts):
        now = datetime.now(IST)
        if last_ts.tzinfo is None:
            last_ts = IST.localize(last_ts)
        diff = now - last_ts.astimezone(IST)
        mins = int(diff.total_seconds() / 60)
        if mins < 2:  return "just now"
        if mins < 60: return f"{mins}m ago"
        hours = mins // 60
        return f"{hours}h {mins % 60}m ago"

    # Check app_config first
    try:
        val = get_config("LAST_SCAN_TIME")
        if val:
            last_ts = datetime.fromisoformat(val)
            return _format(last_ts)
    except Exception:
        pass

    # Fallback — last signal timestamp
    try:
        with _get_cursor() as cur:
            cur.execute("""
                SELECT timestamp FROM signals
                ORDER BY timestamp DESC
                LIMIT 1
            """)
            row = cur.fetchone()

        if not row:
            return None

        last_ts = row["timestamp"]
        if hasattr(last_ts, "tzinfo") and last_ts.tzinfo is None:
            last_ts = IST.localize(last_ts)
        return _format(last_ts)

    except Exception as e:
        print(f"[DB] get_last_scan_time error: {e}")
        return None


# ============================================================
# ALERT STATES TABLE
# ============================================================

def get_alert_state(stock: str, timeframe: str, strategy: str = None) -> str | None:
    """
    Returns last alerted signal for stock+timeframe (+strategy).

    When strategy is given, each strategy keeps its own transition
    state so parallel strategies do not overwrite each other's alerts.
    """
    try:
        with _get_cursor() as cur:
            if strategy is not None:
                cur.execute("""
                    SELECT signal FROM alert_states
                    WHERE stock = %s AND timeframe = %s AND strategy = %s
                    LIMIT 1
                """, (stock, timeframe, strategy))
            else:
                cur.execute("""
                    SELECT signal FROM alert_states
                    WHERE stock = %s AND timeframe = %s
                    LIMIT 1
                """, (stock, timeframe))
            row = cur.fetchone()

        if row:
            return row["signal"]
        return None

    except Exception as e:
        print(f"[DB] get_alert_state error: {e}")
        return None


def upsert_alert_state(
    stock: str,
    timeframe: str,
    signal: str,
    strategy: str = "RSI Reversal",
) -> None:
    """
    Insert or update the alert state for stock+timeframe+strategy.

    Requires the alert_states table to have a `strategy` column and a
    UNIQUE (stock, timeframe, strategy) constraint — see
    core/database/migration_multistrategy.sql.

    Falls back to the legacy (stock, timeframe) conflict target if the
    migration has not been applied yet, so deploys never hard-fail.
    """
    try:
        with _get_cursor() as cur:
            cur.execute("""
                INSERT INTO alert_states (stock, timeframe, strategy, signal, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (stock, timeframe, strategy)
                DO UPDATE SET
                    signal     = EXCLUDED.signal,
                    updated_at = NOW()
            """, (stock, timeframe, strategy, signal))
    except Exception as e:
        # Migration not yet applied — fall back to legacy 2-column upsert
        # so the scanner keeps working until the SQL migration is run.
        print(f"[DB] upsert_alert_state (strategy-aware) failed, "
              f"falling back to legacy: {e}")
        try:
            with _get_cursor() as cur:
                cur.execute("""
                    INSERT INTO alert_states (stock, timeframe, signal, updated_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (stock, timeframe)
                    DO UPDATE SET
                        signal     = EXCLUDED.signal,
                        updated_at = NOW()
                """, (stock, timeframe, signal))
        except Exception as e2:
            print(f"[DB] upsert_alert_state legacy fallback error: {e2}")


# ============================================================
# BACKTEST RESULTS TABLE
# ============================================================

def upsert_backtest(
    symbol:    str,
    name:      str,
    timeframe: str,
    category:  str,
    trades:    int,
    pnl:       float,
    pnl_pct:   float,
    win_rate:  float,
    wins:      int,
    losses:    int,
    period:    str,
    strategy:  str = "RSI Reversal",
) -> None:
    """Insert or update backtest result for symbol+timeframe+strategy."""
    try:
        with _get_cursor() as cur:
            cur.execute("""
                INSERT INTO backtest_results
                    (symbol, name, timeframe, category, strategy,
                     trades, pnl, pnl_pct, win_rate, wins, losses, period, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (symbol, timeframe, strategy)
                DO UPDATE SET
                    trades     = EXCLUDED.trades,
                    pnl        = EXCLUDED.pnl,
                    pnl_pct    = EXCLUDED.pnl_pct,
                    win_rate   = EXCLUDED.win_rate,
                    wins       = EXCLUDED.wins,
                    losses     = EXCLUDED.losses,
                    period     = EXCLUDED.period,
                    updated_at = NOW()
            """, (
                symbol, name, timeframe, category, strategy,
                trades,
                round(float(pnl),      2),
                round(float(pnl_pct),  2),
                round(float(win_rate), 1),
                wins, losses, period,
            ))
    except Exception as e:
        print(f"[DB] upsert_backtest error: {e}")


def get_backtest_results(
    timeframe: str = None,
    strategy:  str = None,
) -> pd.DataFrame:
    """Fetch backtest results, optionally filtered."""
    try:
        conditions = []
        params     = []

        if timeframe:
            conditions.append("timeframe = %s")
            params.append(timeframe)
        if strategy:
            conditions.append("strategy = %s")
            params.append(strategy)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        with _get_cursor() as cur:
            cur.execute(
                f"SELECT * FROM backtest_results {where} ORDER BY updated_at DESC",
                params,
            )
            rows = cur.fetchall()

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame([dict(r) for r in rows])
        df = df.rename(columns={
            "symbol":    "Symbol",
            "name":      "Name",
            "timeframe": "Timeframe",
            "category":  "Category",
            "strategy":  "Strategy",
            "trades":    "Trades",
            "pnl":       "PnL",
            "pnl_pct":   "PnL %",
            "win_rate":  "Win Rate %",
            "wins":      "Wins",
            "losses":    "Losses",
            "period":    "Period",
        })
        return df

    except Exception as e:
        print(f"[DB] get_backtest_results error: {e}")
        return pd.DataFrame()


# ============================================================
# UPSTOX TOKENS TABLE
# ============================================================

def save_upstox_token(access_token: str) -> bool:
    """Save Upstox access token. Expires at 3:30 AM next day IST."""
    try:
        import pytz
        IST        = pytz.timezone("Asia/Kolkata")
        now        = datetime.now(IST)
        expires_at = (now + timedelta(days=1)).replace(
            hour=3, minute=30, second=0, microsecond=0
        )
        with _get_cursor() as cur:
            cur.execute("""
                INSERT INTO upstox_tokens (access_token, created_at, expires_at)
                VALUES (%s, NOW(), %s)
            """, (access_token, expires_at))
        return True
    except Exception as e:
        print(f"[DB] save_upstox_token error: {e}")
        return False


def get_upstox_token() -> str | None:
    """
    Fetch the latest valid Upstox access token.
    Returns None if not found or expired.
    """
    try:
        import pytz
        IST = pytz.timezone("Asia/Kolkata")

        with _get_cursor() as cur:
            cur.execute("""
                SELECT access_token, expires_at
                FROM upstox_tokens
                ORDER BY created_at DESC
                LIMIT 1
            """)
            row = cur.fetchone()

        if not row:
            return None

        expires_at = row["expires_at"]
        if expires_at.tzinfo is None:
            expires_at = IST.localize(expires_at)

        if datetime.now(IST) >= expires_at:
            return None

        return row["access_token"]

    except Exception as e:
        print(f"[DB] get_upstox_token error: {e}")
        return None


# ============================================================
# APP CONFIG TABLE
# ============================================================

def get_config(key: str) -> str | None:
    """Get a config value by key from app_config table."""
    try:
        with _get_cursor() as cur:
            cur.execute("""
                SELECT value FROM app_config
                WHERE key = %s
                LIMIT 1
            """, (key,))
            row = cur.fetchone()

        if row:
            return row["value"]
        return None

    except Exception as e:
        print(f"[DB] get_config error: {e}")
        return None


def set_config(key: str, value: str) -> bool:
    """Set a config value — upserts into app_config table."""
    try:
        with _get_cursor() as cur:
            cur.execute("""
                INSERT INTO app_config (key, value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (key)
                DO UPDATE SET
                    value      = EXCLUDED.value,
                    updated_at = NOW()
            """, (key, value))
        return True
    except Exception as e:
        print(f"[DB] set_config error: {e}")
        return False
    
    # ============================================================
# PAPER TRADING — append these to core/database/db.py
#
# Matches the existing db.py patterns exactly:
#   - _get_cursor() context manager
#   - RealDictCursor rows
#   - server-side NOW() for timestamps (timestamptz)
#   - returns bool / DataFrame / dict like the rest of the module
#
# Requires the paper_positions table — see migration_paper_trading.sql
# ============================================================


def open_paper_position(
    symbol:      str,
    side:        str,
    quantity:    int,
    entry_price: float,
    stop_loss:   float,
    target:      float,
    strategy:    str,
    timeframe:   str,
    risk_amount: float = 0.0,
    order_id:    str   = "",
) -> bool:
    """Insert a new OPEN paper position. Returns True on success."""
    try:
        with _get_cursor() as cur:
            cur.execute("""
                INSERT INTO paper_positions
                    (symbol, side, quantity, entry_price, stop_loss, target,
                     strategy, timeframe, risk_amount, order_id,
                     status, opened_at)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'OPEN', NOW())
            """, (
                symbol, side, int(quantity),
                round(float(entry_price), 2),
                round(float(stop_loss),   2),
                round(float(target),      2),
                strategy, timeframe,
                round(float(risk_amount), 2),
                order_id,
            ))
        return True
    except Exception as e:
        print(f"[DB] open_paper_position error: {e}")
        return False


def close_paper_position(
    position_id: int,
    exit_price:  float,
    exit_reason: str = "signal",
) -> bool:
    """
    Close an OPEN position: set exit price/time, compute realized P&L.
    P&L = (exit-entry)*qty for BUY, (entry-exit)*qty for SELL.
    Returns True on success.
    """
    try:
        with _get_cursor() as cur:
            # fetch the open position
            cur.execute("""
                SELECT side, quantity, entry_price
                FROM paper_positions
                WHERE id = %s AND status = 'OPEN'
                LIMIT 1
            """, (position_id,))
            row = cur.fetchone()
            if not row:
                return False

            qty   = int(row["quantity"])
            entry = float(row["entry_price"])
            exitp = round(float(exit_price), 2)

            if row["side"] == "BUY":
                pnl = (exitp - entry) * qty
            else:  # SELL (short)
                pnl = (entry - exitp) * qty

            cur.execute("""
                UPDATE paper_positions
                SET status      = 'CLOSED',
                    exit_price  = %s,
                    exit_reason = %s,
                    pnl         = %s,
                    closed_at   = NOW()
                WHERE id = %s
            """, (exitp, exit_reason, round(pnl, 2), position_id))
        return True
    except Exception as e:
        print(f"[DB] close_paper_position error: {e}")
        return False


def get_open_paper_positions(symbol: str = None) -> pd.DataFrame:
    """Return OPEN positions (optionally for one symbol), newest first."""
    try:
        with _get_cursor() as cur:
            if symbol:
                cur.execute("""
                    SELECT * FROM paper_positions
                    WHERE status = 'OPEN' AND symbol = %s
                    ORDER BY opened_at DESC
                """, (symbol,))
            else:
                cur.execute("""
                    SELECT * FROM paper_positions
                    WHERE status = 'OPEN'
                    ORDER BY opened_at DESC
                """)
            rows = cur.fetchall()
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame([dict(r) for r in rows])
    except Exception as e:
        print(f"[DB] get_open_paper_positions error: {e}")
        return pd.DataFrame()


def get_closed_paper_positions(days: int = 30) -> pd.DataFrame:
    """Return CLOSED positions from the last N days, newest first."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        with _get_cursor() as cur:
            cur.execute("""
                SELECT * FROM paper_positions
                WHERE status = 'CLOSED' AND closed_at >= %s
                ORDER BY closed_at DESC
            """, (cutoff,))
            rows = cur.fetchall()
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame([dict(r) for r in rows])
    except Exception as e:
        print(f"[DB] get_closed_paper_positions error: {e}")
        return pd.DataFrame()


def count_open_paper_positions() -> int:
    """How many positions are currently OPEN (for the max-concurrent cap)."""
    try:
        with _get_cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM paper_positions WHERE status = 'OPEN'")
            row = cur.fetchone()
        return int(row["n"]) if row else 0
    except Exception as e:
        print(f"[DB] count_open_paper_positions error: {e}")
        return 0


def is_paper_position_open(symbol: str) -> bool:
    """True if there is an OPEN position for this symbol (idempotency check)."""
    try:
        with _get_cursor() as cur:
            cur.execute("""
                SELECT 1 FROM paper_positions
                WHERE status = 'OPEN' AND symbol = %s
                LIMIT 1
            """, (symbol,))
            return cur.fetchone() is not None
    except Exception as e:
        print(f"[DB] is_paper_position_open error: {e}")
        return False


def get_paper_pnl_summary(days: int = 30) -> dict:
    """
    Scorecard: totals over CLOSED positions in the last N days.
    Returns dict with total_pnl, trades, wins, losses, win_rate, open_count.
    """
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        with _get_cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*)                                   AS trades,
                    COALESCE(SUM(pnl), 0)                       AS total_pnl,
                    COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) AS wins,
                    COALESCE(SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END), 0) AS losses
                FROM paper_positions
                WHERE status = 'CLOSED' AND closed_at >= %s
            """, (cutoff,))
            row = cur.fetchone() or {}
            cur.execute("SELECT COUNT(*) AS n FROM paper_positions WHERE status = 'OPEN'")
            open_row = cur.fetchone() or {"n": 0}

        trades = int(row.get("trades", 0) or 0)
        wins   = int(row.get("wins", 0) or 0)
        losses = int(row.get("losses", 0) or 0)
        win_rate = round((wins / trades * 100), 1) if trades else 0.0

        return {
            "total_pnl":  round(float(row.get("total_pnl", 0) or 0), 2),
            "trades":     trades,
            "wins":       wins,
            "losses":     losses,
            "win_rate":   win_rate,
            "open_count": int(open_row.get("n", 0) or 0),
        }
    except Exception as e:
        print(f"[DB] get_paper_pnl_summary error: {e}")
        return {"total_pnl": 0.0, "trades": 0, "wins": 0, "losses": 0,
                "win_rate": 0.0, "open_count": 0}


# ============================================================
# PAPER TRADING — SYMMETRIC BUY/SELL + MANUAL CONTROLS
# (added for: symmetric long/short, capital visibility, manual
#  close/stop-edit buttons — Jwala Jul 8 call)
# ============================================================

def get_open_position(symbol: str) -> dict | None:
    """
    Return the OPEN position row for a symbol (full dict, includes
    side), or None if nothing is open for it.

    Used by PaperTrader.on_signal() to decide what an incoming signal
    should do: if a position is open in the OPPOSITE direction, close
    it (reversal exit); if nothing is open, open a new one in the
    signal's direction; if a position is already open in the SAME
    direction, skip (no duplicate entry).
    """
    try:
        with _get_cursor() as cur:
            cur.execute("""
                SELECT * FROM paper_positions
                WHERE status = 'OPEN' AND symbol = %s
                ORDER BY opened_at DESC
                LIMIT 1
            """, (symbol,))
            row = cur.fetchone()
        return dict(row) if row else None
    except Exception as e:
        print(f"[DB] get_open_position error: {e}")
        return None


def update_paper_position_stop(position_id: int, new_stop: float) -> bool:
    """
    Manually move the stop-loss of an OPEN position (Jwala's
    breakeven/trailing-stop button on the dashboard — a manual
    override, not automated trailing logic). Returns True on success,
    False if the position isn't open (or on error).
    """
    try:
        with _get_cursor() as cur:
            cur.execute("""
                UPDATE paper_positions
                SET stop_loss = %s
                WHERE id = %s AND status = 'OPEN'
            """, (round(float(new_stop), 2), position_id))
            return cur.rowcount > 0
    except Exception as e:
        print(f"[DB] update_paper_position_stop error: {e}")
        return False


def get_capital_deployed() -> float:
    """
    Sum of (entry_price * quantity) across all OPEN positions — how
    much of total capital is currently tied up. Computed live from
    existing columns; no new column or migration needed.
    """
    try:
        with _get_cursor() as cur:
            cur.execute("""
                SELECT COALESCE(SUM(entry_price * quantity), 0) AS deployed
                FROM paper_positions
                WHERE status = 'OPEN'
            """)
            row = cur.fetchone()
        return round(float(row["deployed"]), 2) if row else 0.0
    except Exception as e:
        print(f"[DB] get_capital_deployed error: {e}")
        return 0.0