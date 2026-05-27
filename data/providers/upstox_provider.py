# ============================================================
# data/providers/upstox_provider.py
#
# Upstox API data provider — replaces YFinanceProvider
# for intraday timeframes (5m, 15m, 1h).
#
# Uses historical candle API — accurate IST timestamps,
# no data gaps, real-time prices during market hours.
#
# Token management:
#   - Access token stored in Supabase (table: upstox_tokens)
#   - Token generated daily via scripts/upstox_login.py
#   - Falls back to YFinanceProvider if token missing/expired
#
# Upstox symbol format:
#   NSE stocks : NSE_EQ|INE002A01018  (requires ISIN)
#   Indexes    : NSE_INDEX|Nifty 50
#   Commodities: MCX_FO|...
#
# Since ISIN mapping is complex, we use a hybrid approach:
#   - Upstox for indexes (Nifty 50, Bank Nifty, Sensex)
#   - Upstox for top liquid stocks via symbol mapping
#   - YFinance fallback for anything not in mapping
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

UPSTOX_INTERVALS = {
    "5m":  "5minute",
    "15m": "30minute",   # Upstox uses 30minute for 15m equivalent
    "1h":  "1hour",
    "1d":  "1day",
    "1wk": "1week",
    "1mo": "1month",
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

# Commodities not supported via Upstox historical API in this tier
# Fall back to yfinance for GC=F, SI=F, HG=F, CL=F
UPSTOX_UNSUPPORTED = {"GC=F", "SI=F", "HG=F", "CL=F"}

# Timeframes that use yfinance (daily/weekly — no accuracy issue)
YFINANCE_TIMEFRAMES = {"1d", "1wk", "1mo"}


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
    Fetches OHLCV data from Upstox historical candle API.
    Falls back to YFinanceProvider when:
      - No valid token
      - Symbol not in mapping (commodities)
      - Daily/Weekly/Monthly timeframes
      - Any API error
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

        # Use yfinance for daily/weekly/monthly — no accuracy issue
        if interval in YFINANCE_TIMEFRAMES:
            return self._yf.fetch_data(symbol, interval, period)

        # Use yfinance for unsupported symbols (commodities)
        if symbol in UPSTOX_UNSUPPORTED:
            return self._yf.fetch_data(symbol, interval, period)

        # Check symbol mapping
        upstox_sym = UPSTOX_SYMBOL_MAP.get(symbol)
        if not upstox_sym:
            return self._yf.fetch_data(symbol, interval, period)

        # Get token
        token = get_token()
        if not token:
            print(f"[Upstox] No valid token — falling back to yfinance for {symbol}")
            return self._yf.fetch_data(symbol, interval, period)

        # Map interval
        upstox_interval = UPSTOX_INTERVALS.get(interval)
        if not upstox_interval:
            return self._yf.fetch_data(symbol, interval, period)

        # Calculate date range from period
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
                return self._yf.fetch_data(symbol, interval, period)

            return df

        except Exception as e:
            print(f"[Upstox] fetch error {symbol}: {e} — falling back to yfinance")
            return self._yf.fetch_data(symbol, interval, period)

    def _fetch_candles(
        self,
        token:          str,
        instrument_key: str,
        interval:       str,
        from_date:      str,
        to_date:        str,
    ) -> pd.DataFrame:
        """
        Call Upstox historical candle API and return clean DataFrame.
        API docs: https://upstox.com/developer/api-documentation/historical-candle-data
        """
        # URL encode the instrument key
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