# ============================================================
# core/strategies/arbitrage_strategy.py
#
# Cash-Futures Arbitrage Strategy — Jwala's exact logic
#
# Conditions (from Jwala's explanation):
#   1. Future price MUST be > Spot price (contango only)
#   2. Basis = (Futures - Spot) / Spot × 100
#   3. Normal days (not expiry week): Basis > 2% → SIGNAL
#   4. Expiry week (last 7 days of month): Basis > 1% → SIGNAL
#
# Action: BUY Spot + SELL Futures simultaneously
# Profit: Locked at entry. Realised at expiry when prices converge.
#
# Jwala's exact words:
#   "If I see a basis of more than 1% I would enter the trade"
#   "For normal days basis should be better than 2%"
#   "Expiry week — 1% will work — capturing 1% in 7 days"
#   "Yearly 12% for 30 days — okay but not wow"
#   "1% in one week — circulate money — nothing like that"
# ============================================================

import os
import requests
import logging
from datetime import datetime, date
import calendar
from typing import Optional

import pytz

from core.strategies.base_strategy import BaseStrategy, SignalResult
from configs.universe import get_lot_size

log = logging.getLogger("arbitrage")
IST = pytz.timezone("Asia/Kolkata")

# ── Threshold constants ──────────────────────────────────────
NORMAL_BASIS_PCT  = 0.5   # Reduced to 0.5% for signal testing (Jwala 06-Jun-2026)
EXPIRY_BASIS_PCT  = 0.5   # Expiry week — same 0.5% for testing
EXPIRY_WEEK_DAYS  = 7     # Last 7 calendar days of month = expiry week
MAX_BASIS_PCT     = 10.0  # Sanity check — above this = data error

# In-memory cache for futures contracts {symbol: {key, expiry, fetched_at}}
_futures_cache: dict = {}


# ============================================================
# EXPIRY WEEK DETECTION
# ============================================================

def is_expiry_week() -> bool:
    """
    Returns True if today is within the last 7 calendar days of the month.
    This is the NSE F&O monthly expiry window.

    Jwala: "In the last week we can execute this strategy at 1% basis.
            Not now — in last week of month."
    """
    today    = datetime.now(IST).date()
    last_day = calendar.monthrange(today.year, today.month)[1]
    days_to_expiry = last_day - today.day
    return days_to_expiry <= EXPIRY_WEEK_DAYS


def get_basis_threshold() -> float:
    """
    Returns the applicable basis threshold based on current date.

    Expiry week  → 1%  (capturing 1% in 7 days is excellent)
    Normal days  → 2%  (need higher spread for 30-day hold)
    """
    if is_expiry_week():
        return EXPIRY_BASIS_PCT
    return NORMAL_BASIS_PCT


def days_to_expiry() -> int:
    """Returns number of calendar days to end of month."""
    today    = datetime.now(IST).date()
    last_day = calendar.monthrange(today.year, today.month)[1]
    return last_day - today.day


# ============================================================
# UPSTOX TOKEN
# ============================================================

def _get_upstox_token() -> Optional[str]:
    try:
        from core.database.db import get_upstox_token
        return get_upstox_token()
    except Exception as e:
        log.warning(f"Could not get Upstox token: {e}")
        return None


# ============================================================
# FUTURES CONTRACT LOOKUP
# ============================================================

