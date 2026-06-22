# ============================================================
# core/alerts/alert_manager.py
#
# FIXES (2026-06-19):
#   - Nifty message: shows trend arrows only (not RSI values)
#   - Stock RSI: shows D/H/5m separately per Jwala spec
#   - Email fallback when Telegram fails/banned
#   - Clean message format per Jwala's requirements
# ============================================================

import os
import pytz
import requests
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from dotenv import load_dotenv

from core.database import db

load_dotenv()

log = logging.getLogger("alert_manager")
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
        self._bot_token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id     = os.getenv("TELEGRAM_CHAT_ID", "")
        # Email fallback config
        self._email_from  = os.getenv("ALERT_EMAIL_FROM", "")
        self._email_to    = os.getenv("ALERT_EMAIL_TO", "")
        self._email_pass  = os.getenv("ALERT_EMAIL_PASSWORD", "")
        self._smtp_host   = os.getenv("ALERT_SMTP_HOST", "smtp.gmail.com")
        self._smtp_port   = int(os.getenv("ALERT_SMTP_PORT", "587"))

    def check_alert(
        self,
        timeframe:      str,
        stock:          str,
        current_signal: str,
        rsi:            float,
        price:          float,
        strategy:       str  = "RSI Reversal",
        signal_result          = None,
        data_source:    str  = "yfinance",
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

        # Try Telegram first, fallback to email
        telegram_sent = False
        if self._bot_token and self._chat_id:
            telegram_sent = self._send_telegram(alert)

        if not telegram_sent and self._email_from and self._email_to:
            self._send_email(alert)

        return alert

    def _build_message(self, alert: dict) -> str:
        """
        Build alert message per Jwala's exact spec (18-Jun-2026):

        Line 1: emoji + Stock + B/S + price + time
        Line 2: strength + strategy
        Line 3: Nifty: D↑ H↑ 5m↑  (trend arrows ONLY — no RSI for Nifty)
        Line 4: Stock: D↑(52) H↑(38) 5m↓(28)  (trend + RSI values for stock)
        Line 5: timeframe + data source
        """
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

        sig_letter = "B" if signal == "BUY" else "S"
        sig_emoji  = "🟢" if signal == "BUY" else "🔴"

        try:
            price_str = f"₹{float(price):,.2f}"
        except Exception:
            price_str = str(price)

        # Arbitrage format
        if strategy == "Cash-Futures Arbitrage":
            spread     = alert["rsi"]
            indicators = result.indicators if result else {}
            gross      = indicators.get("Gross_Profit", 0)
            net        = indicators.get("Net_Profit_Est", 0)
            expiry     = indicators.get("Expiry", "")
            fut_sym    = indicators.get("Futures_Symbol", "")

            return (
                f"🔵 *{name}  ARB  {price_str}  {ist_now}*\n"
                f"Spread `{spread}%`  Futures: `{fut_sym}`  Exp: `{expiry}`\n"
                f"Gross `₹{gross:,.0f}`  Net `₹{net:,.0f}`\n"
                f"_Buy spot + Sell futures simultaneously_"
            )

        # RSI / other strategy format
        str_emoji   = _strength_emoji(strength)
        data_source = alert.get("data_source", "yfinance")
        src_tag     = "✅" if data_source == "upstox" else "⚠"

        indicators  = result.indicators if (result and hasattr(result, "indicators")) else {}

        # Volume spike info
        vol_label = indicators.get("volume_label", "")
        vol_line  = f"  |  {vol_label}" if vol_label and "🔥" in vol_label else ""

        # ── Nifty: trend arrows ONLY (per Jwala 18-Jun: "I'm not looking for RSI level")
        nifty_d = _trend_arrow(indicators.get("nifty_daily_trend",  nifty_trend))
        nifty_h = _trend_arrow(indicators.get("nifty_hourly_trend", "NEUTRAL"))
        nifty_5 = _trend_arrow(indicators.get("nifty_5min_trend",   "NEUTRAL"))
        nifty_line = f"Nifty: D{nifty_d} H{nifty_h} 5m{nifty_5}"

        # ── Stock: trend arrows + RSI values (per Jwala: "RSI DHM for the stock only")
        def _rsi_str(val):
            if val is None: return "—"
            return str(val)

        s_d  = _rsi_str(indicators.get("stock_rsi_daily"))
        s_h  = _rsi_str(indicators.get("stock_rsi_hourly"))
        s_5  = _rsi_str(indicators.get("stock_rsi_5min"))
        stk_d = _trend_arrow(indicators.get("stock_daily_trend",  stock_trend))
        stk_h = _trend_arrow(indicators.get("stock_hourly_trend", "NEUTRAL"))
        stk_5 = _trend_arrow(indicators.get("stock_5min_trend",   "NEUTRAL"))
        stock_line = f"Stock: D{stk_d}({s_d}) H{stk_h}({s_h}) 5m{stk_5}({s_5})"

        return (
            f"{sig_emoji} *{name}  {sig_letter}  {price_str}  {ist_now}*\n"
            f"{str_emoji} {strength}  |  {strategy}{vol_line}\n"
            f"{nifty_line}\n"
            f"{stock_line}  |  {tf}  {src_tag}"
        )

    def _send_telegram(self, alert: dict) -> bool:
        """Send Telegram message. Returns True on success."""
        message = self._build_message(alert)
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        try:
            resp = requests.post(
                url,
                json={
                    "chat_id":    self._chat_id,
                    "text":       message,
                    "parse_mode": "Markdown",
                },
                timeout=5,
            )
            if resp.status_code == 200:
                return True
            log.warning(f"Telegram failed: {resp.status_code} — {resp.text[:100]}")
            return False
        except Exception as e:
            log.warning(f"Telegram error: {e}")
            return False

    def _send_email(self, alert: dict) -> bool:
        """
        Email fallback alert — used when Telegram is unavailable/banned.
        Configure via environment variables:
          ALERT_EMAIL_FROM     : sender email (e.g. alerts@gmail.com)
          ALERT_EMAIL_TO       : recipient email (Jwala's email)
          ALERT_EMAIL_PASSWORD : app password (not account password)
          ALERT_SMTP_HOST      : smtp.gmail.com (default)
          ALERT_SMTP_PORT      : 587 (default)
        """
        try:
            signal = alert["signal"]
            name   = _display_name(alert["stock"])
            price  = alert["price"]
            tf     = alert["timeframe"]

            subject = f"[ALGO] {signal}: {name} @ ₹{price:,.2f} | {tf}"
            body    = self._build_message(alert)
            # Remove markdown formatting for email
            body = body.replace("*", "").replace("`", "").replace("_", "")

            msg = MIMEMultipart()
            msg["From"]    = self._email_from
            msg["To"]      = self._email_to
            msg["Subject"] = subject
            msg.attach(MIMEText(body, "plain"))

            with smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=10) as server:
                server.starttls()
                server.login(self._email_from, self._email_pass)
                server.send_message(msg)

            log.info(f"Email alert sent: {name} {signal}")
            return True

        except Exception as e:
            log.warning(f"Email alert failed: {e}")
            return False