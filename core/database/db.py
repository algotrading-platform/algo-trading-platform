# ============================================================
# core/database/db.py
#
# Central database layer using Azure SQL.
# Translated from the Azure PostgreSQL version — all function
# signatures and return types are identical, so no other file
# needs to change.
#
# Environment variables:
#   AZURE_SQL_CONNECTION_STRING : full ODBC connection string (optional
#                                  override, use if you need extra ODBC
#                                  keywords)
#
# Fallback (if AZURE_SQL_CONNECTION_STRING not set), built from parts:
#   AZURE_DB_HOST     : algo-sql2-rjw4desia2hqk.database.windows.net
#   AZURE_DB_PORT     : 1433
#   AZURE_DB_NAME     : algodb
#   AZURE_DB_USER     : algoadmin
#   AZURE_DB_PASSWORD : (see .env / Key Vault)
#   AZURE_SQL_DRIVER  : {ODBC Driver 17 for SQL Server}  (default)
#
# NOTE ON DEFAULTS: the host/db defaults above match what's actually
# deployed (see infra/DEPLOYED_RESOURCES.md) — but they're defaults,
# not a substitute for setting the real values in .env. Confirm
# against your actual .env before relying on them.
# ============================================================

import logging
import os
import queue
import struct
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pandas as pd
import pyodbc
import pytz
from dotenv import load_dotenv

log = logging.getLogger("db")
IST = pytz.timezone("Asia/Kolkata")

load_dotenv()

# ============================================================
# DATETIMEOFFSET OUTPUT CONVERTER
# ============================================================
#
# pyodbc does not decode SQL Server's DATETIMEOFFSET type by default —
# without this converter registered, every timestamp column in this
# file (all of them, since the schema uses DATETIMEOFFSET throughout)
# would come back as raw bytes instead of a Python datetime, silently
# breaking every .tzinfo / .astimezone() call in this module.
#
# SQL_SS_TIMESTAMPOFFSET = -155. Byte layout per the ODBC driver's
# SQL_SS_TIMESTAMPOFFSET_STRUCT: this is a widely-used community
# workaround (pyodbc does not support this type natively as of
# writing) — flagging that this is the one piece of this file most
# worth testing directly against a real query before trusting it.

def _handle_datetimeoffset(dto_value: bytes) -> datetime:
    tup = struct.unpack("<6hI2h", dto_value)
    return datetime(
        tup[0], tup[1], tup[2], tup[3], tup[4], tup[5], tup[6] // 1000,
        timezone(timedelta(hours=tup[7], minutes=tup[8])),
    )


# ============================================================
# CONNECTION
# ============================================================
#
# Same reasoning as the Postgres version: a pooled connection is reused
# across calls instead of opening a new TCP+TLS connection to Azure SQL
# on every single query, for the same reason (500+ instruments x
# multiple strategies/timeframes per scan cycle).
#
# pyodbc has no built-in pool comparable to psycopg2.pool — this is a
# minimal thread-safe pool mirroring that pool's getconn()/putconn()
# interface, so the rest of this file needed almost no structural change.

_pool      = None
_pool_lock = threading.Lock()


def _build_connection_string() -> str:
    """
    Builds a pyodbc connection string for Azure SQL.
    Prefers AZURE_SQL_CONNECTION_STRING, falls back to building one
    from individual AZURE_DB_* vars — same fallback pattern the
    Postgres version used with DATABASE_URL.
    """
    full = os.getenv("AZURE_SQL_CONNECTION_STRING", "")
    if full:
        return full

    driver   = os.getenv("AZURE_SQL_DRIVER",  "{ODBC Driver 17 for SQL Server}")
    host     = os.getenv("AZURE_DB_HOST",     "algo-sql2-rjw4desia2hqk.database.windows.net")
    port     = os.getenv("AZURE_DB_PORT",     "1433")
    dbname   = os.getenv("AZURE_DB_NAME",     "algodb")
    user     = os.getenv("AZURE_DB_USER",     "algoadmin")
    password = os.getenv("AZURE_DB_PASSWORD", "")

    return (
        f"DRIVER={driver};"
        f"SERVER=tcp:{host},{port};"
        f"DATABASE={dbname};"
        f"UID={user};"
        f"PWD={password};"
        f"Encrypt=yes;TrustServerCertificate=no;Connection Timeout=15;"
        # MARS lets one physical connection hold more than one open result
        # set at a time. Off by default for this driver, which is the
        # standard cause of "Connection is busy with results for another
        # command" the moment any code path issues a second command on a
        # connection before fully consuming/closing the first (seen in
        # production Aug 12 2026). Each _get_cursor() call already gets its
        # own connection from the pool, so this shouldn't be strictly
        # necessary — but it's a one-line change that makes that whole
        # error class structurally impossible instead of relying on every
        # call site getting cursor lifecycle exactly right.
        # NOTE: "MultipleActiveResultSets=True" (first attempt) is the
        # ADO.NET/SqlClient keyword and is silently ignored by the ODBC
        # driver pyodbc talks to — confirmed by the error still appearing
        # after deploying it. The actual ODBC keyword is MARS_Connection.
        "MARS_Connection=yes;"
    )


