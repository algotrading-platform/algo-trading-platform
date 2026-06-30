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