# ============================================================
# core/reporting/report_data.py
#
# Orchestrates DB fetch + pandas derivations for both report types.
# One fetch per underlying table drives every derived sheet (equity
# curve, symbol breakdown, day/hour patterns) -- trade volume is low
# enough (low-thousands/year even generously estimated, see the
# reporting design's scaling assessment) that extra SQL round-trips
# buy nothing over deriving everything in pandas from a single
# DataFrame already in memory.
# ============================================================

import pandas as pd
import pytz

from core.database.db import (
    get_paper_trades_for_report,
    get_paper_summary_by_strategy,
    get_signals_for_report,
    get_signal_summary_by_strategy,
)

IST = pytz.timezone("Asia/Kolkata")

_DAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _to_ist(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True).dt.tz_convert(IST)


def build_paper_trading_dataset(start_utc, end_utc, strategy=None, timeframe=None) -> dict:
    """Returns dict: trades, by_strategy, equity_curve, by_symbol, by_day, by_hour."""
    trades      = get_paper_trades_for_report(start_utc, end_utc, strategy, timeframe)
    by_strategy = get_paper_summary_by_strategy(start_utc, end_utc, strategy, timeframe)

    empty = pd.DataFrame()
    if trades.empty:
        return dict(trades=trades, by_strategy=by_strategy, equity_curve=empty,
                    by_symbol=empty, by_day=empty, by_hour=empty)

    t = trades.copy()
    t["closed_at_ist"] = _to_ist(t["closed_at"])
    t["opened_at_ist"] = _to_ist(t["opened_at"])
    t["net_pnl"] = t["net_pnl"].astype(float)
    t["pnl"]     = t["pnl"].astype(float)

    # Equity curve: daily net P&L + running cumulative, by close date.
    equity_curve = (
        t.groupby(t["closed_at_ist"].dt.date)["net_pnl"].sum()
        .reset_index().rename(columns={"closed_at_ist": "date", "net_pnl": "daily_net_pnl"})
        .sort_values("date")
    )
    equity_curve["cumulative_net_pnl"] = equity_curve["daily_net_pnl"].cumsum().round(2)
    equity_curve["daily_net_pnl"] = equity_curve["daily_net_pnl"].round(2)

    # Symbol breakdown.
    by_symbol = t.groupby("symbol").agg(
        trades=("id", "count"),
        wins=("pnl", lambda s: int((s > 0).sum())),
        gross_pnl=("pnl", "sum"),
        net_pnl=("net_pnl", "sum"),
        best_trade=("pnl", "max"),
        worst_trade=("pnl", "min"),
    ).reset_index()
    by_symbol["win_rate"] = (by_symbol["wins"] / by_symbol["trades"] * 100).round(1)
    by_symbol["avg_pnl"]  = (by_symbol["net_pnl"] / by_symbol["trades"]).round(2)
    by_symbol[["gross_pnl", "net_pnl", "best_trade", "worst_trade"]] = \
        by_symbol[["gross_pnl", "net_pnl", "best_trade", "worst_trade"]].round(2)
    by_symbol = by_symbol.sort_values("net_pnl", ascending=False)

    # Day-of-week pattern.
    t["day_of_week"] = t["closed_at_ist"].dt.day_name()
    by_day = t.groupby("day_of_week").agg(
        trades=("id", "count"),
        net_pnl=("net_pnl", "sum"),
        wins=("pnl", lambda s: int((s > 0).sum())),
    ).reset_index()
    by_day["win_rate"] = (by_day["wins"] / by_day["trades"] * 100).round(1)
    by_day["net_pnl"]  = by_day["net_pnl"].round(2)
    by_day["day_of_week"] = pd.Categorical(by_day["day_of_week"], categories=_DAY_ORDER, ordered=True)
    by_day = by_day.sort_values("day_of_week")
    by_day["day_of_week"] = by_day["day_of_week"].astype(str)

    # Entry-hour pattern (IST hour of entry).
    t["entry_hour"] = t["opened_at_ist"].dt.hour
    by_hour = t.groupby("entry_hour").agg(
        trades=("id", "count"),
        net_pnl=("net_pnl", "sum"),
        wins=("pnl", lambda s: int((s > 0).sum())),
    ).reset_index()
    by_hour["win_rate"] = (by_hour["wins"] / by_hour["trades"] * 100).round(1)
    by_hour["net_pnl"]  = by_hour["net_pnl"].round(2)
    by_hour = by_hour.sort_values("entry_hour")

    return dict(trades=trades, by_strategy=by_strategy, equity_curve=equity_curve,
                by_symbol=by_symbol, by_day=by_day, by_hour=by_hour)


