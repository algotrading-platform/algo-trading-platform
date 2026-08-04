# ============================================================
# configs/universe.py
#
# NSE F&O Universe — dynamic fetch from NSE API
# Falls back to hardcoded verified list if API is down.
#
# Fixes applied (2026-06-17):
#   REMOVED: LTIM.NS (404), KPIT.NS (404), TATAMOTORS.NS (404)
#   REMOVED: APL.NS (delisted), PVR.NS (merged with INOX → INOXCINE.NS)
#   REMOVED: MCDOWELL-N.NS (symbol changed → UNITDSPR.NS)
#   REMOVED: DALMIACEME.NS (delisted), HEIDELBERGCE.NS (delisted)
#   REMOVED: AAPL.NS (Apple US stock — wrong exchange)
#   FIXED:   ZOMATO.NS → ETERNAL.NS
#   ADDED:   LTIMINDTREE.NS (replaced LTIM.NS)
#   ADDED:   TATAMOTOR.NS replaced by TATAMOTORS.NS where valid
# ============================================================

import requests
import logging
import gzip
import json
import csv
from io import BytesIO
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
# All symbols verified as active on NSE as of June 2026
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
    "LTIMINDTREE.NS", "MPHASIS.NS", "COFORGE.NS", "PERSISTENT.NS",
    "OFSS.NS", "TATAELXSI.NS", "CYIENT.NS",

    # Energy & Power
    "RELIANCE.NS", "ONGC.NS", "BPCL.NS", "IOC.NS", "NTPC.NS",
    "POWERGRID.NS", "ADANIGREEN.NS", "ADANIPORTS.NS", "ADANIENT.NS",
    "TATAPOWER.NS", "TORNTPOWER.NS", "CESC.NS", "NHPC.NS",
    "SJVN.NS", "IREDA.NS", "GAIL.NS", "IGL.NS", "MGL.NS",
    "PETRONET.NS", "OIL.NS",

    # Auto
    "MARUTI.NS", "M&M.NS", "BAJAJ-AUTO.NS", "EICHERMOT.NS",
    "HEROMOTOCO.NS", "ASHOKLEY.NS", "ESCORTS.NS",
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
    "VBL.NS", "UNITDSPR.NS",

    # Metals
    "TATASTEEL.NS", "JSWSTEEL.NS", "HINDALCO.NS", "COALINDIA.NS",
    "VEDL.NS", "SAIL.NS", "NATIONALUM.NS", "NMDC.NS",
    "HINDCOPPER.NS", "WELCORP.NS",

    # Infra & Capital Goods
    "LT.NS", "SIEMENS.NS", "ABB.NS", "HAVELLS.NS",
    "VOLTAS.NS", "POLYCAB.NS", "KEI.NS", "CUMMINSIND.NS",
    "THERMAX.NS", "BEL.NS", "HAL.NS", "COCHINSHIP.NS",
    "GRINDWELL.NS", "AIAENG.NS",

    # Telecom & Media
    "BHARTIARTL.NS", "IDEA.NS", "TATACOMM.NS", "ZEEL.NS",

    # Cement
    "ULTRACEMCO.NS", "SHREECEM.NS", "AMBUJACEM.NS", "ACC.NS",
    "JKCEMENT.NS", "RAMCOCEM.NS",

    # Real Estate
    "DLF.NS", "GODREJPROP.NS", "OBEROIRLTY.NS", "PRESTIGE.NS",
    "BRIGADE.NS", "SUNTECK.NS",

    # Chemicals
    "PIDILITIND.NS", "DEEPAKNTR.NS", "ATUL.NS",
    "NAVINFLUOR.NS", "FINEORG.NS", "CLEAN.NS", "ALKYLAMINE.NS",

    # Consumer & Retail
    "TITAN.NS", "TRENT.NS", "DMART.NS", "NYKAA.NS",
    "ETERNAL.NS", "PAYTM.NS", "POLICYBZR.NS", "CARTRADE.NS",

    # Jwala original picks
    "CDSL.NS", "BSE.NS", "SYRMA.NS",
]

