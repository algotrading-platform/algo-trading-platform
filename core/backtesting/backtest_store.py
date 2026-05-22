# ============================================================
# core/backtesting/backtest_store.py
#
# Reads and writes backtest results CSV.
# Written by scheduler, read by dashboard.
# ============================================================

import os
import pandas as pd
from datetime import datetime

_FILE = "data/backtest_results.csv"

_COLUMNS = [
    "Symbol", "Name", "Timeframe", "Category",
    "Trades", "PnL", "PnL %", "Win Rate %",
    "Wins", "Losses", "Period", "Updated",
]


def write_result(
    symbol: str,
    name: str,
    timeframe: str,
    category: str,
    summary: dict,
    period: str,
) -> None:
    """Write or update one backtest result row."""
    os.makedirs("data", exist_ok=True)

    df = _read()

    # Remove existing row for this symbol + timeframe
    mask = (df["Symbol"] == symbol) & (df["Timeframe"] == timeframe)
    df   = df[~mask]

    new_row = {
        "Symbol":    symbol,
        "Name":      name,
        "Timeframe": timeframe,
        "Category":  category,
        "Trades":    summary.get("trades",   0),
        "PnL":       summary.get("pnl",      0.0),
        "PnL %":     summary.get("pnl_pct",  0.0),
        "Win Rate %":summary.get("win_rate", 0.0),
        "Wins":      summary.get("wins",     0),
        "Losses":    summary.get("losses",   0),
        "Period":    period,
        "Updated":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    df = pd.concat(
        [df, pd.DataFrame([new_row])],
        ignore_index=True,
    )

    df.to_csv(_FILE, index=False)


def get_results(timeframe: str = None) -> pd.DataFrame:
    """
    Return backtest results, optionally filtered by timeframe.
    Always returns a DataFrame (never raises).
    """
    df = _read()

    if df.empty:
        return df

    if timeframe:
        df = df[df["Timeframe"] == timeframe]

    return df


def _read() -> pd.DataFrame:
    if not os.path.exists(_FILE):
        return pd.DataFrame(columns=_COLUMNS)
    try:
        df = pd.read_csv(_FILE)
        for col in _COLUMNS:
            if col not in df.columns:
                df[col] = None
        return df
    except Exception:
        return pd.DataFrame(columns=_COLUMNS)