class _PyodbcPool:
    """
    Minimal thread-safe connection pool for pyodbc. Mirrors the
    getconn() / putconn(conn, close=bool) interface the original
    psycopg2.pool.ThreadedConnectionPool exposed.
    """

    def __init__(self, minconn: int, maxconn: int, conn_str: str):
        self._conn_str = conn_str
        self._maxconn  = maxconn
        self._pool     = queue.LifoQueue(maxsize=maxconn)
        self._created  = 0
        # RLock, not Lock: getconn() acquires this and calls _new_conn()
        # *while still holding it*, and _new_conn() acquires the same lock
        # again to bump _created. With a plain Lock that's a guaranteed
        # self-deadlock — silent, no exception — for any thread that needs
        # to grow the pool under concurrent load (i.e. exactly a busy scan
        # cycle). Found Aug 12 2026 while chasing why some fraction of
        # in-flight instrument scans never completed even accounting for
        # the 300s deadline, and intermittent "connection busy" /
        # transient token-lookup failures under load.
        self._lock     = threading.RLock()
        for _ in range(minconn):
            self._pool.put(self._new_conn())

    def _new_conn(self):
        conn = pyodbc.connect(self._conn_str, autocommit=False)
        conn.add_output_converter(-155, _handle_datetimeoffset)  # SQL_SS_TIMESTAMPOFFSET
        with self._lock:
            self._created += 1
        return conn

    def getconn(self):
        try:
            return self._pool.get_nowait()
        except queue.Empty:
            with self._lock:
                if self._created < self._maxconn:
                    return self._new_conn()
            return self._pool.get(timeout=30)  # pool exhausted -- wait for one to free up

    def putconn(self, conn, close: bool = False):
        if close:
            try:
                conn.close()
            except Exception:
                pass
            with self._lock:
                self._created -= 1
            return
        try:
            self._pool.put_nowait(conn)
        except queue.Full:
            conn.close()
            with self._lock:
                self._created -= 1


def _get_pool() -> _PyodbcPool:
    """
    Lazily creates the process-wide connection pool on first use.
    Each process (dashboard, scheduler, ws listener) gets its own pool,
    sized above the scan engine's max_workers=10 thread pool so a full
    scan cycle never blocks waiting for a free connection.
    """
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                conn_str = _build_connection_string()
                maxconn  = int(os.getenv("DB_POOL_MAX", "20"))
                _pool = _PyodbcPool(1, maxconn, conn_str)
    return _pool


class _DictCursor:
    """
    Wraps a raw pyodbc cursor to behave like psycopg2.extras.RealDictCursor:
    .fetchone() / .fetchall() return dict(s) keyed by column name instead
    of pyodbc's positional Row objects, matching every `row["col"]` /
    `row.get("col")` call site elsewhere in this file.
    """

    def __init__(self, raw_cursor):
        self._cur = raw_cursor

    def execute(self, query: str, params=None):
        if params is None:
            return self._cur.execute(query)
        return self._cur.execute(query, params)

    def executemany(self, query: str, seq_of_params):
        return self._cur.executemany(query, seq_of_params)

    def _row_to_dict(self, row):
        if row is None:
            return None
        cols = [c[0] for c in self._cur.description]
        return dict(zip(cols, row))

    def fetchone(self):
        return self._row_to_dict(self._cur.fetchone())

    def fetchall(self):
        return [self._row_to_dict(r) for r in self._cur.fetchall()]

    @property
    def rowcount(self):
        return self._cur.rowcount