# Lot sizes for F&O stocks (shares per lot) — used in Arbitrage strategy
# NOTE: these are static fallback defaults only. get_fno_universe() overwrites
# this dict in-place with live lot sizes from Upstox's instrument master
# whenever that fetch succeeds, since NSE revises lot sizes periodically.
LOT_SIZES = {
    "HDFCBANK.NS": 550, "ICICIBANK.NS": 1375, "RELIANCE.NS": 250,
    "TCS.NS": 150, "INFY.NS": 300, "NIFTY": 65, "BANKNIFTY": 30,
    "SBIN.NS": 1500, "BAJFINANCE.NS": 125, "KOTAKBANK.NS": 400,
    "AXISBANK.NS": 1200, "WIPRO.NS": 1500, "HCLTECH.NS": 350,
    "MARUTI.NS": 100, "SUNPHARMA.NS": 350,
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
# DYNAMIC FETCH FROM UPSTOX INSTRUMENT MASTER (primary source)
#
# NSE's own website blocks datacenter/cloud IPs at the Akamai layer
# (homepage itself returns 403; the API endpoint serves a fake 404
# "Resource not found" page as an anti-bot response) — no request
# header tweaking fixes that from a hosted environment. Upstox
# publishes a public instrument dump (same file used by
# data/providers/upstox_provider.py for market data) that lists every
# F&O futures contract with its underlying stock symbol and current
# lot size, so we derive the F&O universe + live lot sizes from that
# instead of scraping NSE.
# ============================================================

_UPSTOX_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"


def _fetch_fno_from_upstox() -> tuple[list[str], dict[str, int]]:
    """
    Derive F&O-eligible stock symbols + their current lot sizes from
    Upstox's public instrument master. Returns ([], {}) on any failure
    so callers can fall through to the next source.
    """
    try:
        response = requests.get(_UPSTOX_INSTRUMENTS_URL, timeout=30)
        if response.status_code != 200:
            log.warning(f"Upstox instruments file returned {response.status_code}")
            return [], {}

        with gzip.GzipFile(fileobj=BytesIO(response.content)) as gz:
            instruments = json.load(gz)

        symbols: list[str] = []
        lots: dict[str, int] = {}
        seen: set[str] = set()

        for inst in instruments:
            if (inst.get("segment") == "NSE_FO"
                    and inst.get("instrument_type") == "FUT"
                    and inst.get("underlying_type") == "EQUITY"):
                sym = inst.get("underlying_symbol", "")
                if sym and sym not in seen:
                    seen.add(sym)
                    yf_sym = f"{sym}.NS"
                    symbols.append(yf_sym)
                    lots[yf_sym] = inst.get("lot_size", DEFAULT_LOT_SIZE)

            elif (inst.get("segment") == "NSE_FO"
                    and inst.get("instrument_type") == "FUT"
                    and inst.get("underlying_type") == "INDEX"
                    and inst.get("underlying_symbol") in ("NIFTY", "BANKNIFTY")):
                lots[inst["underlying_symbol"]] = inst.get("lot_size", DEFAULT_LOT_SIZE)

        if symbols:
            log.info(f"Upstox instruments: derived {len(symbols)} F&O stock symbols")
        return symbols, lots

    except Exception as e:
        log.warning(f"Upstox F&O universe fetch failed: {e}")
        return [], {}


def _fetch_from_nse() -> list[str]:
    """
    Fetch live F&O eligible stocks from NSE API.
    Returns list of .NS symbols.
    """
    endpoints = [
        ("https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%20500", "Nifty 500"),
        ("https://www.nseindia.com/api/equity-stockIndices?index=SECURITIES%20IN%20F%26O", "F&O"),
    ]

    for url, label in endpoints:
        try:
            session = requests.Session()
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
                url,
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
                log.warning(f"NSE {label} API returned {response.status_code}")
                continue

            data   = response.json()
            stocks = data.get("data", [])

            symbols = []
            for s in stocks:
                symbol = s.get("symbol", "")
                if symbol and symbol not in ("NIFTY 50", "NIFTY500", "Nifty500"):
                    symbols.append(f"{symbol}.NS")

            if symbols:
                log.info(f"NSE API ({label}): fetched {len(symbols)} symbols")
                return symbols

        except Exception as e:
            log.warning(f"NSE {label} API failed: {e}")
            continue

    return []


def get_fno_universe(force_refresh: bool = False) -> list[str]:
    """
    Returns list of NSE F&O stock symbols in yfinance format.
    Fetches from NSE API once per day, caches result.
    Falls back to hardcoded list if API unavailable.
    """
    global _cache

    today = date.today()

    if not force_refresh and _cache["date"] == today and _cache["symbols"]:
        return _cache["symbols"]

    symbols, lots = _fetch_fno_from_upstox()

    if symbols:
        if lots:
            LOT_SIZES.update(lots)
        _cache = {"symbols": symbols, "date": today}
        return symbols

    symbols = _fetch_from_nse()

    if symbols:
        _cache = {"symbols": symbols, "date": today}
        return symbols

    log.warning("Using hardcoded F&O fallback list")
    _cache = {"symbols": FALLBACK_FNO_SYMBOLS, "date": today}
    return FALLBACK_FNO_SYMBOLS


# ============================================================
# NIFTY 500 UNIVERSE — dynamic fetch from NSE's static archive
#
# The interactive nseindia.com API is blocked at the Akamai layer for
# datacenter IPs (see the comment above _fetch_fno_from_upstox), but
# NSE also publishes index constituent lists as plain CSVs on its
# static archive subdomain, which is NOT behind the same bot check.
# Unlike the F&O list, membership here changes too often (index
# rebalances) to keep a safe hardcoded fallback — on fetch failure we
# just return [] and the scan universe temporarily shrinks back to
# the F&O list until the next successful refresh.
# ============================================================

_NIFTY500_CSV_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"

_nifty500_cache: dict = {"symbols": [], "date": None}


def _fetch_nifty500_from_nse() -> list[str]:
    """Fetch Nifty 500 constituent symbols from NSE's static archive CSV."""
    try:
        response = requests.get(
            _NIFTY500_CSV_URL,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36",
            },
            timeout=15,
        )
        if response.status_code != 200:
            log.warning(f"NSE Nifty 500 archive CSV returned {response.status_code}")
            return []

        reader = csv.DictReader(response.text.splitlines())
        symbols = [
            f"{row['Symbol'].strip()}.NS"
            for row in reader
            if row.get("Symbol", "").strip()
        ]

        if symbols:
            log.info(f"NSE archive: fetched {len(symbols)} Nifty 500 symbols")
        return symbols

    except Exception as e:
        log.warning(f"Nifty 500 archive fetch failed: {e}")
        return []


