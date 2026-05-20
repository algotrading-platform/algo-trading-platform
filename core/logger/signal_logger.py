# ============================================================
# core/logger/signal_logger.py
#
# Responsibilities:
#   - Log every BUY / SELL signal (never HOLD)
#   - Deduplicate: skip if last logged signal for that
#     stock + timeframe is already the same value
#   - Rolling 7-day window: prune older entries on every write
#   - Thread-safe enough for single-process Streamlit usage
# ============================================================

import os
import pandas as pd
from datetime import datetime, timedelta


_COLUMNS = ["Timestamp", "Stock", "Timeframe", "Signal", "RSI", "Price"]


class SignalLogger:

    def __init__(self, log_file: str = "data/signal_logs.csv"):
        os.makedirs("data", exist_ok=True)
        self.file = log_file

        if not os.path.exists(self.file):
            pd.DataFrame(columns=_COLUMNS).to_csv(
                self.file, index=False
            )

    # ----------------------------------------------------------
    # LOG
    # ----------------------------------------------------------

    def log_signal(
        self,
        stock: str,
        timeframe: str,
        signal: str,
        rsi: float,
        price: float,
    ) -> bool:
        """
        Returns True if a new entry was written, False if skipped.
        """
        if signal == "HOLD":
            return False

        df = self._read()

        # --- deduplication: skip same consecutive signal ---
        mask = (df["Stock"] == stock) & (df["Timeframe"] == timeframe)
        history = df[mask]

        if not history.empty:
            last = history.iloc[-1]["Signal"]
            if last == signal:
                return False

        # --- append new entry ---
        new_row = {
            "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "Stock":     stock,
            "Timeframe": timeframe,
            "Signal":    signal,
            "RSI":       round(float(rsi), 2),
            "Price":     round(float(price), 2),
        }

        df = pd.concat(
            [df, pd.DataFrame([new_row])],
            ignore_index=True
        )

        # --- rolling 7-day window ---
        df["Timestamp"] = pd.to_datetime(df["Timestamp"])
        cutoff = datetime.now() - timedelta(days=7)
        df = df[df["Timestamp"] >= cutoff]

        df.to_csv(self.file, index=False)
        return True

    # ----------------------------------------------------------
    # READ
    # ----------------------------------------------------------

    def get_logs(self) -> pd.DataFrame:
        """
        Returns logs sorted newest-first.
        Always returns a DataFrame (never raises).
        """
        df = self._read()

        if df.empty:
            return df

        try:
            df["Timestamp"] = pd.to_datetime(df["Timestamp"])
            df = df.sort_values("Timestamp", ascending=False)
        except Exception:
            pass

        return df

    # ----------------------------------------------------------
    # INTERNAL
    # ----------------------------------------------------------

    def _read(self) -> pd.DataFrame:
        if not os.path.exists(self.file):
            return pd.DataFrame(columns=_COLUMNS)

        try:
            df = pd.read_csv(self.file)
            # ensure all expected columns exist
            for col in _COLUMNS:
                if col not in df.columns:
                    df[col] = None
            return df
        except Exception:
            return pd.DataFrame(columns=_COLUMNS)