def build_strategy_performance_table(trades: pd.DataFrame, by_strategy: pd.DataFrame) -> pd.DataFrame:
    """Win rate, avg win/loss, profit factor, best/worst, avg hold time -- per strategy."""
    if trades is None or trades.empty or by_strategy is None or by_strategy.empty:
        return pd.DataFrame()

    t = trades.copy()
    opened = pd.to_datetime(t["opened_at"], utc=True)
    closed = pd.to_datetime(t["closed_at"], utc=True)
    t["hold_minutes"] = (closed - opened).dt.total_seconds() / 60.0
    hold = t.groupby("strategy")["hold_minutes"].mean().round(1).rename("avg_hold_minutes")

    perf = by_strategy.copy()
    perf["win_rate"] = (perf["wins"] / perf["trades"] * 100).round(1)
    perf["avg_win"]  = (perf["gross_win"]  / perf["wins"].replace(0, pd.NA)).round(2)
    perf["avg_loss"] = (perf["gross_loss"] / perf["losses"].replace(0, pd.NA)).round(2)

    def _profit_factor(row):
        if row["gross_loss"] != 0:
            return round(float(row["gross_win"]) / abs(float(row["gross_loss"])), 2)
        return "∞" if row["gross_win"] > 0 else 0.0
    perf["profit_factor"] = perf.apply(_profit_factor, axis=1)

    perf = perf.merge(hold, left_on="strategy", right_index=True, how="left")
    return perf[[
        "strategy", "trades", "wins", "losses", "win_rate",
        "total_pnl", "total_net_pnl", "total_charges",
        "avg_win", "avg_loss", "profit_factor",
        "best_trade", "worst_trade", "avg_hold_minutes",
    ]]


def build_signal_history_dataset(start_utc, end_utc, strategy=None, timeframe=None) -> dict:
    """Returns dict: signals, by_strategy, by_symbol."""
    signals     = get_signals_for_report(start_utc, end_utc, strategy, timeframe)
    by_strategy = get_signal_summary_by_strategy(start_utc, end_utc, strategy, timeframe)

    if signals.empty:
        return dict(signals=signals, by_strategy=by_strategy, by_symbol=pd.DataFrame())

    by_symbol = signals.groupby("Stock").agg(
        signals=("Signal", "count"),
        buy=("Signal", lambda x: int((x == "BUY").sum())),
        sell=("Signal", lambda x: int((x == "SELL").sum())),
    ).reset_index().sort_values("signals", ascending=False)

    return dict(signals=signals, by_strategy=by_strategy, by_symbol=by_symbol)


def compute_signal_conversion(signals_df: pd.DataFrame, trades_df: pd.DataFrame, tolerance_minutes: int = 10) -> pd.DataFrame:
    """
    Signal -> trade conversion via merge_asof on (symbol, strategy,
    timeframe), forward direction, `tolerance_minutes` window.
    StrategyEngine logs a signal and PaperTrader.on_signal() (if RMS
    allows a trade) fire within the same scan tick, comfortably inside
    this window even accounting for scheduler offsets -- so no new SQL
    join is needed, just a time-tolerant pandas match on the two
    DataFrames already fetched for their own sheets.
    """
    empty_cols = ["strategy", "timeframe", "signals_generated", "trades_taken", "conversion_rate", "signals_skipped"]
    if signals_df is None or signals_df.empty:
        return pd.DataFrame(columns=empty_cols)

    sig = signals_df.rename(columns={
        "Stock": "symbol", "Strategy": "strategy", "Timeframe": "timeframe", "Timestamp": "sig_time",
    })[["symbol", "strategy", "timeframe", "sig_time", "Signal"]]
    sig = sig[sig["Signal"].isin(["BUY", "SELL"])].sort_values("sig_time")

    if trades_df is None or trades_df.empty:
        conv = sig.copy()
        conv["traded"] = False
    else:
        tr = trades_df[["symbol", "strategy", "timeframe", "opened_at"]].copy()
        tr["sig_time"] = pd.to_datetime(tr["opened_at"], utc=True)
        tr["traded"] = True
        tr = tr[["sig_time", "symbol", "strategy", "timeframe", "traded"]].sort_values("sig_time")

        conv = pd.merge_asof(
            sig, tr, on="sig_time", by=["symbol", "strategy", "timeframe"],
            direction="forward", tolerance=pd.Timedelta(minutes=tolerance_minutes),
        )
        conv["traded"] = conv["traded"].where(conv["traded"].notna(), False).astype(bool)

    summary = conv.groupby(["strategy", "timeframe"]).agg(
        signals_generated=("traded", "count"),
        trades_taken=("traded", "sum"),
    ).reset_index()
    summary["trades_taken"]    = summary["trades_taken"].astype(int)
    summary["signals_skipped"] = summary["signals_generated"] - summary["trades_taken"]
    summary["conversion_rate"] = (summary["trades_taken"] / summary["signals_generated"] * 100).round(1)
    return summary.sort_values(["strategy", "timeframe"])