def get_active_futures_contract(
    symbol:   str,
    token:    str,
    base_url: str = "https://api.upstox.com/v2",
) -> Optional[dict]:
    """
    Fetch active (front-month) futures contract from Upstox.
    Caches result per day to avoid repeated API calls.
    """
    global _futures_cache

    today = datetime.now(IST).strftime("%Y-%m-%d")

    if symbol in _futures_cache:
        cached = _futures_cache[symbol]
        if cached.get("date") == today:
            return cached

    search_name = symbol.replace(".NS", "")

    try:
        url     = f"{base_url}/instruments/search"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        params  = {"q": search_name, "asset_type": "FO"}

        response = requests.get(url, headers=headers, params=params, timeout=10)

        if response.status_code != 200:
            log.warning(f"Futures search failed for {symbol}: {response.status_code}")
            return None

        data        = response.json()
        instruments = data.get("data", [])

        if not instruments:
            return None

        # Filter NSE futures only, matching symbol name
        nse_futures = [
            inst for inst in instruments
            if inst.get("exchange", "").upper() == "NSE"
            and inst.get("instrument_type", "").upper() in ("FUT", "FO")
            and search_name.upper() in inst.get("tradingsymbol", "").upper()
        ]

        if not nse_futures:
            return None

        # Sort by expiry — pick front month (nearest expiry)
        nse_futures.sort(key=lambda x: x.get("expiry", "9999-99-99"))
        active = nse_futures[0]

        result = {
            "instrument_key": active.get("instrument_key", ""),
            "expiry":         active.get("expiry", ""),
            "tradingsymbol":  active.get("tradingsymbol", ""),
            "lot_size":       active.get("lot_size", get_lot_size(symbol)),
            "date":           today,
        }

        _futures_cache[symbol] = result
        log.info(
            f"Futures contract: {symbol} → {result['tradingsymbol']} "
            f"(expiry: {result['expiry']})"
        )

        return result

    except Exception as e:
        log.error(f"Futures contract lookup error for {symbol}: {e}")
        return None


def get_futures_price(
    instrument_key: str,
    token:          str,
    base_url:       str = "https://api.upstox.com/v2",
) -> Optional[float]:
    """Fetch latest price for a futures contract from Upstox market quote API."""
    try:
        encoded_key = requests.utils.quote(instrument_key, safe="")
        url = f"{base_url}/market-quote/quotes?instrument_key={encoded_key}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        response = requests.get(url, headers=headers, timeout=10)

        if response.status_code != 200:
            log.warning(f"Futures quote failed: {response.status_code}")
            return None

        data   = response.json()
        quotes = data.get("data", {})

        for key, quote_data in quotes.items():
            ltp = quote_data.get("last_price") or quote_data.get("ltp")
            if ltp:
                return float(ltp)

        return None

    except Exception as e:
        log.error(f"Futures price fetch error: {e}")
        return None


# ============================================================
# ARBITRAGE STRATEGY
# ============================================================

