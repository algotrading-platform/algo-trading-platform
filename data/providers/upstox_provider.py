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

# V2 API intervals (fallback)
UPSTOX_INTERVALS = {
    "5m":  "1minute",
    "15m": "30minute",
    "1h":  "day",
    "1d":  "day",
    "1wk": "week",
    "1mo": "month",
}

# V3 API intervals — supports 5min, 15min, 1hour properly
# Format: (unit, interval_number)
# URL: /v3/historical-candle/{instrument_key}/{unit}/{interval}/{to}/{from}
UPSTOX_INTERVALS_V3 = {
    "5m":  ("minutes", "5"),
    "15m": ("minutes", "15"),
    "1h":  ("hours",   "1"),
    "1d":  ("days",    "1"),
    "1wk": ("weeks",   "1"),
    "1mo": ("months",  "1"),
}

# ============================================================
# UPSTOX SYMBOL MAPPING
# yfinance symbol → Upstox instrument key
# Format: EXCHANGE|TRADING_SYMBOL
# ============================================================

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
    Fetch the current Upstox access token from Supabase.
    Returns None if not found or expired.
    """
    try:
        from core.database.db import get_client
        client = get_client()
        result = (
            client.table("upstox_tokens")
            .select("access_token, expires_at")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if not result.data:
            return None

        row = result.data[0]
        expires_at = datetime.fromisoformat(row["expires_at"])

        if expires_at.tzinfo is None:
            expires_at = IST.localize(expires_at)

        if datetime.now(IST) >= expires_at:
            return None

        return row["access_token"]

    except Exception as e:
        print(f"[Upstox] get_token error: {e}")
        return None


def save_token(access_token: str) -> bool:
    """Save access token to Supabase. Expires at 3:30 AM next day IST."""
    try:
        from core.database.db import get_client
        now = datetime.now(IST)
        # Token valid until 3:30 AM next day
        expires_at = (now + timedelta(days=1)).replace(
            hour=3, minute=30, second=0, microsecond=0
        )
        client = get_client()
        client.table("upstox_tokens").insert({
            "access_token": access_token,
            "created_at":   now.isoformat(),
            "expires_at":   expires_at.isoformat(),
        }).execute()
        return True
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

        # Map interval to Upstox format
        upstox_interval = UPSTOX_INTERVALS.get(interval)
        if not upstox_interval:
            return self._yf.fetch_data(symbol, interval, period)

        # Resolve instrument key
        upstox_sym = self._resolve_symbol(symbol, token)
        if not upstox_sym:
            print(f"[Upstox] No mapping for {symbol} — falling back to yfinance")
            return self._yf.fetch_data(symbol, interval, period)

        # Calculate date range
        to_date, from_date = self._period_to_dates(period)

        try:
            df = self._fetch_candles(
                token=token,
                instrument_key=upstox_sym,
                interval=upstox_interval,
                from_date=from_date,
                to_date=to_date,
            )

            if df is None or df.empty:
                print(f"[Upstox] Empty data for {symbol} — falling back to yfinance")
                return self._yf.fetch_data(symbol, interval, period)

            return df

        except Exception as e:
            print(f"[Upstox] fetch error {symbol}: {e} — falling back to yfinance")
            return self._yf.fetch_data(symbol, interval, period)

    def _resolve_symbol(self, symbol: str, token: str) -> str | None:
        """
        Resolve yfinance symbol to Upstox instrument key.
        For MCX commodities, fetches active contract dynamically.
        """
        # Check static mapping first (indexes + stocks)
        if symbol in UPSTOX_SYMBOL_MAP:
            return UPSTOX_SYMBOL_MAP[symbol]

        # MCX commodity — fetch active contract
        if symbol in MCX_COMMODITY_SEARCH:
            return self._get_mcx_contract(
                symbol=symbol,
                token=token,
                search_name=MCX_COMMODITY_SEARCH[symbol],
            )

        # Auto-resolve: try NSE_EQ|SYMBOL format for unknown .NS stocks
        if symbol.endswith(".NS"):
            base = symbol.replace(".NS", "")
            auto_key = f"NSE_EQ|{base}"
            return auto_key

        return None

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
        Call Upstox V3 historical candle API.
        V3 supports: 5minute, 15minute, 1hour, day, week, month
        API: /v3/historical-candle/{key}/{unit}/{interval}/{to}/{from}
        """
        encoded_key = requests.utils.quote(instrument_key, safe="")

        # Use V3 API with proper unit/interval format
        v3_mapping = UPSTOX_INTERVALS_V3.get(interval)
        if v3_mapping:
            unit, interval_num = v3_mapping
            url = (
                f"https://api.upstox.com/v3/historical-candle"
                f"/{encoded_key}/{unit}/{interval_num}/{to_date}/{from_date}"
            )
        else:
            # Fallback to V2 for unmapped intervals
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