# ============================================================
# core/alerts/alert_manager.py
#
# Responsibilities:
#   - Track last known signal per stock + timeframe
#   - Fire alert ONLY when signal changes (state transition)
#   - Ignore HOLD → HOLD noise
#   - Send Telegram message on BUY/SELL transition
#   - Read Telegram credentials from .env (never hardcoded)
# ============================================================

import os
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

_COLUMNS = ["Timeframe", "Stock", "Signal"]


class AlertManager:

    def __init__(self, alert_file: str = "data/last_signals.csv"):
        os.makedirs("data", exist_ok=True)
        self.alert_file = alert_file

        if not os.path.exists(self.alert_file):
            pd.DataFrame(columns=_COLUMNS).to_csv(
                self.alert_file, index=False
            )

        # Telegram credentials from environment
        self._bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id   = os.getenv("TELEGRAM_CHAT_ID", "")

    # ----------------------------------------------------------
    # CHECK ALERT
    # ----------------------------------------------------------

    def check_alert(
        self,
        timeframe: str,
        stock: str,
        current_signal: str,
        rsi: float,
        price: float,
    ) -> dict | None:
        """
        Returns an alert dict if the signal changed, else None.

        Alert dict shape:
            {
                "stock":    str,
                "timeframe": str,
                "signal":   str,   # current
                "previous": str,   # previous
                "rsi":      float,
                "price":    float,
            }
        """
        df = self._read()

        mask = (df["Stock"] == stock) & (df["Timeframe"] == timeframe)
        existing = df[mask]

        # ---- first time we see this stock + timeframe ----
        if existing.empty:
            new_row = pd.DataFrame([{
                "Timeframe": timeframe,
                "Stock":     stock,
                "Signal":    current_signal,
            }])
            df = pd.concat([df, new_row], ignore_index=True)
            df.to_csv(self.alert_file, index=False)
            return None  # first occurrence is never an alert

        previous_signal = existing.iloc[-1]["Signal"]

        # ---- ignore HOLD noise ----
        if current_signal == "HOLD":
            return None

        # ---- only alert on actual transition ----
        if previous_signal == current_signal:
            return None

        # ---- update stored state ----
        df.loc[mask, "Signal"] = current_signal
        df.to_csv(self.alert_file, index=False)

        alert = {
            "stock":     stock,
            "timeframe": timeframe,
            "signal":    current_signal,
            "previous":  previous_signal,
            "rsi":       round(float(rsi), 2),
            "price":     round(float(price), 2),
        }

        # ---- send Telegram if configured ----
        if self._bot_token and self._chat_id:
            self._send_telegram(alert)

        return alert

    # ----------------------------------------------------------
    # TELEGRAM
    # ----------------------------------------------------------

    def _send_telegram(self, alert: dict) -> None:
        emoji = "🟢" if alert["signal"] == "BUY" else "🔴"
        message = (
            f"{emoji} *ALGO SIGNAL*\n"
            f"Stock     : `{alert['stock']}`\n"
            f"Signal    : *{alert['signal']}*  (was {alert['previous']})\n"
            f"RSI       : `{alert['rsi']}`\n"
            f"Price     : `₹{alert['price']}`\n"
            f"Timeframe : `{alert['timeframe']}`"
        )

        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"

        try:
            requests.post(
                url,
                json={
                    "chat_id":    self._chat_id,
                    "text":       message,
                    "parse_mode": "Markdown",
                },
                timeout=5,
            )
        except Exception:
            pass  # never crash the dashboard on Telegram failures

    # ----------------------------------------------------------
    # INTERNAL
    # ----------------------------------------------------------

    def _read(self) -> pd.DataFrame:
        if not os.path.exists(self.alert_file):
            return pd.DataFrame(columns=_COLUMNS)

        try:
            df = pd.read_csv(self.alert_file)
            for col in _COLUMNS:
                if col not in df.columns:
                    df[col] = None
            return df
        except Exception:
            return pd.DataFrame(columns=_COLUMNS)