def get_nifty500_universe(force_refresh: bool = False) -> list[str]:
    """Returns Nifty 500 constituent symbols (yfinance format), cached once per day."""
    global _nifty500_cache

    today = date.today()

    if not force_refresh and _nifty500_cache["date"] == today and _nifty500_cache["symbols"]:
        return _nifty500_cache["symbols"]

    symbols = _fetch_nifty500_from_nse()
    if symbols:
        _nifty500_cache = {"symbols": symbols, "date": today}

    return symbols


def get_all_instruments_extended() -> list[dict]:
    """
    Returns full instrument list: Indexes + F&O stocks + Nifty 500
    (non-F&O) stocks + Commodities. Used by the scheduler when running
    expanded universe.

    Nifty 500 stocks that AREN'T also F&O-eligible are tagged the same
    "STOCK" category as F&O stocks, since RSI+MA / Volume Spike / 3 Bar
    Play all apply generically to any stock. Arbitrage needs a real
    futures contract to exist, so its instrument list is filtered
    separately by get_fno_universe() membership, not by this category
    (see fno_instruments in core/scheduler/signal_scheduler.py).
    """
    from configs.instruments import (
        INDEXES, INDEXES_DISPLAY, INDEXES_TV,
        COMMODITIES, COMMODITIES_DISPLAY, COMMODITIES_TV,
        stock_display,
    )

    instruments = []

    for sym in INDEXES:
        instruments.append({
            "symbol":   sym,
            "name":     INDEXES_DISPLAY.get(sym, sym),
            "tv":       INDEXES_TV.get(sym, sym),
            "category": "INDEX",
        })

    fno = get_fno_universe()
    fno_set = set(fno)
    for sym in fno:
        instruments.append({
            "symbol":   sym,
            "name":     stock_display(sym),
            "tv":       f"NSE:{stock_display(sym)}",
            "category": "STOCK",
        })

    nifty500 = get_nifty500_universe()
    for sym in nifty500:
        if sym in fno_set:
            continue
        instruments.append({
            "symbol":   sym,
            "name":     stock_display(sym),
            "tv":       f"NSE:{stock_display(sym)}",
            "category": "STOCK",
        })

    for sym in COMMODITIES:
        instruments.append({
            "symbol":   sym,
            "name":     COMMODITIES_DISPLAY.get(sym, sym),
            "tv":       COMMODITIES_TV.get(sym, sym),
            "category": "COMMODITY",
        })

    return instruments