# ============================================================
# core/logger/signal_logger.py
#
# Responsibilities:
#   - Log every BUY/SELL signal to Supabase (never HOLD)
#   - Deduplicate: skip if last logged signal for that
#     stock + timeframe is already the same value
#   - Dashboard reads via get_logs() which queries Supabase
# ============================================================

import pandas as pd
from core.database import db


class SignalLogger:

    def log_signal(
        self,
        stock:     str,
        timeframe: str,
        signal:    str,
        rsi:       float,
        price:     float,
        strategy:  str = "RSI Reversal",
    ) -> bool:
        """
        Log a signal to Supabase.
        Returns True if inserted, False if skipped (HOLD or duplicate).
        """
        if signal == "HOLD":
            return False

        # Deduplication — skip same consecutive signal FOR THIS STRATEGY.
        # Per-strategy so parallel strategies (RSI, Volume Spike, Arbitrage)
        # do not mask each other's signals.
        last = db.get_last_signal(stock, timeframe, strategy)
        if last == signal:
            return False

        return db.insert_signal(
            stock=stock,
            timeframe=timeframe,
            signal=signal,
            rsi=rsi,
            price=price,
            strategy=strategy,
        )

    def log_trade_signal(
        self,
        stock:     str,
        timeframe: str,
        signal:    str,
        rsi:       float,
        price:     float,
        strategy:  str = "RSI Reversal",
    ) -> bool:
        """
        Unconditional log for an ALREADY-CONFIRMED trade event (Sep 17)
        -- same shape as log_signal(), but skips the same-as-last-signal
        dedup entirely. Mirrors AlertManager.send_trade_alert()'s fix
        and exact same reasoning: this dedup exists so a continuously-
        polled strategy doesn't re-log every scan cycle while a signal
        persists, but ws_listener.py's 3-Bar-Play path only ever calls
        into here AFTER on_signal() has already confirmed a genuinely
        NEW position was just opened (deduped upstream by the
        open-position guard) -- there's nothing left to de-duplicate.

        Confirmed live, Sep 17: M&M.NS opened a real new BUY today, but
        the `signals` table had nothing for it at all -- get_last_signal
        returned 'BUY' from 2026-07-27, almost two months earlier, so
        log_signal()'s dedup silently skipped the insert. Same root
        cause and same fix shape as the alert_states bug found the same
        day.
        """
        if signal == "HOLD":
            return False
        return db.insert_signal(
            stock=stock,
            timeframe=timeframe,
            signal=signal,
            rsi=rsi,
            price=price,
            strategy=strategy,
        )

    def get_logs(
        self,
        timeframe: str = None,
        strategy:  str = None,
        days:      int = 7,
    ) -> pd.DataFrame:
        """
        Fetch signal logs from Supabase.
        Returns DataFrame sorted newest first.
        Always returns a DataFrame (never raises).
        """
        return db.get_signals(
            timeframe=timeframe,
            strategy=strategy,
            days=days,
        )