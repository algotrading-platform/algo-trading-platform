# ============================================================
# core/database/db.py
#
# Central database layer using Supabase (PostgreSQL).
# All reads and writes go through this module.
#
# Tables managed here:
#   - signals          : every BUY/SELL signal logged
#   - alert_states     : last known signal per stock+timeframe
#   - backtest_results : backtest summary per symbol+timeframe
#
# Environment variables required:
#   SUPABASE_URL   : https://xxxx.supabase.co
#   SUPABASE_KEY   : anon public key from Supabase dashboard
# ============================================================

import os
from datetime import datetime, timedelta

import pandas as pd
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# CONNECTION
# ============================================================

def get_client() -> Client:
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_KEY", "")
    if not url or not key:
        raise EnvironmentError(
            "SUPABASE_URL and SUPABASE_KEY must be set in .env"
        )
    return create_client(url, key)


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
        client = get_client()
        client.table("signals").insert({
            "timestamp": datetime.now().isoformat(),
            "stock":     stock,
            "timeframe": timeframe,
            "signal":    signal,
            "rsi":       round(float(rsi), 2),
            "price":     round(float(price), 2),
            "strategy":  strategy,
        }).execute()
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
        client  = get_client()
        cutoff  = (datetime.now() - timedelta(days=days)).isoformat()
        query   = client.table("signals").select("*").gte("timestamp", cutoff)

        if timeframe:
            query = query.eq("timeframe", timeframe)
        if strategy:
            query = query.eq("strategy", strategy)

        result = query.order("timestamp", desc=True).execute()
        data   = result.data

        if not data:
            return pd.DataFrame()

        df = pd.DataFrame(data)

        # Rename to match existing dashboard column expectations
        df = df.rename(columns={
            "timestamp": "Timestamp",
            "stock":     "Stock",
            "timeframe": "Timeframe",
            "signal":    "Signal",
            "rsi":       "RSI",
            "price":     "Price",
            "strategy":  "Strategy",
        })

        df["Timestamp"] = pd.to_datetime(df["Timestamp"])
        return df

    except Exception as e:
        print(f"[DB] get_signals error: {e}")
        return pd.DataFrame()


def get_last_signal(stock: str, timeframe: str) -> str | None:
    """
    Returns the most recent signal for a stock+timeframe.
    Used for deduplication in SignalLogger.
    """
    try:
        client = get_client()
        result = (
            client.table("signals")
            .select("signal")
            .eq("stock", stock)
            .eq("timeframe", timeframe)
            .order("timestamp", desc=True)
            .limit(1)
            .execute()
        )
        if result.data:
            return result.data[0]["signal"]
        return None
    except Exception as e:
        print(f"[DB] get_last_signal error: {e}")
        return None


def get_last_scan_time() -> str | None:
    """
    Returns how long ago the last signal was written.
    Used for scheduler status in dashboard sidebar.
    """
    try:
        client = get_client()
        result = (
            client.table("signals")
            .select("timestamp")
            .order("timestamp", desc=True)
            .limit(1)
            .execute()
        )
        if not result.data:
            return None

        last_ts = datetime.fromisoformat(result.data[0]["timestamp"])
        import pytz
        IST  = pytz.timezone("Asia/Kolkata")
        now  = datetime.now(IST)
        if last_ts.tzinfo is None:
            last_ts = IST.localize(last_ts)
        diff = now - last_ts.astimezone(IST)
        mins = int(diff.total_seconds() / 60)

        if mins < 2:    return "just now"
        if mins < 60:   return f"{mins}m ago"
        hours = mins // 60
        return f"{hours}h {mins % 60}m ago"

    except Exception as e:
        print(f"[DB] get_last_scan_time error: {e}")
        return None


# ============================================================
# ALERT STATES TABLE
# ============================================================

def get_alert_state(stock: str, timeframe: str) -> str | None:
    """Returns last alerted signal for stock+timeframe."""
    try:
        client = get_client()
        result = (
            client.table("alert_states")
            .select("signal")
            .eq("stock", stock)
            .eq("timeframe", timeframe)
            .limit(1)
            .execute()
        )
        if result.data:
            return result.data[0]["signal"]
        return None
    except Exception as e:
        print(f"[DB] get_alert_state error: {e}")
        return None


def upsert_alert_state(stock: str, timeframe: str, signal: str) -> None:
    """Insert or update the alert state for stock+timeframe."""
    try:
        client = get_client()
        client.table("alert_states").upsert({
            "stock":     stock,
            "timeframe": timeframe,
            "signal":    signal,
            "updated_at": datetime.now().isoformat(),
        }, on_conflict="stock,timeframe").execute()
    except Exception as e:
        print(f"[DB] upsert_alert_state error: {e}")


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
        client = get_client()
        client.table("backtest_results").upsert({
            "symbol":     symbol,
            "name":       name,
            "timeframe":  timeframe,
            "category":   category,
            "strategy":   strategy,
            "trades":     trades,
            "pnl":        round(float(pnl), 2),
            "pnl_pct":    round(float(pnl_pct), 2),
            "win_rate":   round(float(win_rate), 1),
            "wins":       wins,
            "losses":     losses,
            "period":     period,
            "updated_at": datetime.now().isoformat(),
        }, on_conflict="symbol,timeframe,strategy").execute()
    except Exception as e:
        print(f"[DB] upsert_backtest error: {e}")


def get_backtest_results(
    timeframe: str = None,
    strategy:  str = None,
) -> pd.DataFrame:
    """Fetch backtest results, optionally filtered."""
    try:
        client = get_client()
        query  = client.table("backtest_results").select("*")

        if timeframe:
            query = query.eq("timeframe", timeframe)
        if strategy:
            query = query.eq("strategy", strategy)

        result = query.execute()
        data   = result.data

        if not data:
            return pd.DataFrame()

        df = pd.DataFrame(data)
        df = df.rename(columns={
            "symbol":   "Symbol",
            "name":     "Name",
            "timeframe":"Timeframe",
            "category": "Category",
            "strategy": "Strategy",
            "trades":   "Trades",
            "pnl":      "PnL",
            "pnl_pct":  "PnL %",
            "win_rate": "Win Rate %",
            "wins":     "Wins",
            "losses":   "Losses",
            "period":   "Period",
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
        IST = pytz.timezone("Asia/Kolkata")
        now = datetime.now(IST)
        expires_at = (now + timedelta(days=1)).replace(
            hour=3, minute=30, second=0, microsecond=0
        )
        client = get_client()
        client.table("upstox_tokens").insert({
            "access_token": access_token,
            "created_at":   now.isoformat(),
            "expires_at":   expires_at.isoformat(),
        }).execute()
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
        client = get_client()
        result = (
            client.table("upstox_tokens")
            .select("access_token, expires_at")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if not result.data:
            return None

        row = result.data[0]
        expires_at = datetime.fromisoformat(row["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = IST.localize(expires_at)

        if datetime.now(IST) >= expires_at:
            return None

        return row["access_token"]

    except Exception as e:
        print(f"[DB] get_upstox_token error: {e}")
        return None