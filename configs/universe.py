# ============================================================
# configs/universe.py
#
# NSE F&O Universe — dynamic fetch from NSE API
# Falls back to hardcoded verified list if API is down.
#
# Usage:
#   from configs.universe import get_fno_universe
#   symbols = get_fno_universe()  # returns list of .NS symbols
# ============================================================

import requests
import logging
from datetime import datetime, date
from typing import Optional

log = logging.getLogger("universe")

# ============================================================
# CACHE — refreshed once per day
# ============================================================
_cache: dict = {"symbols": [], "date": None}

# ============================================================
# HARDCODED FALLBACK — verified NSE F&O stocks
# Used when NSE API is unavailable
# ============================================================
FALLBACK_FNO_SYMBOLS = [
    # Banking & Finance
    "HDFCBANK.NS", "ICICIBANK.NS", "KOTAKBANK.NS", "AXISBANK.NS",
    "SBIN.NS", "INDUSINDBK.NS", "BAJFINANCE.NS", "BAJAJFINSV.NS",
    "MUTHOOTFIN.NS", "PNB.NS", "BANKBARODA.NS", "CANBK.NS",
    "FEDERALBNK.NS", "IDFCFIRSTB.NS", "RBLBANK.NS", "AUBANK.NS",
    "CHOLAFIN.NS", "M&MFIN.NS", "LICHSGFIN.NS", "MANAPPURAM.NS",
    "SHRIRAMFIN.NS", "ABCAPITAL.NS", "RECLTD.NS", "PFC.NS",
    "SBICARD.NS", "HDFCLIFE.NS", "SBILIFE.NS", "ICICIGI.NS",
    "ICICIPRULI.NS", "NAUKRI.NS",

    # IT
    "TCS.NS", "INFY.NS", "WIPRO.NS", "HCLTECH.NS", "TECHM.NS",
    "LTIM.NS", "MPHASIS.NS", "COFORGE.NS", "PERSISTENT.NS",
    "OFSS.NS", "KPIT.NS", "TATAELXSI.NS", "CYIENT.NS",

    # Energy & Power
    "RELIANCE.NS", "ONGC.NS", "BPCL.NS", "IOC.NS", "NTPC.NS",
    "POWERGRID.NS", "ADANIGREEN.NS", "ADANIPORTS.NS", "ADANIENT.NS",
    "TATAPOWER.NS", "TORNTPOWER.NS", "CESC.NS", "NHPC.NS",
    "SJVN.NS", "IREDA.NS", "GAIL.NS", "IGL.NS", "MGL.NS",
    "PETRONET.NS", "OIL.NS",

    # Auto
    "MARUTI.NS", "M&M.NS", "BAJAJ-AUTO.NS", "EICHERMOT.NS",
    "HEROMOTOCO.NS", "TATAMOTORS.NS", "ASHOKLEY.NS", "ESCORTS.NS",
    "BALKRISIND.NS", "MRF.NS", "APOLLOTYRE.NS", "CEATLTD.NS",
    "MOTHERSON.NS", "BOSCHLTD.NS", "BHARATFORG.NS", "SUNDRMFAST.NS",
    "TIINDIA.NS", "CRAFTSMAN.NS",

    # Pharma
    "SUNPHARMA.NS", "DRREDDY.NS", "DIVISLAB.NS", "CIPLA.NS",
    "APOLLOHOSP.NS", "LUPIN.NS", "AUROPHARMA.NS", "ALKEM.NS",
    "TORNTPHARM.NS", "BIOCON.NS", "ABBOTINDIA.NS", "PFIZER.NS",
    "GLAXO.NS", "IPCALAB.NS", "LALPATHLAB.NS", "METROPOLIS.NS",

    # FMCG
    "HINDUNILVR.NS", "ITC.NS", "NESTLEIND.NS", "BRITANNIA.NS",
    "DABUR.NS", "MARICO.NS", "GODREJCP.NS", "COLPAL.NS",
    "EMAMILTD.NS", "TATACONSUM.NS", "UBL.NS", "RADICO.NS",
    "VBL.NS", "MCDOWELL-N.NS",

    # Metals
    "TATASTEEL.NS", "JSWSTEEL.NS", "HINDALCO.NS", "COALINDIA.NS",
    "VEDL.NS", "SAIL.NS", "NATIONALUM.NS", "NMDC.NS",
    "HINDCOPPER.NS", "WELCORP.NS", "APL.NS",

    # Infra & Capital Goods
    "LT.NS", "SIEMENS.NS", "ABB.NS", "HAVELLS.NS",
    "VOLTAS.NS", "POLYCAB.NS", "KEI.NS", "CUMMINSIND.NS",
    "THERMAX.NS", "BEL.NS", "HAL.NS", "COCHINSHIP.NS",
    "GRINDWELL.NS", "AIAENG.NS",

    # Telecom & Media
    "BHARTIARTL.NS", "IDEA.NS", "TATACOMM.NS", "ZEEL.NS", "PVR.NS",

    # Cement
    "ULTRACEMCO.NS", "SHREECEM.NS", "AMBUJACEM.NS", "ACC.NS",
    "DALMIACEME.NS", "JKCEMENT.NS", "RAMCOCEM.NS", "HEIDELBERGCE.NS",

    # Real Estate
    "DLF.NS", "GODREJPROP.NS", "OBEROIRLTY.NS", "PRESTIGE.NS",
    "BRIGADE.NS", "SUNTECK.NS",

    # Chemicals
    "PIDILITIND.NS", "AAPL.NS", "DEEPAKNTR.NS", "ATUL.NS",
    "NAVINFLUOR.NS", "FINEORG.NS", "CLEAN.NS", "ALKYLAMINE.NS",

    # Consumer & Retail
    "TITAN.NS", "TRENT.NS", "DMART.NS", "NYKAA.NS",
    "ZOMATO.NS", "PAYTM.NS", "POLICYBZR.NS", "CARTRADE.NS",

    # Jwala original picks
    "CDSL.NS", "BSE.NS", "SYRMA.NS", "MCDOWELL-N.NS",
]

