import logging
log = logging.getLogger("upstox_provider")
# ============================================================
# data/providers/upstox_provider.py
#
# Upstox API data provider — PRIMARY data source for ALL
# instruments and ALL timeframes.
#
# Uses historical candle API — accurate IST timestamps,
# no data gaps, real-time prices during market hours.
#
# Data source priority:
#   1. Upstox API (all instruments, all timeframes)
#   2. yfinance (fallback ONLY when Upstox is down/token missing)
#
# Token management:
#   - Access token stored in Supabase (table: upstox_tokens)
#   - Token generated daily via scripts/upstox_login.py
#   - Falls back to YFinanceProvider if token missing/expired
#
# Commodity support:
#   - MCX contracts fetched dynamically from Upstox instruments API
#   - Auto-detects active contract for Gold, Silver, Copper, Crude
#   - Contract keys cached in memory to avoid repeated API calls
#   - Falls back to yfinance if MCX contract lookup fails
# ============================================================

import os
import requests
import pandas as pd
from datetime import datetime, timedelta
import pytz

from data.providers.base_provider import BaseDataProvider
from data.providers.yfinance_provider import YFinanceProvider

IST = pytz.timezone("Asia/Kolkata")

# ============================================================
# UPSTOX INTERVAL MAPPING
# yfinance interval → Upstox interval
# ============================================================

# Upstox V2 fetch strategy:
#
#   5m  → fetch "1minute"  for 5d  → resample to  5-min OHLCV
#   15m → fetch "1minute"  for 5d  → resample to 15-min OHLCV
#   1h  → fetch "30minute" for 15d → resample to  1-hr  OHLCV
#   1d  → fetch "day"      directly ✅
#   1wk → fetch "week"     directly ✅
#   1mo → fetch "month"    directly ✅
#
# This gives 100% Upstox NSE data — same candle boundaries as TradingView.

UPSTOX_FETCH_MAP = {
    #  interval  fetch_interval  resample_rule  fetch_period
    "5m":  ("1minute",   "5min",   "5d"),
    "15m": ("1minute",   "15min",  "5d"),
    "1h":  ("30minute",  "1h",     "15d"),
    "1d":  ("day",       None,     None),
    "1wk": ("week",      None,     None),
    "1mo": ("month",     None,     None),
}

# Keep for backward compat
UPSTOX_INTERVALS = {
    "1d":  "day",
    "1wk": "week",
    "1mo": "month",
}

# ============================================================
# UPSTOX INSTRUMENT KEY RESOLUTION
#
# Upstox uses ISIN-based instrument keys (since 2024):
#   NSE_EQ|INE040A01034  (not NSE_EQ|HDFCBANK)
#
# We download Upstox's instruments file once at startup
# and build a symbol → instrument_key map dynamically.
# ============================================================

import gzip
from io import BytesIO
import json

_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
_MCX_URL         = "https://assets.upstox.com/market-quote/instruments/exchange/MCX.json.gz"

# Runtime cache: yfinance_symbol → upstox instrument_key
_symbol_key_cache: dict = {}
_instruments_loaded  = False


