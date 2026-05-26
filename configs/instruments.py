# ============================================================
# configs/instruments.py
# ============================================================

# ----------------------------------------------------------
# INDEXES
# ----------------------------------------------------------
INDEXES = [
    "^NSEI",
    "^NSEBANK",
    "^BSESN",
]

INDEXES_TV = {
    "^NSEI":    "NSE:NIFTY",
    "^NSEBANK": "NSE:BANKNIFTY",
    "^BSESN":   "BSE:SENSEX",
}

INDEXES_DISPLAY = {
    "^NSEI":    "NIFTY 50",
    "^NSEBANK": "BANK NIFTY",
    "^BSESN":   "SENSEX",
}

# ----------------------------------------------------------
# NSE F&O STOCKS — Top 50 verified yfinance symbols
# Removed: TATAMOTORS.NS (401), LTIM.NS (401)
# Fixed:   LT.NS confirmed working
# ----------------------------------------------------------
STOCKS = [
    # Banking & Finance
    "HDFCBANK.NS",
    "ICICIBANK.NS",
    "KOTAKBANK.NS",
    "AXISBANK.NS",
    "SBIN.NS",
    "INDUSINDBK.NS",
    "BAJFINANCE.NS",
    "BAJAJFINSV.NS",
    "MUTHOOTFIN.NS",
    "PNB.NS",

    # IT
    "TCS.NS",
    "INFY.NS",
    "WIPRO.NS",
    "HCLTECH.NS",
    "TECHM.NS",

    # Energy & Power
    "RELIANCE.NS",
    "ONGC.NS",
    "BPCL.NS",
    "IOC.NS",
    "NTPC.NS",
    "POWERGRID.NS",

    # Auto
    "MARUTI.NS",
    "M&M.NS",
    "BAJAJ-AUTO.NS",
    "EICHERMOT.NS",
    "HEROMOTOCO.NS",

    # Pharma
    "SUNPHARMA.NS",
    "DRREDDY.NS",
    "DIVISLAB.NS",
    "CIPLA.NS",
    "APOLLOHOSP.NS",

    # FMCG
    "HINDUNILVR.NS",
    "ITC.NS",
    "NESTLEIND.NS",
    "BRITANNIA.NS",

    # Metals
    "TATASTEEL.NS",
    "JSWSTEEL.NS",
    "HINDALCO.NS",
    "COALINDIA.NS",
    "VEDL.NS",

    # Infra & Capital Goods
    "LT.NS",
    "SIEMENS.NS",

    # Telecom
    "BHARTIARTL.NS",

    # Adani Group
    "ADANIPORTS.NS",
    "ADANIGREEN.NS",

    # Jwala's original picks
    "CDSL.NS",
    "BSE.NS",
    "SYRMA.NS",
]


def stock_display(symbol: str) -> str:
    return symbol.replace(".NS", "")


# ----------------------------------------------------------
# COMMODITIES
# Note: Only daily/weekly intervals work reliably for
# commodities on yfinance. Intraday (5m, 15m) is skipped
# by the scheduler for these symbols.
# ----------------------------------------------------------
COMMODITIES = [
    "GC=F",    # Gold
    "SI=F",    # Silver
    "HG=F",    # Copper
    "CL=F",    # Crude Oil WTI
]

COMMODITIES_DISPLAY = {
    "GC=F": "GOLD",
    "SI=F": "SILVER",
    "HG=F": "COPPER",
    "CL=F": "CRUDE OIL",
}

COMMODITIES_TV = {
    "GC=F": "COMEX:GC1!",
    "SI=F": "COMEX:SI1!",
    "HG=F": "COMEX:HG1!",
    "CL=F": "NYMEX:CL1!",
}

# Commodities scan in the same 9:15–3:30 window as everything else.
# No timeframes are skipped.
COMMODITIES_SKIP_TIMEFRAMES = set()


def get_all_instruments() -> list[dict]:
    """Flat list of all instruments for the scheduler."""
    instruments = []

    for sym in INDEXES:
        instruments.append({
            "symbol":   sym,
            "name":     INDEXES_DISPLAY.get(sym, sym),
            "tv":       INDEXES_TV.get(sym, sym),
            "category": "INDEX",
        })

    for sym in STOCKS:
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


WATCHLIST = STOCKS