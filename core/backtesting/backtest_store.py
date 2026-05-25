# ============================================================
# core/backtesting/backtest_store.py
#
# Reads and writes backtest results to Supabase.
# Written by scheduler after each scan.
# Read by dashboard for backtest summary cards.
# ============================================================

import pandas as pd
from core.database import db


def write_result(
    symbol:   str,
    name:     str,
    timeframe: str,
    category: str,
    summary:  dict,
    period:   str,
    strategy: str = "RSI Reversal",
) -> None:
    """Write or update one backtest result row in Supabase."""
    db.upsert_backtest(
        symbol=symbol,
        name=name,
        timeframe=timeframe,
        category=category,
        trades=summary.get("trades",   0),
        pnl=summary.get("pnl",         0.0),
        pnl_pct=summary.get("pnl_pct", 0.0),
        win_rate=summary.get("win_rate",0.0),
        wins=summary.get("wins",        0),
        losses=summary.get("losses",    0),
        period=period,
        strategy=strategy,
    )


def get_results(
    timeframe: str = None,
    strategy:  str = None,
) -> pd.DataFrame:
    """
    Return backtest results from Supabase.
    Optionally filtered by timeframe and strategy.
    Always returns a DataFrame (never raises).
    """
    return db.get_backtest_results(
        timeframe=timeframe,
        strategy=strategy,
    )