@contextmanager
def _get_cursor():
    """
    Context manager that yields a dict-row cursor from the pool.
    Commits on success, rolls back on error, always returns the
    connection to the pool (discarding it instead of returning it if
    the connection itself is bad, so the pool self-heals).
    """
    pool = _get_pool()
    conn = pool.getconn()
    bad_conn = False
    try:
        cur = _DictCursor(conn.cursor())
        yield cur
        conn.commit()
    except (pyodbc.OperationalError, pyodbc.InterfaceError):
        bad_conn = True
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn, close=bad_conn)


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
                    ([timestamp], stock, timeframe, signal, rsi, price, strategy)
                VALUES
                    (SYSDATETIMEOFFSET(), ?, ?, ?, ?, ?, ?)
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
        # Signals are stored via SYSDATETIMEOFFSET() (UTC-equivalent,
        # datetimeoffset). The cutoff MUST be timezone-aware UTC too --
        # a naive datetime.now() is local IST and would shift the window
        # ~5.5h, silently hiding the most recent rows (this was the
        # "dashboard shows no records" bug in the Postgres version).
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        conditions = ["[timestamp] >= ?"]
        params     = [cutoff]

        if timeframe:
            conditions.append("timeframe = ?")
            params.append(timeframe)
        if strategy:
            conditions.append("strategy = ?")
            params.append(strategy)

        where = " AND ".join(conditions)

        with _get_cursor() as cur:
            cur.execute(
                f"SELECT * FROM signals WHERE {where} ORDER BY [timestamp] DESC",
                params,
            )
            rows = cur.fetchall()

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)

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
                    SELECT TOP 1 signal FROM signals
                    WHERE stock = ? AND timeframe = ? AND strategy = ?
                    ORDER BY [timestamp] DESC
                """, (stock, timeframe, strategy))
            else:
                cur.execute("""
                    SELECT TOP 1 signal FROM signals
                    WHERE stock = ? AND timeframe = ?
                    ORDER BY [timestamp] DESC
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
                SELECT TOP 1 [timestamp] FROM signals
                ORDER BY [timestamp] DESC
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
                    SELECT TOP 1 signal FROM alert_states
                    WHERE stock = ? AND timeframe = ? AND strategy = ?
                """, (stock, timeframe, strategy))
            else:
                cur.execute("""
                    SELECT TOP 1 signal FROM alert_states
                    WHERE stock = ? AND timeframe = ?
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

    Postgres's ON CONFLICT ... DO UPDATE has no T-SQL equivalent --
    this is rewritten as a MERGE statement, matched against the same
    (stock, timeframe, strategy) unique constraint the table already has.

    Falls back to a (stock, timeframe)-only match if the strategy-aware
    MERGE fails for any reason, mirroring the original's fallback intent
    -- though note the deployed schema only has the 3-column unique
    constraint, so this fallback path is a legacy safety net rather than
    something expected to trigger in normal operation.
    """
    try:
        with _get_cursor() as cur:
            cur.execute("""
                MERGE INTO alert_states AS target
                USING (SELECT ? AS stock, ? AS timeframe, ? AS strategy, ? AS signal) AS source
                ON (target.stock = source.stock
                    AND target.timeframe = source.timeframe
                    AND target.strategy = source.strategy)
                WHEN MATCHED THEN
                    UPDATE SET signal = source.signal, updated_at = SYSDATETIMEOFFSET()
                WHEN NOT MATCHED THEN
                    INSERT (stock, timeframe, strategy, signal, updated_at)
                    VALUES (source.stock, source.timeframe, source.strategy, source.signal, SYSDATETIMEOFFSET());
            """, (stock, timeframe, strategy, signal))
    except Exception as e:
        print(f"[DB] upsert_alert_state (strategy-aware) failed, "
              f"falling back to legacy: {e}")
        try:
            with _get_cursor() as cur:
                cur.execute("""
                    MERGE INTO alert_states AS target
                    USING (SELECT ? AS stock, ? AS timeframe, ? AS signal) AS source
                    ON (target.stock = source.stock AND target.timeframe = source.timeframe)
                    WHEN MATCHED THEN
                        UPDATE SET signal = source.signal, updated_at = SYSDATETIMEOFFSET()
                    WHEN NOT MATCHED THEN
                        INSERT (stock, timeframe, signal, updated_at)
                        VALUES (source.stock, source.timeframe, source.signal, SYSDATETIMEOFFSET());
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
    """Insert or update backtest result for symbol+timeframe+strategy (MERGE)."""
    try:
        with _get_cursor() as cur:
            cur.execute("""
                MERGE INTO backtest_results AS target
                USING (SELECT ? AS symbol, ? AS name, ? AS timeframe, ? AS category, ? AS strategy,
                              ? AS trades, ? AS pnl, ? AS pnl_pct, ? AS win_rate, ? AS wins,
                              ? AS losses, ? AS period) AS source
                ON (target.symbol = source.symbol
                    AND target.timeframe = source.timeframe
                    AND target.strategy = source.strategy)
                WHEN MATCHED THEN
                    UPDATE SET
                        trades     = source.trades,
                        pnl        = source.pnl,
                        pnl_pct    = source.pnl_pct,
                        win_rate   = source.win_rate,
                        wins       = source.wins,
                        losses     = source.losses,
                        period     = source.period,
                        updated_at = SYSDATETIMEOFFSET()
                WHEN NOT MATCHED THEN
                    INSERT (symbol, name, timeframe, category, strategy,
                            trades, pnl, pnl_pct, win_rate, wins, losses, period, updated_at)
                    VALUES (source.symbol, source.name, source.timeframe, source.category, source.strategy,
                            source.trades, source.pnl, source.pnl_pct, source.win_rate, source.wins,
                            source.losses, source.period, SYSDATETIMEOFFSET());
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
            conditions.append("timeframe = ?")
            params.append(timeframe)
        if strategy:
            conditions.append("strategy = ?")
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

        df = pd.DataFrame(rows)
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
        now        = datetime.now(IST)
        expires_at = (now + timedelta(days=1)).replace(
            hour=3, minute=30, second=0, microsecond=0
        )
        with _get_cursor() as cur:
            cur.execute("""
                INSERT INTO upstox_tokens (access_token, created_at, expires_at)
                VALUES (?, SYSDATETIMEOFFSET(), ?)
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
        with _get_cursor() as cur:
            cur.execute("""
                SELECT TOP 1 access_token, expires_at
                FROM upstox_tokens
                ORDER BY created_at DESC
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
                SELECT TOP 1 value FROM app_config
                WHERE [key] = ?
            """, (key,))
            row = cur.fetchone()

        if row:
            return row["value"]
        return None

    except Exception as e:
        print(f"[DB] get_config error: {e}")
        return None


def set_config(key: str, value: str) -> bool:
    """Set a config value — upserts into app_config table (MERGE)."""
    try:
        with _get_cursor() as cur:
            cur.execute("""
                MERGE INTO app_config AS target
                USING (SELECT ? AS [key], ? AS value) AS source
                ON (target.[key] = source.[key])
                WHEN MATCHED THEN
                    UPDATE SET value = source.value, updated_at = SYSDATETIMEOFFSET()
                WHEN NOT MATCHED THEN
                    INSERT ([key], value, updated_at) VALUES (source.[key], source.value, SYSDATETIMEOFFSET());
            """, (key, value))
        return True
    except Exception as e:
        print(f"[DB] set_config error: {e}")
        return False


# ============================================================
# PAPER TRADING — SYMMETRIC BUY/SELL + MANUAL CONTROLS
# ============================================================

def open_paper_position_if_capacity(
    symbol:        str,
    side:          str,
    quantity:      int,
    entry_price:   float,
    stop_loss:     float,
    target:        float,
    strategy:      str,
    timeframe:     str,
    max_positions: int,
    risk_amount:   float = 0.0,
    order_id:      str   = "",
) -> dict:
    """
    Atomically re-check the per-strategy open-position cap and insert
    in the same transaction, so concurrent scan jobs can't each read a
    stale count and jointly overshoot the cap.

    Postgres's pg_advisory_xact_lock(hashtext(...)) is replaced with
    T-SQL's sp_getapplock, using @LockOwner='Transaction' so it
    auto-releases on commit/rollback just like the Postgres version --
    no explicit unlock needed, and no hashing required since
    sp_getapplock takes the resource name directly as a string.

    Returns {"opened": True} on success, or {"opened": False, "reason": ...}
    if the cap was already full (checked fresh, under the lock).
    """
    try:
        initial_stop_distance = round(abs(float(entry_price) - float(stop_loss)), 2)
        with _get_cursor() as cur:
            cur.execute(
                "EXEC sp_getapplock @Resource = ?, @LockMode = 'Exclusive', @LockOwner = 'Transaction'",
                (f"paper_pos_cap:{strategy}",)
            )
            cur.execute("""
                SELECT COUNT(*) AS n FROM paper_positions
                WHERE status = 'OPEN' AND strategy = ?
            """, (strategy,))
            if int(cur.fetchone()["n"]) >= max_positions:
                return {"opened": False, "cause": "cap",
                        "reason": f"max {max_positions} open positions for {strategy}"}

            cur.execute("""
                INSERT INTO paper_positions
                    (symbol, side, quantity, entry_price, stop_loss, target,
                     strategy, timeframe, risk_amount, order_id,
                     peak_price, initial_stop_distance,
                     status, opened_at)
                VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', SYSDATETIMEOFFSET())
            """, (
                symbol, side, int(quantity),
                round(float(entry_price), 2),
                round(float(stop_loss),   2),
                round(float(target),      2),
                strategy, timeframe,
                round(float(risk_amount), 2),
                order_id,
                round(float(entry_price), 2),  # peak_price starts at entry
                initial_stop_distance,
            ))
        return {"opened": True}
    except Exception as e:
        print(f"[DB] open_paper_position_if_capacity error: {e}")
        return {"opened": False, "cause": "error", "reason": f"db error: {e}"}


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
    """
    Insert a new OPEN paper position. Returns True on success.

    peak_price starts at entry_price and initial_stop_distance is
    snapshotted at open time -- both feed the Volume Spike trailing
    stop in paper_trader.py.
    """
    try:
        initial_stop_distance = round(abs(float(entry_price) - float(stop_loss)), 2)
        with _get_cursor() as cur:
            cur.execute("""
                INSERT INTO paper_positions
                    (symbol, side, quantity, entry_price, stop_loss, target,
                     strategy, timeframe, risk_amount, order_id,
                     peak_price, initial_stop_distance,
                     status, opened_at)
                VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', SYSDATETIMEOFFSET())
            """, (
                symbol, side, int(quantity),
                round(float(entry_price), 2),
                round(float(stop_loss),   2),
                round(float(target),      2),
                strategy, timeframe,
                round(float(risk_amount), 2),
                order_id,
                round(float(entry_price), 2),  # peak_price starts at entry
                initial_stop_distance,
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
    pnl = (exit-entry)*qty for BUY, (entry-exit)*qty for SELL -- GROSS
    P&L. Also computes charges and net_pnl = pnl - charges.
    """
    try:
        from core.execution.charges import estimate_charges_for_trade

        with _get_cursor() as cur:
            cur.execute("""
                SELECT TOP 1 side, quantity, entry_price
                FROM paper_positions
                WHERE id = ? AND status = 'OPEN'
            """, (position_id,))
            row = cur.fetchone()
            if not row:
                return False

            qty   = int(row["quantity"])
            entry = float(row["entry_price"])
            side  = row["side"]
            exitp = round(float(exit_price), 2)

            if side == "BUY":
                pnl = (exitp - entry) * qty
            else:  # SELL (short)
                pnl = (entry - exitp) * qty

            charges = estimate_charges_for_trade(side, entry, exitp, qty)
            net_pnl = round(pnl - charges, 2)

            cur.execute("""
                UPDATE paper_positions
                SET status      = 'CLOSED',
                    exit_price  = ?,
                    exit_reason = ?,
                    pnl         = ?,
                    charges     = ?,
                    net_pnl     = ?,
                    closed_at   = SYSDATETIMEOFFSET()
                WHERE id = ?
            """, (exitp, exit_reason, round(pnl, 2), charges, net_pnl, position_id))
        return True
    except Exception as e:
        print(f"[DB] close_paper_position error: {e}")
        return False


def get_open_position(symbol: str) -> dict | None:
    """
    Return the OPEN position row for a symbol (full dict, includes
    side), or None if nothing is open for it.
    """
    try:
        with _get_cursor() as cur:
            cur.execute("""
                SELECT TOP 1 * FROM paper_positions
                WHERE status = 'OPEN' AND symbol = ?
                ORDER BY opened_at DESC
            """, (symbol,))
            row = cur.fetchone()
        return row if row else None
    except Exception as e:
        print(f"[DB] get_open_position error: {e}")
        return None


def update_trailing_state(position_id: int, peak_price: float, new_stop: float = None) -> bool:
    """
    Persists the Volume Spike trailing-stop state each monitor cycle.
    Always updates peak_price; only touches stop_loss if new_stop is given.
    """
    try:
        with _get_cursor() as cur:
            if new_stop is not None:
                cur.execute("""
                    UPDATE paper_positions
                    SET peak_price = ?, stop_loss = ?
                    WHERE id = ? AND status = 'OPEN'
                """, (round(float(peak_price), 2), round(float(new_stop), 2), position_id))
            else:
                cur.execute("""
                    UPDATE paper_positions
                    SET peak_price = ?
                    WHERE id = ? AND status = 'OPEN'
                """, (round(float(peak_price), 2), position_id))
            return cur.rowcount > 0
    except Exception as e:
        print(f"[DB] update_trailing_state error: {e}")
        return False


def update_paper_position_stop(position_id: int, new_stop: float) -> bool:
    """Manually move the stop-loss of an OPEN position."""
    try:
        with _get_cursor() as cur:
            cur.execute("""
                UPDATE paper_positions
                SET stop_loss = ?
                WHERE id = ? AND status = 'OPEN'
            """, (round(float(new_stop), 2), position_id))
            return cur.rowcount > 0
    except Exception as e:
        print(f"[DB] update_paper_position_stop error: {e}")
        return False


def get_capital_deployed(strategy: str = None) -> float:
    """Sum of (entry_price * quantity) across OPEN positions."""
    try:
        with _get_cursor() as cur:
            if strategy:
                cur.execute("""
                    SELECT COALESCE(SUM(entry_price * quantity), 0) AS deployed
                    FROM paper_positions
                    WHERE status = 'OPEN' AND strategy = ?
                """, (strategy,))
            else:
                cur.execute("""
                    SELECT COALESCE(SUM(entry_price * quantity), 0) AS deployed
                    FROM paper_positions
                    WHERE status = 'OPEN'
                """)
            row = cur.fetchone()
        return round(float(row["deployed"]), 2) if row else 0.0
    except Exception as e:
        print(f"[DB] get_capital_deployed error: {e}")


def get_open_paper_positions(symbol: str = None) -> pd.DataFrame:
    """Return OPEN positions (optionally for one symbol), newest first."""
    try:
        with _get_cursor() as cur:
            if symbol:
                cur.execute("""
                    SELECT * FROM paper_positions
                    WHERE status = 'OPEN' AND symbol = ?
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
        return pd.DataFrame(rows)
    except Exception as e:
        log.error(f"get_open_paper_positions error: {e}")
        return pd.DataFrame()


def get_closed_paper_positions(days: int = 30) -> pd.DataFrame:
    """Return CLOSED positions from the last N days, newest first."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        with _get_cursor() as cur:
            cur.execute("""
                SELECT * FROM paper_positions
                WHERE status = 'CLOSED' AND closed_at >= ?
                ORDER BY closed_at DESC
            """, (cutoff,))
            rows = cur.fetchall()
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows)
    except Exception as e:
        print(f"[DB] get_closed_paper_positions error: {e}")
        return pd.DataFrame()


def _ist_today_bounds_utc() -> tuple:
    """
    (start, end) of the CURRENT IST calendar day, as UTC datetimes --
    for querying datetimeoffset columns against a trading-day boundary.
    """
    now_ist   = datetime.now(IST)
    start_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    end_ist   = start_ist + timedelta(days=1)
    return start_ist.astimezone(timezone.utc), end_ist.astimezone(timezone.utc)


def get_today_closed_paper_positions() -> pd.DataFrame:
    """CLOSED positions from TODAY (IST calendar day) only."""
    try:
        start_utc, end_utc = _ist_today_bounds_utc()
        with _get_cursor() as cur:
            cur.execute("""
                SELECT * FROM paper_positions
                WHERE status = 'CLOSED' AND closed_at >= ? AND closed_at < ?
                ORDER BY closed_at DESC
            """, (start_utc, end_utc))
            rows = cur.fetchall()
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows)
    except Exception as e:
        print(f"[DB] get_today_closed_paper_positions error: {e}")
        return pd.DataFrame()


def count_open_paper_positions(strategy: str = None) -> int:
    """How many positions are currently OPEN (for the max-concurrent cap)."""
    try:
        with _get_cursor() as cur:
            if strategy:
                cur.execute("""
                    SELECT COUNT(*) AS n FROM paper_positions
                    WHERE status = 'OPEN' AND strategy = ?
                """, (strategy,))
            else:
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
                SELECT TOP 1 1 AS x FROM paper_positions
                WHERE status = 'OPEN' AND symbol = ?
            """, (symbol,))
            return cur.fetchone() is not None
    except Exception as e:
        print(f"[DB] is_paper_position_open error: {e}")
        return False


def get_paper_pnl_summary(days: int = 30) -> dict:
    """Scorecard: totals over CLOSED positions in the last N days."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        with _get_cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*)                                   AS trades,
                    COALESCE(SUM(pnl), 0)                       AS total_pnl,
                    COALESCE(SUM(net_pnl), 0)                   AS total_net_pnl,
                    COALESCE(SUM(charges), 0)                   AS total_charges,
                    COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) AS wins,
                    COALESCE(SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END), 0) AS losses
                FROM paper_positions
                WHERE status = 'CLOSED' AND closed_at >= ?
            """, (cutoff,))
            row = cur.fetchone() or {}
            cur.execute("SELECT COUNT(*) AS n FROM paper_positions WHERE status = 'OPEN'")
            open_row = cur.fetchone() or {"n": 0}

        trades = int(row.get("trades", 0) or 0)
        wins   = int(row.get("wins", 0) or 0)
        losses = int(row.get("losses", 0) or 0)
        win_rate = round((wins / trades * 100), 1) if trades else 0.0

        return {
            "total_pnl":     round(float(row.get("total_pnl", 0) or 0), 2),
            "total_net_pnl": round(float(row.get("total_net_pnl", 0) or 0), 2),
            "total_charges": round(float(row.get("total_charges", 0) or 0), 2),
            "trades":     trades,
            "wins":       wins,
            "losses":     losses,
            "win_rate":   win_rate,
            "open_count": int(open_row.get("n", 0) or 0),
        }
    except Exception as e:
        print(f"[DB] get_paper_pnl_summary error: {e}")
        return {"total_pnl": 0.0, "total_net_pnl": 0.0, "total_charges": 0.0,
                "trades": 0, "wins": 0, "losses": 0,
                "win_rate": 0.0, "open_count": 0}


def get_today_pnl_summary() -> dict:
    """Scorecard: totals over CLOSED positions from TODAY (IST calendar day) only."""
    try:
        start_utc, end_utc = _ist_today_bounds_utc()
        with _get_cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*)                                   AS trades,
                    COALESCE(SUM(pnl), 0)                       AS total_pnl,
                    COALESCE(SUM(net_pnl), 0)                   AS total_net_pnl,
                    COALESCE(SUM(charges), 0)                   AS total_charges,
                    COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) AS wins,
                    COALESCE(SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END), 0) AS losses
                FROM paper_positions
                WHERE status = 'CLOSED' AND closed_at >= ? AND closed_at < ?
            """, (start_utc, end_utc))
            row = cur.fetchone() or {}
            cur.execute("SELECT COUNT(*) AS n FROM paper_positions WHERE status = 'OPEN'")
            open_row = cur.fetchone() or {"n": 0}

        trades = int(row.get("trades", 0) or 0)
        wins   = int(row.get("wins", 0) or 0)
        losses = int(row.get("losses", 0) or 0)
        win_rate = round((wins / trades * 100), 1) if trades else 0.0

        return {
            "total_pnl":     round(float(row.get("total_pnl", 0) or 0), 2),
            "total_net_pnl": round(float(row.get("total_net_pnl", 0) or 0), 2),
            "total_charges": round(float(row.get("total_charges", 0) or 0), 2),
            "trades":     trades,
            "wins":       wins,
            "losses":     losses,
            "win_rate":   win_rate,
            "open_count": int(open_row.get("n", 0) or 0),
        }
    except Exception as e:
        print(f"[DB] get_today_pnl_summary error: {e}")
        return {"total_pnl": 0.0, "total_net_pnl": 0.0, "total_charges": 0.0,
                "trades": 0, "wins": 0, "losses": 0,
                "win_rate": 0.0, "open_count": 0}


# ============================================================
# LIVE CANDLES (Upstox WebSocket listener — core/marketdata/ws_listener.py)
# ============================================================

def upsert_live_candles(rows: list[dict]) -> bool:
    """
    Batched upsert of 1-minute candles from the WS listener.

    SQL Server hard-caps queries at ~2100 parameters total. At 8
    params/row, this chunks into batches of 200 rows (1600 params)
    per MERGE, all within the same transaction -- a 502-row single
    batch previously overflowed the limit, surfacing as ODBC's
    generic 'COUNT field incorrect or syntax error' rather than a
    clear parameter-count message.

    Each row: {instrument_key, symbol, ts, open, high, low, close, volume}
    """
    if not rows:
        return True
    BATCH_SIZE = 200
    try:
        with _get_cursor() as cur:
            for i in range(0, len(rows), BATCH_SIZE):
                chunk = rows[i:i + BATCH_SIZE]
                values_sql = ", ".join(["(?,?,?,?,?,?,?,?)"] * len(chunk))
                params = []
                for r in chunk:
                    params.extend([
                        r["instrument_key"], r["symbol"], r["ts"],
                        round(float(r["open"]), 2), round(float(r["high"]), 2),
                        round(float(r["low"]), 2), round(float(r["close"]), 2),
                        int(r.get("volume", 0)),
                    ])
                cur.execute(f"""
                    MERGE INTO live_candles_1min AS target
                    USING (VALUES {values_sql})
                        AS source (instrument_key, symbol, ts, [open], high, low, [close], volume)
                    ON (target.instrument_key = source.instrument_key AND target.ts = source.ts)
                    WHEN MATCHED THEN
                        UPDATE SET
                            high       = source.high,
                            low        = source.low,
                            [close]    = source.[close],
                            volume     = source.volume,
                            updated_at = SYSDATETIMEOFFSET()
                    WHEN NOT MATCHED THEN
                        INSERT (instrument_key, symbol, ts, [open], high, low, [close], volume)
                        VALUES (source.instrument_key, source.symbol, source.ts,
                                source.[open], source.high, source.low, source.[close], source.volume);
                """, params)
        return True
    except Exception as e:
        print(f"[DB] upsert_live_candles error ({len(rows)} rows): {e}")
        return False


def get_live_candles_today(symbol: str) -> pd.DataFrame:
    """
    Today's 1-minute candles for a symbol, oldest first (for resampling).

    Uses the same IST-calendar-day boundary helper as the paper-trading
    functions (_ist_today_bounds_utc), computed in Python, rather than
    Postgres's date_trunc('day', NOW()) -- T-SQL has no direct
    equivalent, and computing the boundary in IST explicitly avoids a
    UTC-midnight-vs-IST-midnight mismatch that a naive CAST(... AS DATE)
    could introduce.
    """
    try:
        start_utc, _ = _ist_today_bounds_utc()
        with _get_cursor() as cur:
            cur.execute("""
                SELECT ts AS [Datetime], [open] AS [Open], high AS [High],
                       low AS [Low], [close] AS [Close], volume AS [Volume]
                FROM live_candles_1min
                WHERE symbol = ? AND ts >= ?
                ORDER BY ts ASC
            """, (symbol, start_utc))
            rows = cur.fetchall()
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows)
    except Exception as e:
        print(f"[DB] get_live_candles_today error for {symbol}: {e}")
        return pd.DataFrame()


def get_latest_live_price(symbol: str, max_age_minutes: int = 2) -> float | None:
    """
    Latest close for a symbol from the live feed. Returns None (not a
    stale value) if the WS listener hasn't updated this symbol within
    max_age_minutes -- callers should fall back to REST/yfinance in
    that case.

    Postgres's `NOW() - INTERVAL '1 minute' * %s` is replaced with a
    Python-computed cutoff timestamp, passed as a parameter -- the same
    pattern get_signals() already uses elsewhere in this file, so this
    is consistent with the rest of the module rather than introducing
    a new style.
    """
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        with _get_cursor() as cur:
            cur.execute("""
                SELECT TOP 1 [close] FROM live_candles_1min
                WHERE symbol = ? AND updated_at >= ?
                ORDER BY ts DESC
            """, (symbol, cutoff))
            row = cur.fetchone()
        return round(float(row["close"]), 2) if row else None
    except Exception as e:
        print(f"[DB] get_latest_live_price error for {symbol}: {e}")
        return None


# ============================================================
# SCAN RUN-LOCK (prevents overlapping Container Apps Job executions)
# ============================================================
#
# Container Apps Jobs do NOT prevent a new cron-triggered execution
# from starting while a previous one is still running -- parallelism/
# replicaCompletionCount only limit replicas *within* one execution,
# not across separate scheduled triggers. Without this, an overlapping
# run means every timeframe gets scanned twice concurrently: duplicate
# signals, duplicate paper positions, races on alert_states.
#
# Implemented as a row in app_config rather than sp_getapplock, since
# sp_getapplock is tied to one connection/transaction -- this lock
# needs to stay held across the whole script's runtime, spanning many
# separate short-lived pooled connections.

def try_acquire_scan_lock(lock_name: str, stale_after_seconds: int = 900) -> bool:
    """
    Atomically acquire a named run-lock. A lock is free if its value
    is 'FREE', or if it's been 'RUNNING' longer than stale_after_seconds
    (so a crashed prior run that never released it doesn't permanently
    block every future scan). Returns True if acquired, False if
    another run currently holds it (or the check itself failed --
    fails closed: skip this cycle rather than risk a duplicate run).
    """
    key = f"SCAN_LOCK::{lock_name}"
    try:
        with _get_cursor() as cur:
            cur.execute("""
                MERGE INTO app_config AS target
                USING (SELECT ? AS [key]) AS source
                ON (target.[key] = source.[key])
                WHEN MATCHED AND (target.value = 'FREE'
                                   OR target.updated_at < DATEADD(second, ?, SYSDATETIMEOFFSET())) THEN
                    UPDATE SET value = 'RUNNING', updated_at = SYSDATETIMEOFFSET()
                WHEN NOT MATCHED THEN
                    INSERT ([key], value, updated_at) VALUES (source.[key], 'RUNNING', SYSDATETIMEOFFSET());
            """, (key, -stale_after_seconds))
            return cur.rowcount > 0
    except Exception as e:
        print(f"[DB] try_acquire_scan_lock error: {e}")
        return False


def release_scan_lock(lock_name: str) -> None:
    """Marks a scan lock as FREE again. Always call in a finally block."""
    key = f"SCAN_LOCK::{lock_name}"
    try:
        with _get_cursor() as cur:
            cur.execute("""
                UPDATE app_config SET value = 'FREE', updated_at = SYSDATETIMEOFFSET()
                WHERE [key] = ?
            """, (key,))
    except Exception as e:
        print(f"[DB] release_scan_lock error: {e}")