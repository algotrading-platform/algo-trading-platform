# ============================================================
# core/alerts/alert_manager.py
#
# Responsibilities:
#   - Track last known signal per stock+timeframe in Supabase
#   - Fire Telegram alert ONLY when signal changes
#   - Telegram message includes:
#       - Proper commodity names (GOLD not GC=F)
#       - Timestamp (IST)
#       - Strategy name
#       - Signal grade (when implemented)
# ============================================================

import os
import pytz
import requests
from datetime import datetime
from dotenv import load_dotenv

from core.database import db

load_dotenv()

IST = pytz.timezone("Asia/Kolkata")

# Human-readable display names for all instruments
_DISPLAY_NAMES = {
    # Commodities
    "GC=F":  "GOLD",
    "SI=F":  "SILVER",
    "HG=F":  "COPPER",
    "CL=F":  "CRUDE OIL",
    # Indexes
    "^NSEI":    "NIFTY 50",
    "^NSEBANK": "BANK NIFTY",
    "^BSESN":   "SENSEX",
}


def _display_name(symbol: str) -> str:
    """Returns human-readable name for any symbol."""
    if symbol in _DISPLAY_NAMES:
        return _DISPLAY_NAMES[symbol]
    # NSE stocks — strip .NS suffix
    return symbol.replace(".NS", "")


class AlertManager:

    def __init__(self):
        self._bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id   = os.getenv("TELEGRAM_CHAT_ID", "")

    # ----------------------------------------------------------
    # CHECK ALERT
    # ----------------------------------------------------------

    def check_alert(
        self,
        timeframe: str,
        stock:     str,
        current_signal: str,
        rsi:       float,
        price:     float,
        strategy:  str = "RSI Reversal",
    ) -> dict | None:
        """
        Returns alert dict if signal changed, else None.
        Fires Telegram message on state transition.
        """
        previous_signal = db.get_alert_state(stock, timeframe)

        # First time we see this stock+timeframe — store state, no alert
        if previous_signal is None:
            db.upsert_alert_state(stock, timeframe, current_signal)
            return None

        # Ignore HOLD
        if current_signal == "HOLD":
            return None

        # No change — no alert
        if previous_signal == current_signal:
            return None

        # Signal changed — update state and fire alert
        db.upsert_alert_state(stock, timeframe, current_signal)

        alert = {
            "stock":     stock,
            "timeframe": timeframe,
            "signal":    current_signal,
            "previous":  previous_signal,
            "rsi":       round(float(rsi), 2),
            "price":     round(float(price), 2),
            "strategy":  strategy,
        }

        if self._bot_token and self._chat_id:
            self._send_telegram(alert)

        return alert

    # ----------------------------------------------------------
    # TELEGRAM — with all fixes Jwala requested
    # ----------------------------------------------------------

    def _send_telegram(self, alert: dict) -> None:
        emoji     = "🟢" if alert["signal"] == "BUY" else "🔴"
        name      = _display_name(alert["stock"])
        ist_now   = datetime.now(IST).strftime("%d-%m-%Y %H:%M IST")
        signal    = alert["signal"]
        previous  = alert["previous"]
        rsi       = alert["rsi"]
        price     = alert["price"]
        timeframe = alert["timeframe"]
        strategy  = alert["strategy"]

        # Format price — commodities have large numbers
        try:
            price_str = f"₹{float(price):,.2f}"
        except Exception:
            price_str = str(price)

        message = (
            f"{emoji} *ALGO SIGNAL*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"Instrument : `{name}`\n"
            f"Signal     : *{signal}*  (was {previous})\n"
            f"Strategy   : `{strategy}`\n"
            f"Timeframe  : `{timeframe}`\n"
            f"RSI        : `{rsi}`\n"
            f"Price      : `{price_str}`\n"
            f"Time       : `{ist_now}`\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"_For research purposes only. Not financial advice._"
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
            pass  # never crash on Telegram failure