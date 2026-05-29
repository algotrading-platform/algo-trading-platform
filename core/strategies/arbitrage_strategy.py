# ============================================================
# core/strategies/arbitrage_strategy.py
#
# Cash-Futures Arbitrage Strategy
#
# Logic (from Jwala's document):
#   Spread % = (Futures Price − Spot Price) / Spot Price × 100
#   Trigger  → Spread % > 1.0% → BUY Spot + SELL Futures
#
# This strategy requires TWO data feeds:
#   1. Spot price  → from Upstox NSE_EQ or yfinance
#   2. Futures price → from Upstox NSE_FO active contract
#
# Contract management:
#   - Fetches active front-month futures contract from Upstox
#   - Handles monthly rollover automatically
#   - Stores active contract keys in Supabase (table: futures_contracts)
#
# Signal types:
#   BUY_SPOT    → Spread > 1% (futures at premium) → buy spot, sell futures
#   HOLD        → Spread within normal range
#   EXIT        → Spread collapsed to zero → exit position
# ============================================================

import os
import requests
import pandas as pd
import logging
from datetime import datetime, timedelta
from typing import Optional

import pytz

from core.strategies.base_strategy import BaseStrategy, SignalResult
from configs.universe import get_lot_size

log = logging.getLogger("arbitrage")
IST = pytz.timezone("Asia/Kolkata")

# Minimum spread % to trigger arbitrage
MIN_SPREAD_PCT = 1.0

# Maximum spread % — above this something is wrong (data error)
MAX_SPREAD_PCT = 5.0

# In-memory cache for futures contracts: {symbol: {key, expiry, fetched_at}}
_futures_cache: dict = {}


# ============================================================
# UPSTOX FUTURES CONTRACT LOOKUP
# ============================================================

def _get_upstox_token() -> Optional[str]:
    """Get Upstox token from Supabase."""
    try:
        from core.database.db import get_upstox_token
        return get_upstox_token()
    except Exception as e:
        log.warning(f"Could not get Upstox token: {e}")
        return None


def get_active_futures_contract(
    symbol:     str,
    token:      str,
    base_url:   str = "https://api.upstox.com/v2",
) -> Optional[dict]:
    """
    Fetch active (front-month) futures contract from Upstox.
    Returns dict with instrument_key and expiry, or None.
    """
    global _futures_cache

    # Check cache (valid for today)
    today = datetime.now(IST).strftime("%Y-%m-%d")
    if symbol in _futures_cache:
        cached = _futures_cache[symbol]
        if cached.get("date") == today:
            return cached

    # Strip .NS suffix for Upstox search
    search_name = symbol.replace(".NS", "")

    try:
        url = f"{base_url}/instruments/search"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        params = {
            "q":          search_name,
            "asset_type": "FO",
        }

        response = requests.get(url, headers=headers, params=params, timeout=10)

        if response.status_code != 200:
            log.warning(f"Futures search failed for {symbol}: {response.status_code}")
            return None

        data        = response.json()
        instruments = data.get("data", [])

        if not instruments:
            return None

        # Filter: NSE futures only, matching symbol
        nse_futures = [
            inst for inst in instruments
            if inst.get("exchange", "").upper() == "NSE"
            and inst.get("instrument_type", "").upper() in ("FUT", "FO")
            and search_name.upper() in inst.get("tradingsymbol", "").upper()
        ]

        if not nse_futures:
            return None

        # Sort by expiry — pick front month
        nse_futures.sort(key=lambda x: x.get("expiry", "9999-99-99"))
        active = nse_futures[0]

        result = {
            "instrument_key": active.get("instrument_key", ""),
            "expiry":         active.get("expiry", ""),
            "tradingsymbol":  active.get("tradingsymbol", ""),
            "lot_size":       active.get("lot_size", get_lot_size(symbol)),
            "date":           today,
        }

        # Cache it
        _futures_cache[symbol] = result
        log.info(f"Futures contract for {symbol}: {result['tradingsymbol']} "
                 f"(expiry: {result['expiry']})")

        return result

    except Exception as e:
        log.error(f"Futures contract lookup error for {symbol}: {e}")
        return None