def _load_instruments() -> None:
    """
    Download Upstox instruments file and build symbol→key map.
    Called once at startup. Cached in memory.
    """
    global _symbol_key_cache, _instruments_loaded

    if _instruments_loaded:
        return

    print("[Upstox] Downloading instruments file...", flush=True)
    try:
        r = requests.get(_INSTRUMENTS_URL, timeout=30)
        print(f"[Upstox] Instruments file status: {r.status_code}", flush=True)

        if r.status_code != 200:
            print(f"[Upstox] Instruments file failed: {r.status_code}", flush=True)
            _instruments_loaded = True
            return

        print(f"[Upstox] Parsing {len(r.content)} bytes...", flush=True)
        with gzip.GzipFile(fileobj=BytesIO(r.content)) as gz:
            instruments = json.load(gz)

        print(f"[Upstox] Total instruments: {len(instruments)}", flush=True)

        count = 0
        for inst in instruments:
            # Handle both possible field name formats in Upstox JSON
            sym   = inst.get("tradingsymbol") or inst.get("trading_symbol") or ""
            key   = inst.get("instrument_key") or inst.get("key") or ""
            seg   = inst.get("segment") or inst.get("exchange_segment") or ""
            itype = inst.get("instrument_type") or inst.get("type") or ""

            if not sym or not key:
                continue

            # NSE equity stocks
            if seg == "NSE_EQ" and itype == "EQ":
                yf_sym = f"{sym}.NS"
                _symbol_key_cache[yf_sym] = key
                count += 1

            # NSE indices — match from instrument_key since tradingsymbol is empty
            if seg == "NSE_INDEX":
                if key == "NSE_INDEX|Nifty 50":
                    _symbol_key_cache["^NSEI"]    = key
                elif key == "NSE_INDEX|Nifty Bank":
                    _symbol_key_cache["^NSEBANK"] = key
                elif key == "NSE_INDEX|Nifty 100":
                    _symbol_key_cache["^NSEI100"] = key

        print(f"[Upstox] ✅ Loaded {count} NSE instruments into cache", flush=True)
        print(f"[Upstox] Sample: HDFCBANK.NS → {_symbol_key_cache.get('HDFCBANK.NS', 'NOT FOUND')}", flush=True)
        _instruments_loaded = True

    except Exception as e:
        print(f"[Upstox] ❌ Load instruments error: {type(e).__name__}: {e}", flush=True)
        import traceback
        traceback.print_exc()
        _instruments_loaded = True


def get_instrument_key(yf_symbol: str) -> str | None:
    """Get Upstox instrument key for a yfinance symbol."""
    _load_instruments()
    return _symbol_key_cache.get(yf_symbol)

UPSTOX_SYMBOL_MAP = {
    # Indexes
    "^NSEI":    "NSE_INDEX|Nifty 50",
    "^NSEBANK": "NSE_INDEX|Nifty Bank",
    "^BSESN":   "BSE_INDEX|SENSEX",

    # Banking & Finance
    "HDFCBANK.NS":  "NSE_EQ|HDFCBANK",
    "ICICIBANK.NS": "NSE_EQ|ICICIBANK",
    "KOTAKBANK.NS": "NSE_EQ|KOTAKBANK",
    "AXISBANK.NS":  "NSE_EQ|AXISBANK",
    "SBIN.NS":      "NSE_EQ|SBIN",
    "INDUSINDBK.NS":"NSE_EQ|INDUSINDBK",
    "BAJFINANCE.NS":"NSE_EQ|BAJFINANCE",
    "BAJAJFINSV.NS":"NSE_EQ|BAJAJFINSV",
    "MUTHOOTFIN.NS":"NSE_EQ|MUTHOOTFIN",
    "PNB.NS":       "NSE_EQ|PNB",

    # IT
    "TCS.NS":     "NSE_EQ|TCS",
    "INFY.NS":    "NSE_EQ|INFY",
    "WIPRO.NS":   "NSE_EQ|WIPRO",
    "HCLTECH.NS": "NSE_EQ|HCLTECH",
    "TECHM.NS":   "NSE_EQ|TECHM",

    # Energy & Power
    "RELIANCE.NS":  "NSE_EQ|RELIANCE",
    "ONGC.NS":      "NSE_EQ|ONGC",
    "BPCL.NS":      "NSE_EQ|BPCL",
    "IOC.NS":       "NSE_EQ|IOC",
    "NTPC.NS":      "NSE_EQ|NTPC",
    "POWERGRID.NS": "NSE_EQ|POWERGRID",

    # Auto
    "MARUTI.NS":    "NSE_EQ|MARUTI",
    "M&M.NS":       "NSE_EQ|M&M",
    "BAJAJ-AUTO.NS":"NSE_EQ|BAJAJ-AUTO",
    "EICHERMOT.NS": "NSE_EQ|EICHERMOT",
    "HEROMOTOCO.NS":"NSE_EQ|HEROMOTOCO",

    # Pharma
    "SUNPHARMA.NS": "NSE_EQ|SUNPHARMA",
    "DRREDDY.NS":   "NSE_EQ|DRREDDY",
    "DIVISLAB.NS":  "NSE_EQ|DIVISLAB",
    "CIPLA.NS":     "NSE_EQ|CIPLA",
    "APOLLOHOSP.NS":"NSE_EQ|APOLLOHOSP",

    # FMCG
    "HINDUNILVR.NS":"NSE_EQ|HINDUNILVR",
    "ITC.NS":       "NSE_EQ|ITC",
    "NESTLEIND.NS": "NSE_EQ|NESTLEIND",
    "BRITANNIA.NS": "NSE_EQ|BRITANNIA",

    # Metals
    "TATASTEEL.NS": "NSE_EQ|TATASTEEL",
    "JSWSTEEL.NS":  "NSE_EQ|JSWSTEEL",
    "HINDALCO.NS":  "NSE_EQ|HINDALCO",
    "COALINDIA.NS": "NSE_EQ|COALINDIA",
    "VEDL.NS":      "NSE_EQ|VEDL",

    # Infra
    "LT.NS":      "NSE_EQ|LT",
    "SIEMENS.NS": "NSE_EQ|SIEMENS",

    # Telecom
    "BHARTIARTL.NS":"NSE_EQ|BHARTIARTL",

    # Adani
    "ADANIPORTS.NS": "NSE_EQ|ADANIPORTS",
    "ADANIGREEN.NS": "NSE_EQ|ADANIGREEN",

    # Jwala picks
    "CDSL.NS":  "NSE_EQ|CDSL",
    "BSE.NS":   "NSE_EQ|BSE",
    "SYRMA.NS": "NSE_EQ|SYRMA",
}

