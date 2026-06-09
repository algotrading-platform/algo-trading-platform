# ============================================================
# core/alerts/alert_manager.py
#
# Telegram format — Jwala's exact spec:
# Line 1: emoji + Stock + signal letter + price + time
# Line 2: strength + strategy + Nifty trend + Stock trend
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


def _trend_arrow(trend: str) -> str:
    if trend == "RISING":  return "↑"
    if trend == "FALLING": return "↓"
    return "→"


def _strength_emoji(strength: str) -> str:
    if strength == "VERY STRONG": return "💎"
    if strength == "STRONG":      return "💪"
    if strength == "MODERATE":    return "👍"
    return "👌"


class AlertManager:

    def __init__(self):
        self._bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id   = os.getenv("TELEGRAM_CHAT_ID", "")

    def check_alert(
        self,
        timeframe:      str,
        stock:          str,
        current_signal: str,
        rsi:            float,
        price:          float,
        strategy:       str  = "RSI Reversal",
        signal_result   = None,
        data_source:    str  = "yfinance",  # "upstox" or "yfinance"
    ) -> dict | None:

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
            "stock":         stock,
            "timeframe":     timeframe,
            "signal":        current_signal,
            "previous":      previous_signal,
            "rsi":           round(float(rsi), 2),
            "price":         round(float(price), 2),
            "strategy":      strategy,
            "signal_result": signal_result,
            "data_source":   data_source,
        }

        if self._bot_token and self._chat_id:
            self._send_telegram(alert)

        return alert

    def _send_telegram(self, alert: dict) -> None:
        signal   = alert["signal"]
        name     = _display_name(alert["stock"])
        tf       = alert["timeframe"]
        price    = alert["price"]
        strategy = alert["strategy"]
        result   = alert.get("signal_result")

        ist_now  = datetime.now(IST).strftime("%H:%M IST")

        nifty_trend = "NEUTRAL"
        stock_trend = "NEUTRAL"
        strength    = "MODERATE"

        if result:
            nifty_trend = getattr(result, "nifty_trend", "NEUTRAL")
            stock_trend = getattr(result, "stock_trend", "NEUTRAL")
            strength    = getattr(result, "strength",    "MODERATE")

        # Signal letter: B for buy, S for sell
        sig_letter = "B" if signal == "BUY" else "S"
        sig_emoji  = "🟢" if signal == "BUY" else "🔴"

        try:
            price_str = f"₹{float(price):,.2f}"
        except Exception:
            price_str = str(price)

        # ── Arbitrage format ──
        if strategy == "Cash-Futures Arbitrage":
            spread     = alert["rsi"]
            indicators = result.indicators if result else {}
            gross      = indicators.get("Gross_Profit", 0)
            net        = indicators.get("Net_Profit_Est", 0)
            expiry     = indicators.get("Expiry", "")
            fut_sym    = indicators.get("Futures_Symbol", "")

            message = (
                f"🔵 *{name}  ARB  {price_str}  {ist_now}*\n"
                f"Spread `{spread}%`  Futures: `{fut_sym}`  Exp: `{expiry}`\n"
                f"Gross `₹{gross:,.0f}`  Net `₹{net:,.0f}`\n"
                f"_Buy spot + Sell futures simultaneously_"
            )

        # ── RSI / other strategy format — Jwala's spec ──
        else:
            str_emoji   = _strength_emoji(strength)
            data_source = alert.get("data_source", "yfinance")
            src_tag     = "✅" if data_source == "upstox" else "⚠"

            # 3-arrow trend labels — D/H/5m for both Nifty and Stock
            indicators   = result.indicators if (result and hasattr(result, "indicators")) else {}
            nifty_label  = indicators.get("nifty_trend_label") or f"N{_trend_arrow(nifty_trend)}"
            stock_label  = indicators.get("stock_trend_label") or f"S{_trend_arrow(stock_trend)}"

            # Volume spike info
            vol_label = indicators.get("volume_label", "")
            vol_line  = f"  |  {vol_label}" if vol_label and "🔥" in vol_label else ""

            # RSI D/H/5m values
            def _rsi_str(val):
                if val is None: return "–"
                return str(val)

            n_d = _rsi_str(indicators.get("nifty_rsi_daily"))
            n_h = _rsi_str(indicators.get("nifty_rsi_hourly"))
            n_5 = _rsi_str(indicators.get("nifty_rsi_5min"))
            s_d = _rsi_str(indicators.get("stock_rsi_daily"))
            s_h = _rsi_str(indicators.get("stock_rsi_hourly"))
            s_5 = _rsi_str(indicators.get("stock_rsi_5min"))

            # Format:
            # 🔴 GODREJCP  S  ₹995.10  14:11 IST
            # 💎 VERY STRONG  |  RSI Reversal  |  VOL 2400% 🔥
            # Nifty: D↓(44) H↓(38) 5m↓(29)
            # Stock: D↑(58) H↑(62) 5m↑(34)  |  1H  ⚠
            nifty_rsi_line = f"Nifty: D{_trend_arrow(nifty_trend)}({n_d}) H{_trend_arrow(nifty_trend)}({n_h}) 5m{_trend_arrow(nifty_trend)}({n_5})"
            stock_rsi_line = f"Stock: D{_trend_arrow(stock_trend)}({s_d}) H{_trend_arrow(stock_trend)}({s_h}) 5m{_trend_arrow(stock_trend)}({s_5})"

            message = (
                f"{sig_emoji} *{name}  {sig_letter}  {price_str}  {ist_now}*\n"
                f"{str_emoji} {strength}  |  {strategy}{vol_line}\n"
                f"{nifty_rsi_line}\n"
                f"{stock_rsi_line}  |  {tf}  {src_tag}"
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
            pass