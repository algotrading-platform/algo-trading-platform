# ============================================================
# core/strategies/arbitrage_strategy.py
#
# Cash-Futures Arbitrage Strategy — Jwala's exact logic
#
# FIXES (2026-06-19):
#   - Futures contracts cached in PostgreSQL (persists across jobs)
#   - Added timeout to all API calls (prevent hanging)
#   - Added rate limiting delay between calls
#   - In-memory cache as L1, PostgreSQL as L2
# ============================================================

import os
import time
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
# Raised from 0.5% (Jwala, Jul 11: "let's increase the value to at
# least 0.9%" — the 0.5% was an explicit testing value to check
# whether signals fired at all, not the real target).
NORMAL_BASIS_PCT  = 0.9
EXPIRY_BASIS_PCT  = 0.9
EXPIRY_WEEK_DAYS  = 7
MAX_BASIS_PCT     = 10.0

# ── In-memory L1 cache {symbol: {key, expiry, fetched_at, date}} ──
_futures_cache: dict = {}

# ── Rate limiting ────────────────────────────────────────────
_last_api_call: float = 0.0
_API_MIN_INTERVAL = 0.3  # 300ms between calls = max 3 calls/sec


def _rate_limit():
    """Enforce minimum interval between Upstox API calls."""
    global _last_api_call
    elapsed = time.time() - _last_api_call
    if elapsed < _API_MIN_INTERVAL:
        time.sleep(_API_MIN_INTERVAL - elapsed)
    _last_api_call = time.time()


# ============================================================
# EXPIRY WEEK DETECTION
# ============================================================

def is_expiry_week() -> bool:
    today    = datetime.now(IST).date()
    last_day = calendar.monthrange(today.year, today.month)[1]
    return (last_day - today.day) <= EXPIRY_WEEK_DAYS


def get_basis_threshold() -> float:
    return EXPIRY_BASIS_PCT if is_expiry_week() else NORMAL_BASIS_PCT


def days_to_expiry() -> int:
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
# POSTGRESQL CACHE FOR FUTURES CONTRACTS
# Persists across Container Job runs — fixes 429 rate limiting
# ============================================================

def _load_futures_from_db(symbol: str) -> Optional[dict]:
    """Load futures contract from PostgreSQL cache."""
    try:
        from core.database.db import get_config
        import json
        val = get_config(f"futures_contract_{symbol.replace('.', '_')}")
        if not val:
            return None
        data = json.loads(val)
        # Check if cached today
        if data.get("date") == date.today().isoformat():
            return data
        return None
    except Exception:
        return None


def _save_futures_to_db(symbol: str, contract: dict) -> None:
    """Save futures contract to PostgreSQL cache."""
    try:
        from core.database.db import set_config
        import json
        key = f"futures_contract_{symbol.replace('.', '_')}"
        set_config(key, json.dumps(contract))
    except Exception:
        pass


# ============================================================
# FUTURES CONTRACT LOOKUP
# L1: in-memory cache
# L2: PostgreSQL cache (persists across job runs)
# L3: Upstox API (rate-limited)
# ============================================================

def get_active_futures_contract(
    symbol:   str,
    token:    str,
    base_url: str = "https://api.upstox.com/v2",
) -> Optional[dict]:
    """
    Fetch active (front-month) futures contract.
    Uses 3-level cache to avoid repeated API calls:
      L1: in-memory (fast, lost on restart)
      L2: PostgreSQL (persists across Container Job runs)
      L3: Upstox API (rate-limited, last resort)
    """
    global _futures_cache

    today = date.today().isoformat()

    # L1: in-memory cache
    if symbol in _futures_cache:
        cached = _futures_cache[symbol]
        if cached.get("date") == today and cached.get("instrument_key"):
            return cached

    # L2: PostgreSQL cache — only trust it if it's from TODAY and has a
    # real instrument_key (guards against stale/partial entries left by
    # the old broken search from masking a good fresh fetch).
    db_cached = _load_futures_from_db(symbol)
    if db_cached and db_cached.get("date") == today and db_cached.get("instrument_key"):
        _futures_cache[symbol] = db_cached
        return db_cached

    # L3: Upstox API (rate-limited)
    _rate_limit()

    search_name = symbol.replace(".NS", "")

    try:
        url     = f"{base_url}/instruments/search"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        # Upstox expects the parameter named 'query' (NOT 'q' — 'q' returns HTTP 400).
        params  = {"query": search_name, "asset_type": "FO"}

        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=8,
        )

        if response.status_code == 429:
            log.warning(f"Futures search rate limited for {symbol} — skipping")
            return None

        if response.status_code != 200:
            log.warning(f"Futures search failed for {symbol}: {response.status_code}")
            return None

        data        = response.json()
        instruments = data.get("data", [])

        if not instruments:
            log.warning(f"Futures search returned no instruments for {symbol}")
            return None

        # Filter to the FUTURES contract(s) for THIS underlying.
        # Correct Upstox response field names (this is what was broken):
        #   - instrument_type == "FUT"      (excludes EQ / CE / PE options)
        #   - underlying_symbol == name     (exact match, not a substring)
        #   - trading_symbol / lot_size      (note: underscores)
        nse_futures = [
            inst for inst in instruments
            if str(inst.get("instrument_type", "")).upper() == "FUT"
            and str(inst.get("underlying_symbol", "")).upper() == search_name.upper()
        ]

        if not nse_futures:
            log.warning(
                f"No FUT contract matched underlying '{search_name}' for {symbol}"
            )
            return None

        # Front-month = nearest expiry
        nse_futures.sort(key=lambda x: x.get("expiry", "9999-99-99"))
        active = nse_futures[0]

        # Store with the key names the rest of this module already expects
        # (tradingsymbol / lot_size / expiry / instrument_key).
        result = {
            "instrument_key": active.get("instrument_key", ""),
            "expiry":         active.get("expiry", ""),
            "tradingsymbol":  active.get("trading_symbol", ""),
            "lot_size":       active.get("lot_size") or get_lot_size(symbol),
            "date":           today,
        }

        # Save to both caches
        _futures_cache[symbol] = result
        _save_futures_to_db(symbol, result)

        log.info(
            f"Futures contract fetched: {symbol} → {result['tradingsymbol']} "
            f"(expiry: {result['expiry']})"
        )

        return result

    except requests.exceptions.Timeout:
        log.warning(f"Futures search timed out for {symbol}")
        return None
    except Exception as e:
        log.error(f"Futures contract lookup error for {symbol}: {e}")
        return None