def get_futures_price(
    instrument_key: str,
    token:          str,
    base_url:       str = "https://api.upstox.com/v2",
) -> Optional[float]:
    """
    Fetch latest price for a futures contract from Upstox.
    Uses intraday quote API for real-time price.
    """
    try:
        encoded_key = requests.utils.quote(instrument_key, safe="")
        url = f"{base_url}/market-quote/quotes?instrument_key={encoded_key}"

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

        response = requests.get(url, headers=headers, timeout=10)

        if response.status_code != 200:
            log.warning(f"Futures quote failed: {response.status_code}")
            return None

        data   = response.json()
        quotes = data.get("data", {})

        # Key format in response varies — find any value
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
    Cash-Futures Spread Arbitrage.

    Monitors the spread between NSE spot (cash) and futures prices.
    When futures trade at >1% premium to spot → risk-free arbitrage opportunity.

    Entry:
        BUY spot shares + SELL futures contract simultaneously
        Profit locked at entry = spread amount per share

    Exit:
        Prices converge at futures expiry → profit automatically realised

    Risk:
        Near-zero directional risk (market neutral)
        Risk = execution slippage + brokerage costs
    """

    name = "Cash-Futures Arbitrage"
    description = (
        "Identifies risk-free arbitrage when NSE futures trade at >1% premium "
        "to spot price. Entry: Buy spot + Sell futures simultaneously. "
        "Profit is locked at entry and realised at expiry when prices converge. "
        "Near-zero directional risk — purely a spread capture strategy."
    )

    MIN_SPREAD = MIN_SPREAD_PCT

    def generate_signal(self, df: pd.DataFrame) -> SignalResult:
        """
        Standard interface — returns HOLD since arbitrage needs
        live futures price. Use generate_arbitrage_signal() instead.
        """
        return SignalResult(
            "HOLD", "WEAK",
            "Use generate_arbitrage_signal() for live arbitrage scanning",
            strategy=self.name,
        )

    def generate_arbitrage_signal(
        self,
        symbol:      str,
        spot_price:  float,
        token:       str,
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

        # Get futures price
        futures_price = get_futures_price(instrument_key, token)

        if futures_price is None:
            return SignalResult(
                "HOLD", "WEAK",
                f"Could not fetch futures price for {contract.get('tradingsymbol','')}",
                strategy=self.name,
            )

        # Calculate spread
        spread_abs = futures_price - spot_price
        spread_pct = (spread_abs / spot_price) * 100

        lot_size      = contract.get("lot_size", get_lot_size(symbol))
        gross_profit  = round(spread_abs * lot_size, 2)
        brokerage_est = round(lot_size * 3.5, 2)  # ~₹3.5/share estimate
        net_profit    = round(gross_profit - brokerage_est, 2)
        expiry        = contract.get("expiry", "Unknown")
        tradingsymbol = contract.get("tradingsymbol", "")

        indicators = {
            "Spot_Price":    round(spot_price, 2),
            "Futures_Price": round(futures_price, 2),
            "Spread_Abs":    round(spread_abs, 2),
            "Spread_Pct":    round(spread_pct, 2),
            "Lot_Size":      lot_size,
            "Gross_Profit":  gross_profit,
            "Net_Profit_Est": net_profit,
            "Futures_Symbol": tradingsymbol,
            "Expiry":        expiry,
        }

        # Sanity check — spread too high = data error
        if spread_pct > MAX_SPREAD_PCT:
            return SignalResult(
                "HOLD", "WEAK",
                f"Spread {round(spread_pct,2)}% exceeds maximum threshold "
                f"— possible data error",
                indicators, self.name,
            )

        # Arbitrage opportunity
        if spread_pct >= self.MIN_SPREAD and spread_abs > 0:
            # Strength based on spread size
            if spread_pct >= 2.0:
                strength = "STRONG"
            elif spread_pct >= 1.5:
                strength = "MODERATE"
            else:
                strength = "MODERATE"

            reason = (
                f"ARBITRAGE OPPORTUNITY DETECTED\n"
                f"Spot: ₹{round(spot_price,2)} | Futures: ₹{round(futures_price,2)}\n"
                f"Spread: ₹{round(spread_abs,2)} per share ({round(spread_pct,2)}%)\n"
                f"Action: BUY {lot_size} shares @ ₹{round(spot_price,2)} "
                f"+ SELL 1 lot {tradingsymbol} @ ₹{round(futures_price,2)}\n"
                f"Gross profit: ₹{gross_profit} | Est. net: ₹{net_profit}\n"
                f"Profit locked at entry. Realised at expiry: {expiry}"
            )
            return SignalResult("BUY", strength, reason, indicators, self.name)

        # Futures at discount (backwardation) — no standard arbitrage
        if spread_pct < 0:
            return SignalResult(
                "HOLD", "WEAK",
                f"Futures in backwardation: futures ₹{round(futures_price,2)} "
                f"< spot ₹{round(spot_price,2)} "
                f"(spread: {round(spread_pct,2)}%). "
                f"Possible dividend pricing. No arbitrage.",
                indicators, self.name,
            )

        return SignalResult(
            "HOLD", "WEAK",
            f"Spread {round(spread_pct,2)}% below 1% threshold. "
            f"Normal contango pricing. No arbitrage opportunity.",
            indicators, self.name,
        )


# Singleton instance
arbitrage_strategy = ArbitrageStrategy()