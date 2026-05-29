# ============================================================
# core/alerts/alert_manager.py
#
# Enhanced with:
#   - Signal strength (STRONG / MODERATE / WEAK)
#   - Strategy explanation (why this is a signal)
#   - Key indicator values
#   - Arbitrage breakdown (spread %, profit estimate)
#   - Clean formatted Telegram message
# ============================================================

import os
import pytz
import requests
from datetime import datetime
from dotenv import load_dotenv

from core.database import db

load_dotenv()

IST = pytz.timezone("Asia/Kolkata")

_DISPLAY_NAMES = {
    "GC=F":     "GOLD",
    "SI=F":     "SILVER",
    "HG=F":     "COPPER",
    "CL=F":     "CRUDE OIL",
    "^NSEI":    "NIFTY 50",
    "^NSEBANK": "BANK NIFTY",
    "^BSESN":   "SENSEX",
}


def _display_name(symbol: str) -> str:
    if symbol in _DISPLAY_NAMES:
        return _DISPLAY_NAMES[symbol]
    return symbol.replace(".NS", "")


class AlertManager:

    def __init__(self):
        self._bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id   = os.getenv("TELEGRAM_CHAT_ID", "")

    def check_alert(
        self,
        timeframe:     str,
        stock:         str,
        current_signal: str,
        rsi:           float,
        price:         float,
        strategy:      str  = "RSI Reversal",
        signal_result  = None,  # SignalResult object if available
    ) -> dict | None:
        """
        Returns alert dict if signal changed, else None.
        Fires Telegram message on state transition.
        """
        previous_signal = db.get_alert_state(stock, timeframe)

        if previous_signal is None:
            db.upsert_alert_state(stock, timeframe, current_signal)
            return None

        if current_signal == "HOLD":
            return None

        if previous_signal == current_signal:
            return None

        db.upsert_alert_state(stock, timeframe, current_signal)

        alert = {
            "stock":     stock,
            "timeframe": timeframe,
            "signal":    current_signal,
            "previous":  previous_signal,
            "rsi":       round(float(rsi), 2),
            "price":     round(float(price), 2),
            "strategy":  strategy,
            "signal_result": signal_result,
        }

        if self._bot_token and self._chat_id:
            self._send_telegram(alert)

        return alert

    def _send_telegram(self, alert: dict) -> None:
        signal   = alert["signal"]
        name     = _display_name(alert["stock"])
        ist_now  = datetime.now(IST).strftime("%d %b %Y  %H:%M IST")
        strategy = alert["strategy"]
        tf       = alert["timeframe"]
        rsi      = alert["rsi"]
        price    = alert["price"]
        previous = alert["previous"]
        result   = alert.get("signal_result")

        # Signal emoji and header
        if signal == "BUY":
            header_emoji = "🟢"
            action_line  = "📈 *LONG OPPORTUNITY*"
        else:
            header_emoji = "🔴"
            action_line  = "📉 *SHORT OPPORTUNITY*"

        # Strength emoji
        strength = "MODERATE"
        if result:
            strength = result.strength
        strength_emoji = {"STRONG": "💪", "MODERATE": "👍", "WEAK": "👌"}.get(strength, "👍")

        # Price format
        try:
            price_str = f"₹{float(price):,.2f}"
        except Exception:
            price_str = str(price)

        # Build message
        lines = [
            f"{header_emoji} *ALGO SIGNAL — {signal}*",
            f"━━━━━━━━━━━━━━━━━━━━━",
            f"📌 *{name}*",
            f"{action_line}",
            f"",
            f"📊 *Signal Details*",
            f"Strength  : {strength_emoji} `{strength}`",
            f"Strategy  : `{strategy}`",
            f"Timeframe : `{tf}`",
            f"Price     : `{price_str}`",
        ]

        # RSI or Spread depending on strategy
        if strategy == "Cash-Futures Arbitrage":
            lines.append(f"Spread    : `{rsi}%`")
        else:
            lines.append(f"RSI       : `{rsi}`")

        lines.append(f"Changed   : `{previous} → {signal}`")
        lines.append(f"")

        # Strategy explanation — why this is a signal
        if result and result.reason:
            lines.append(f"🧠 *Why this signal?*")
            # For arbitrage, reason is multi-line — format nicely
            if strategy == "Cash-Futures Arbitrage":
                reason_lines = result.reason.split("\n")
                for rl in reason_lines[:6]:  # max 6 lines
                    if rl.strip():
                        lines.append(f"  {rl.strip()}")
            else:
                # Truncate long reasons
                reason = result.reason
                if len(reason) > 200:
                    reason = reason[:197] + "..."
                lines.append(f"  _{reason}_")
            lines.append(f"")

        # Key indicators
        if result and result.indicators:
            lines.append(f"📈 *Key Indicators*")
            indicators = result.indicators

            if strategy == "Cash-Futures Arbitrage":
                spot    = indicators.get("Spot_Price", 0)
                futures = indicators.get("Futures_Price", 0)
                spread  = indicators.get("Spread_Abs", 0)
                lot     = indicators.get("Lot_Size", 0)
                gross   = indicators.get("Gross_Profit", 0)
                net     = indicators.get("Net_Profit_Est", 0)
                expiry  = indicators.get("Expiry", "")
                fut_sym = indicators.get("Futures_Symbol", "")
                lines += [
                    f"  Spot     : `₹{spot:,.2f}`",
                    f"  Futures  : `₹{futures:,.2f}` ({fut_sym})",
                    f"  Spread   : `₹{spread:.2f}/share`",
                    f"  Lot size : `{lot} shares`",
                    f"  Gross P&L: `₹{gross:,.0f}`",
                    f"  Est. Net : `₹{net:,.0f}`",
                    f"  Expiry   : `{expiry}`",
                ]
            elif strategy == "RSI + Pivot Confluence":
                lines += [
                    f"  RSI : `{indicators.get('RSI', '')}` (prev: {indicators.get('RSI_prev', '')})",
                    f"  PP  : `₹{indicators.get('PP', '')}`",
                    f"  S1  : `₹{indicators.get('S1', '')}`",
                    f"  R1  : `₹{indicators.get('R1', '')}`",
                ]
            elif strategy == "Bollinger Bands":
                lines += [
                    f"  BB%B  : `{indicators.get('BB_PCT', '')}`",
                    f"  Upper : `₹{indicators.get('BB_UPPER', '')}`",
                    f"  Lower : `₹{indicators.get('BB_LOWER', '')}`",
                    f"  Mid   : `₹{indicators.get('BB_MID', '')}`",
                ]
            elif strategy == "EMA Crossover":
                lines += [
                    f"  EMA9  : `₹{indicators.get('EMA_9', '')}`",
                    f"  EMA20 : `₹{indicators.get('EMA_20', '')}`",
                    f"  EMA50 : `₹{indicators.get('EMA_50', '')}`",
                    f"  Trend : `{indicators.get('Trend', '')}`",
                ]
            elif strategy == "MACD":
                lines += [
                    f"  MACD   : `{indicators.get('MACD', '')}`",
                    f"  Signal : `{indicators.get('MACD_Signal', '')}`",
                    f"  Hist   : `{indicators.get('MACD_Hist', '')}`",
                ]
            elif strategy == "Volume Breakout":
                lines += [
                    f"  Vol Ratio  : `{indicators.get('Vol_Ratio', '')}x avg`",
                    f"  Period High: `₹{indicators.get('Period_High', '')}`",
                    f"  Period Low : `₹{indicators.get('Period_Low', '')}`",
                ]
            else:
                # Generic RSI
                lines.append(f"  RSI : `{rsi}`")
            lines.append(f"")

        # Footer
        lines += [
            f"🕐 `{ist_now}`",
            f"━━━━━━━━━━━━━━━━━━━━━",
            f"_For research purposes only. Not financial advice._",
        ]

        message = "\n".join(lines)

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
            pass