# Lot sizes for F&O stocks (shares per lot) — used in Arbitrage strategy
LOT_SIZES = {
    "HDFCBANK.NS": 550, "ICICIBANK.NS": 1375, "RELIANCE.NS": 250,
    "TCS.NS": 150, "INFY.NS": 300, "NIFTY": 25, "BANKNIFTY": 15,
    "SBIN.NS": 1500, "BAJFINANCE.NS": 125, "KOTAKBANK.NS": 400,
    "AXISBANK.NS": 1200, "WIPRO.NS": 1500, "HCLTECH.NS": 350,
    "TATAMOTORS.NS": 1425, "MARUTI.NS": 100, "SUNPHARMA.NS": 350,
    "ADANIPORTS.NS": 1250, "TATASTEEL.NS": 3375, "JSWSTEEL.NS": 675,
    "NTPC.NS": 3000, "POWERGRID.NS": 2700, "ONGC.NS": 1925,
    "BPCL.NS": 1800, "IOC.NS": 2625, "LT.NS": 300,
    "BHARTIARTL.NS": 475, "ITC.NS": 3200, "HINDUNILVR.NS": 300,
}

DEFAULT_LOT_SIZE = 500  # used when specific lot size unknown


def get_lot_size(symbol: str) -> int:
    """Returns lot size for a given symbol."""
    return LOT_SIZES.get(symbol, DEFAULT_LOT_SIZE)


# ============================================================
# DYNAMIC FETCH FROM NSE API
# ============================================================

def _fetch_from_nse() -> list[str]:
    """
    Fetch live F&O eligible stocks from NSE API.
    Returns list of .NS symbols.
    """
    try:
        session = requests.Session()
        # NSE requires a session cookie first
        session.get(
            "https://www.nseindia.com",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml",
            },
            timeout=10,
        )

        response = session.get(
            "https://www.nseindia.com/api/equity-stockIndices"
            "?index=SECURITIES%20IN%20F%26O",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json",
                "Referer": "https://www.nseindia.com/",
            },
            timeout=10,
        )

        if response.status_code != 200:
            log.warning(f"NSE API returned {response.status_code}")
            return []

        data = response.json()
        stocks = data.get("data", [])

        symbols = []
        for s in stocks:
            symbol = s.get("symbol", "")
            if symbol and symbol != "NIFTY 50":
                symbols.append(f"{symbol}.NS")

        log.info(f"NSE API: fetched {len(symbols)} F&O symbols")
        return symbols

    except Exception as e:
        log.warning(f"NSE API fetch failed: {e}")
        return []


def get_fno_universe(force_refresh: bool = False) -> list[str]:
    """
    Returns list of NSE F&O stock symbols in yfinance format.
    Fetches from NSE API once per day, caches result.
    Falls back to hardcoded list if API unavailable.
    """
    global _cache

    today = date.today()

    # Return cached if same day
    if not force_refresh and _cache["date"] == today and _cache["symbols"]:
        return _cache["symbols"]

    # Try NSE API
    symbols = _fetch_from_nse()

    if symbols:
        _cache = {"symbols": symbols, "date": today}
        return symbols

    # Fallback to hardcoded list
    log.warning("Using hardcoded F&O fallback list")
    _cache = {"symbols": FALLBACK_FNO_SYMBOLS, "date": today}
    return FALLBACK_FNO_SYMBOLS


def get_all_instruments_extended() -> list[dict]:
    """
    Returns full instrument list including Indexes + all F&O stocks + Commodities.
    Used by the scheduler when running expanded universe.
    """
    from configs.instruments import (
        INDEXES, INDEXES_DISPLAY, INDEXES_TV,
        COMMODITIES, COMMODITIES_DISPLAY, COMMODITIES_TV,
        stock_display,
    )

    instruments = []

    # Indexes
    for sym in INDEXES:
        instruments.append({
            "symbol":   sym,
            "name":     INDEXES_DISPLAY.get(sym, sym),
            "tv":       INDEXES_TV.get(sym, sym),
            "category": "INDEX",
        })

    # All F&O stocks
    fno = get_fno_universe()
    for sym in fno:
        instruments.append({
            "symbol":   sym,
            "name":     stock_display(sym),
            "tv":       f"NSE:{stock_display(sym)}",
            "category": "STOCK",
        })

    # Commodities
    for sym in COMMODITIES:
        instruments.append({
            "symbol":   sym,
            "name":     COMMODITIES_DISPLAY.get(sym, sym),
            "tv":       COMMODITIES_TV.get(sym, sym),
            "category": "COMMODITY",
        })

    return instruments