def get_futures_price(
    instrument_key: str,
    token:          str,
    base_url:       str = "https://api.upstox.com/v2",
) -> Optional[float]:
    """Fetch latest price for a futures contract from Upstox."""
    _rate_limit()

    try:
        encoded_key = requests.utils.quote(instrument_key, safe="")
        url = f"{base_url}/market-quote/quotes?instrument_key={encoded_key}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        response = requests.get(
            url,
            headers=headers,
            timeout=8,  # FIXED: 8 second timeout
        )

        if response.status_code == 429:
            log.warning("Futures price rate limited")
            return None

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

    except requests.exceptions.Timeout:
        log.warning("Futures price fetch timed out")
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
        2. Basis > 0.5% (current testing threshold)

    Entry action:
        BUY spot shares + SELL futures contract simultaneously
        Profit is locked at entry = basis amount per share

    Exit: At futures expiry — prices converge — profit realised automatically
    """

    name = "Cash-Futures Arbitrage"
    description = (
        "Captures risk-free spread between NSE spot and futures price. "
        "Normal days: enters when basis > 0.5%. "
        "Expiry week (last 7 days): enters when basis > 0.5%. "
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
        """Full arbitrage signal with live futures price lookup."""

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

        futures_price = get_futures_price(instrument_key, token)

        if futures_price is None:
            return SignalResult(
                "HOLD", "WEAK",
                f"Could not fetch futures price for {contract.get('tradingsymbol','')}",
                strategy=self.name,
            )

        # Condition 1: Futures must be > Spot (contango)
        if futures_price <= spot_price:
            spread_pct = round((futures_price - spot_price) / spot_price * 100, 2)
            return SignalResult(
                "HOLD", "WEAK",
                f"Futures ₹{futures_price:,.2f} ≤ Spot ₹{spot_price:,.2f} "
                f"(backwardation: {spread_pct}%). No arbitrage.",
                {"Spot_Price": round(spot_price, 2),
                 "Futures_Price": round(futures_price, 2),
                 "Spread_Pct": spread_pct},
                self.name,
            )

        spread_abs = futures_price - spot_price
        spread_pct = round((spread_abs / spot_price) * 100, 2)

        threshold     = get_basis_threshold()
        in_exp_week   = is_expiry_week()
        days_to_exp   = days_to_expiry()
        expiry        = contract.get("expiry", "")
        tradingsymbol = contract.get("tradingsymbol", "")
        lot_size      = contract.get("lot_size", get_lot_size(symbol))

        gross_profit   = round(spread_abs * lot_size, 2)

        # Cost model: ~0.3% of position turnover (brokerage + STT + exchange
        # fees + GST + stamp duty, round-trip), per Jwala's transcript figure.
        # NOTE: the old model used lot_size * 3.5 (₹3.5/share), which invented
        # huge fake costs on large lots (e.g. GAIL 3500-share lot => ₹12,250
        # "cost", turning a real profit into a fake net loss). Turnover-based
        # cost is realistic and scales correctly with position size.
        COST_PCT       = 0.003  # 0.3% of turnover, round-trip
        turnover       = spot_price * lot_size
        cost_est       = round(turnover * COST_PCT, 2)
        net_profit     = round(gross_profit - cost_est, 2)
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

        if spread_pct > MAX_BASIS_PCT:
            return SignalResult(
                "HOLD", "WEAK",
                f"Basis {spread_pct}% exceeds {MAX_BASIS_PCT}% — possible data error",
                indicators, self.name,
            )

        if spread_pct >= threshold:
            strength   = "STRONG" if spread_pct >= threshold * 1.5 else "MODERATE"
            week_label = "EXPIRY WEEK" if in_exp_week else "NORMAL DAY"

            reason = (
                f"ARBITRAGE: Basis {spread_pct}% > {threshold}% threshold ({week_label})\n"
                f"Spot ₹{spot_price:,.2f} | Futures ₹{futures_price:,.2f} ({tradingsymbol})\n"
                f"BUY {lot_size} shares @ ₹{spot_price:,.2f} + SELL 1 lot @ ₹{futures_price:,.2f}\n"
                f"Gross ₹{gross_profit:,.0f} | Net ~₹{net_profit:,.0f} | "
                f"Expiry: {expiry} ({days_to_exp}d) | ~{annualised_pct}% p.a."
            )
            return SignalResult("BUY", strength, reason, indicators, self.name)

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