class ArbitrageStrategy(BaseStrategy):
    """
    Cash-Futures Spread Arbitrage — Jwala's logic.

    Entry conditions:
        1. Futures price > Spot price (contango)
        2. Basis > 2% on normal days
        3. Basis > 1% in expiry week (last 7 days of month)

    Entry action:
        BUY spot shares + SELL futures contract simultaneously
        Profit is locked at entry = basis amount per share

    Exit:
        At futures expiry — prices converge — profit realised automatically
        No directional risk — purely a spread capture

    Jwala's insight:
        1% in 30 days = 12% annualised = "okay but not wow"
        1% in 7 days  = ~52% annualised = "nothing like that"
        Therefore, last week of month is the prime window.
    """

    name = "Cash-Futures Arbitrage"
    description = (
        "Captures risk-free spread between NSE spot and futures price. "
        "Normal days: enters when basis > 2%. "
        "Expiry week (last 7 days): enters when basis > 1%. "
        "Action: Buy spot + Sell futures. Profit locked at entry."
    )

    def generate_signal(self, df) -> SignalResult:
        """Standard interface — returns HOLD. Use generate_arbitrage_signal() for live scanning."""
        return SignalResult(
            "HOLD", "WEAK",
            "Use generate_arbitrage_signal() for live arbitrage scanning",
            strategy=self.name,
        )

    def generate_arbitrage_signal(
        self,
        symbol:     str,
        spot_price: float,
        token:      str,
    ) -> SignalResult:
        """
        Full arbitrage signal with live futures price lookup.

        Parameters:
            symbol:     yfinance symbol (e.g. "HDFCBANK.NS")
            spot_price: current spot/cash market price
            token:      valid Upstox access token
        """

        # Get active futures contract
        contract = get_active_futures_contract(symbol, token)

        if not contract:
            return SignalResult(
                "HOLD", "WEAK",
                f"No active futures contract found for {symbol}",
                strategy=self.name,
            )

        instrument_key = contract.get("instrument_key", "")
        if not instrument_key:
            return SignalResult(
                "HOLD", "WEAK",
                "Invalid futures instrument key",
                strategy=self.name,
            )

        # Get live futures price
        futures_price = get_futures_price(instrument_key, token)

        if futures_price is None:
            return SignalResult(
                "HOLD", "WEAK",
                f"Could not fetch futures price for {contract.get('tradingsymbol','')}",
                strategy=self.name,
            )

        # ── Condition 1: Futures must be > Spot (contango) ──
        if futures_price <= spot_price:
            spread_pct = round((futures_price - spot_price) / spot_price * 100, 2)
            return SignalResult(
                "HOLD", "WEAK",
                f"Futures ₹{futures_price:,.2f} ≤ Spot ₹{spot_price:,.2f} "
                f"(backwardation: {spread_pct}%). "
                f"Possible dividend pricing. No arbitrage.",
                {"Spot_Price": round(spot_price, 2),
                 "Futures_Price": round(futures_price, 2),
                 "Spread_Pct": spread_pct},
                self.name,
            )

        # ── Calculate basis ──
        spread_abs = futures_price - spot_price
        spread_pct = round((spread_abs / spot_price) * 100, 2)

        # ── Determine threshold based on expiry week ──
        threshold    = get_basis_threshold()
        in_exp_week  = is_expiry_week()
        days_to_exp  = days_to_expiry()
        expiry       = contract.get("expiry", "")
        tradingsymbol = contract.get("tradingsymbol", "")
        lot_size     = contract.get("lot_size", get_lot_size(symbol))

        # ── P&L calculation ──
        gross_profit  = round(spread_abs * lot_size, 2)
        brokerage_est = round(lot_size * 3.5, 2)  # ~₹3.5/share
        net_profit    = round(gross_profit - brokerage_est, 2)
        annualised_pct = round((spread_pct / max(days_to_exp, 1)) * 365, 1)

        indicators = {
            "Spot_Price":       round(spot_price, 2),
            "Futures_Price":    round(futures_price, 2),
            "Spread_Abs":       round(spread_abs, 2),
            "Spread_Pct":       spread_pct,
            "Threshold":        threshold,
            "Lot_Size":         lot_size,
            "Gross_Profit":     gross_profit,
            "Net_Profit_Est":   net_profit,
            "Futures_Symbol":   tradingsymbol,
            "Expiry":           expiry,
            "Days_To_Expiry":   days_to_exp,
            "Annualised_Pct":   annualised_pct,
            "Expiry_Week":      in_exp_week,
        }

        # ── Sanity check ──
        if spread_pct > MAX_BASIS_PCT:
            return SignalResult(
                "HOLD", "WEAK",
                f"Basis {spread_pct}% exceeds {MAX_BASIS_PCT}% — possible data error",
                indicators, self.name,
            )

        # ── Signal decision ──
        if spread_pct >= threshold:
            # Strength based on how much above threshold
            if spread_pct >= threshold * 1.5:
                strength = "STRONG"
            else:
                strength = "MODERATE"

            week_label = "EXPIRY WEEK" if in_exp_week else "NORMAL DAY"

            reason = (
                f"ARBITRAGE: Basis {spread_pct}% > {threshold}% threshold ({week_label})\n"
                f"Spot ₹{spot_price:,.2f} | Futures ₹{futures_price:,.2f} ({tradingsymbol})\n"
                f"BUY {lot_size} shares @ ₹{spot_price:,.2f} + SELL 1 lot @ ₹{futures_price:,.2f}\n"
                f"Gross ₹{gross_profit:,.0f} | Net ~₹{net_profit:,.0f} | "
                f"Expiry: {expiry} ({days_to_exp}d) | ~{annualised_pct}% p.a."
            )

            return SignalResult("BUY", strength, reason, indicators, self.name)

        # Below threshold — no trade
        week_label = f"expiry week (need >{EXPIRY_BASIS_PCT}%)" if in_exp_week \
                     else f"normal day (need >{NORMAL_BASIS_PCT}%)"
        return SignalResult(
            "HOLD", "WEAK",
            f"Basis {spread_pct}% below threshold for {week_label}. "
            f"Spot ₹{spot_price:,.2f} | Futures ₹{futures_price:,.2f} | "
            f"{days_to_exp} days to expiry.",
            indicators, self.name,
        )


# Singleton instance
arbitrage_strategy = ArbitrageStrategy()