# Commodities — yfinance symbols mapped to MCX search names
# Active contract fetched dynamically from Upstox instruments API
MCX_COMMODITY_SEARCH = {
    "GC=F": "GOLD",
    "SI=F": "SILVER",
    "HG=F": "COPPER",
    "CL=F": "CRUDEOIL",
}

# In-memory cache for active MCX contract keys
# Refreshed once per day at scheduler startup
_mcx_contract_cache: dict[str, str] = {}
_mcx_cache_date: str = ""


# ============================================================
# TOKEN MANAGEMENT
# ============================================================

def get_token() -> str | None:
    """
    Fetch the current Upstox access token from Azure PostgreSQL.
    Returns None if not found or expired.
    """
    try:
        from core.database.db import get_upstox_token
        return get_upstox_token()
    except Exception as e:
        print(f"[Upstox] get_token error: {e}")
        return None

def save_token(access_token: str) -> bool:
    """Save access token to Azure PostgreSQL. Expires at 3:30 AM next day IST."""
    try:
        from core.database.db import save_upstox_token
        return save_upstox_token(access_token)
    except Exception as e:
        print(f"[Upstox] save_token error: {e}")
        return False

# ============================================================
# UPSTOX PROVIDER
# ============================================================

class UpstoxProvider(BaseDataProvider):
    """
    Fetches OHLCV data from Upstox API for ALL instruments
    and ALL timeframes. yfinance used ONLY as fallback when:
      - No valid token in Supabase
      - Upstox API returns error
      - MCX contract lookup fails
    """

    def __init__(self):
        self._yf = YFinanceProvider()
        self._base_url = "https://api.upstox.com/v2"

    def fetch_data(
        self,
        symbol: str,
        interval: str = "1h",
        period: str   = "1mo",
    ) -> pd.DataFrame:

        # Get token — if missing fall back to yfinance immediately
        token = get_token()
        if not token:
            print(f"[Upstox] No valid token — falling back to yfinance for {symbol}")
            return self._yf.fetch_data(symbol, interval, period)

        # Get fetch strategy for this interval
        fetch_config = UPSTOX_FETCH_MAP.get(interval)
        if not fetch_config:
            return self._yf.fetch_data(symbol, interval, period)

        fetch_interval, resample_rule, fetch_period_override = fetch_config
        effective_period = fetch_period_override if fetch_period_override else period

        # Resolve instrument key
        upstox_sym = self._resolve_symbol(symbol, token)
        if not upstox_sym:
            print(f"[Upstox] No mapping for {symbol} — falling back to yfinance")
            return self._yf.fetch_data(symbol, interval, period)

        # Calculate date range
        to_date, from_date = self._period_to_dates(effective_period)

        try:
            df = self._fetch_candles(
                token=token,
                instrument_key=upstox_sym,
                interval=fetch_interval,
                from_date=from_date,
                to_date=to_date,
            )

            if df is None or df.empty:
                print(f"[Upstox] Empty data for {symbol} — falling back to yfinance")
                return self._yf.fetch_data(symbol, interval, period)

            # Resample if needed (5m, 15m, 1h)
            if resample_rule:
                df = self._resample_candles(df, resample_rule)

            if df.empty:
                return self._yf.fetch_data(symbol, interval, period)

            return df

        except Exception as e:
            print(f"[Upstox] fetch error {symbol}: {e} — falling back to yfinance")
            return self._yf.fetch_data(symbol, interval, period)

    def _resample_candles(self, df: pd.DataFrame, rule: str) -> pd.DataFrame:
        """
        Resample 1-minute or 30-minute candles into larger timeframes.
        rule: "5min", "15min", "1h"

        This gives us true 5m, 15m, 1h candles from Upstox data —
        same candle boundaries as TradingView and Zerodha.
        """
        try:
            if "Datetime" not in df.columns:
                return df

            df = df.copy()
            df["Datetime"] = pd.to_datetime(df["Datetime"])
            df = df.set_index("Datetime")

            # Resample OHLCV
            resampled = df.resample(rule, label="left", closed="left").agg({
                "Open":   "first",
                "High":   "max",
                "Low":    "min",
                "Close":  "last",
                "Volume": "sum",
            }).dropna(subset=["Close"])

            # Keep only market hours candles (9:15 AM to 3:30 PM IST)
            resampled = resampled.between_time("09:15", "15:30")
            resampled = resampled.reset_index()
            resampled = resampled.sort_values("Datetime").reset_index(drop=True)

            return resampled

        except Exception as e:
            print(f"[Upstox] Resample error: {e}")
            return df.reset_index() if df.index.name == "Datetime" else df

    def _resolve_symbol(self, symbol: str, token: str) -> str | None:
        """
        Resolve yfinance symbol to Upstox instrument key.
        Uses the instruments file downloaded at startup.
        Key format: NSE_EQ|{ISIN} (e.g. NSE_EQ|INE040A01034)
        """
        # Use dynamic instruments file (ISIN-based keys)
        key = get_instrument_key(symbol)
        if key:
            return key

        # MCX commodities
        if symbol in ("GC=F", "SI=F", "HG=F", "CL=F"):
            return None  # Fall back to yfinance for MCX

        return None

    
    def _search_instrument(self, symbol: str, token: str) -> str | None:
        """
        Search Upstox instruments API to find correct instrument key.
        Caches results to avoid repeated API calls.
        """
        base = symbol.replace(".NS", "")

        # Check cache
        cache_key = f"NSE_{base}"
        if cache_key in _mcx_contract_cache:
            return _mcx_contract_cache[cache_key]

        try:
            url = "https://api.upstox.com/v2/instruments/search"
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }
            params = {"q": base, "asset_type": "EQUITY"}

            response = requests.get(url, headers=headers, params=params, timeout=8)
            if response.status_code != 200:
                # Fallback: try direct NSE_EQ format
                _mcx_contract_cache[cache_key] = f"NSE_EQ|{base}"
                return f"NSE_EQ|{base}"

            data = response.json()
            instruments = data.get("data", [])

            # Find best match — NSE equity with exact symbol match
            for inst in instruments:
                if (inst.get("exchange", "").upper() == "NSE"
                    and inst.get("instrument_type", "").upper() in ("EQ", "EQUITY")
                    and inst.get("tradingsymbol", "").upper() == base.upper()):
                    key = inst.get("instrument_key", f"NSE_EQ|{base}")
                    _mcx_contract_cache[cache_key] = key
                    return key

            # No exact match — try first NSE result
            for inst in instruments:
                if inst.get("exchange", "").upper() == "NSE":
                    key = inst.get("instrument_key", f"NSE_EQ|{base}")
                    _mcx_contract_cache[cache_key] = key
                    return key

            # Final fallback
            _mcx_contract_cache[cache_key] = f"NSE_EQ|{base}"
            return f"NSE_EQ|{base}"

        except Exception as e:
            return f"NSE_EQ|{base}"

    def _get_mcx_contract(
        self,
        symbol: str,
        token: str,
        search_name: str,
    ) -> str | None:
        """
        Fetch the active MCX futures contract key from Upstox.
        Uses in-memory cache — refreshed once per day.

        Upstox instruments search API returns active contracts.
        We pick the nearest expiry (front month contract).
        """
        global _mcx_contract_cache, _mcx_cache_date

        today = datetime.now(IST).strftime("%Y-%m-%d")

        # Return cached value if still valid today
        if _mcx_cache_date == today and symbol in _mcx_contract_cache:
            return _mcx_contract_cache[symbol]

        try:
            url = f"{self._base_url}/instruments/search"
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }
            params = {
                "q": search_name,
                "asset_type": "FO",
            }

            response = requests.get(
                url, headers=headers, params=params, timeout=10
            )

            if response.status_code != 200:
                print(f"[Upstox] MCX search failed for {search_name}: {response.status_code}")
                return None

            data = response.json()
            instruments = data.get("data", [])

            if not instruments:
                print(f"[Upstox] No MCX contracts found for {search_name}")
                return None

            # Filter MCX futures only
            mcx_futures = [
                inst for inst in instruments
                if inst.get("exchange", "").upper() == "MCX"
                and inst.get("instrument_type", "").upper() in ("FUT", "FO")
            ]

            if not mcx_futures:
                print(f"[Upstox] No MCX futures found for {search_name}")
                return None

            # Sort by expiry — pick nearest (front month)
            mcx_futures.sort(key=lambda x: x.get("expiry", "9999-99-99"))
            active = mcx_futures[0]
            instrument_key = active.get("instrument_key", "")

            if not instrument_key:
                return None

            # Cache it
            _mcx_contract_cache[symbol] = instrument_key
            _mcx_cache_date = today

            print(f"[Upstox] MCX contract for {search_name}: {instrument_key} "
                  f"(expiry: {active.get('expiry', 'unknown')})")

            return instrument_key

        except Exception as e:
            print(f"[Upstox] MCX contract lookup error for {search_name}: {e}")
            return None

    def _fetch_candles(
        self,
        token:          str,
        instrument_key: str,
        interval:       str,
        from_date:      str,
        to_date:        str,
    ) -> pd.DataFrame:
        """
        Call Upstox V2 historical candle API.
        V2 supports: 1minute, 30minute, day, week, month
        Intraday (5m, 15m, 1h) falls back to yfinance automatically.
        """
        encoded_key = requests.utils.quote(instrument_key, safe="")

        url = (
            f"{self._base_url}/historical-candle"
            f"/{encoded_key}/{interval}/{to_date}/{from_date}"
        )

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

        response = requests.get(url, headers=headers, timeout=10)

        if response.status_code == 401:
            print("[Upstox] Token expired or invalid")
            return pd.DataFrame()

        if response.status_code != 200:
            print(f"[Upstox] API error {response.status_code}: {response.text[:200]}")
            return pd.DataFrame()

        data = response.json()

        if data.get("status") != "success":
            return pd.DataFrame()

        candles = data.get("data", {}).get("candles", [])

        if not candles:
            return pd.DataFrame()

        # Upstox candle format:
        # [timestamp, open, high, low, close, volume, oi]
        df = pd.DataFrame(candles, columns=[
            "Datetime", "Open", "High", "Low", "Close", "Volume", "OI"
        ])

        # Convert timestamp to IST datetime
        df["Datetime"] = pd.to_datetime(df["Datetime"], utc=True).dt.tz_convert(IST)

        # Sort ascending (Upstox returns newest first)
        df = df.sort_values("Datetime").reset_index(drop=True)

        # Ensure numeric types
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df.dropna(subset=["Close"], inplace=True)

        return df

    def _period_to_dates(self, period: str) -> tuple[str, str]:
        """
        Convert yfinance period string to Upstox from/to date strings.
        Returns (to_date, from_date) in YYYY-MM-DD format.
        """
        now = datetime.now(IST)
        to_date = now.strftime("%Y-%m-%d")

        period_map = {
            "1d":  timedelta(days=1),
            "5d":  timedelta(days=5),
            "1mo": timedelta(days=30),
            "3mo": timedelta(days=90),
            "6mo": timedelta(days=180),
            "1y":  timedelta(days=365),
            "2y":  timedelta(days=730),
            "5y":  timedelta(days=1825),
        }

        delta = period_map.get(period, timedelta(days=30))
        from_date = (now - delta).strftime("%Y-%m-%d")

        return to_date, from_date