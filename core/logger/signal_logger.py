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

        # Deduplication — skip same consecutive signal
        last = db.get_last_signal(stock, timeframe)
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