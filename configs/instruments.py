# ============================================================
# configs/instruments.py
#
# Three instrument categories Jwala requested:
#   - INDEXES   : Nifty 50, Bank Nifty, BSE Sensex
#   - STOCKS    : NSE F&O watchlist (expandable to 200+)
#   - COMMODITIES : MCX via yfinance CMX/GCF symbols
#
# TradingView symbol mapping lives here too so the dashboard
# can build deep-links with the correct exchange prefix.
# ============================================================

# ----------------------------------------------------------
# INDEXES
# ----------------------------------------------------------
# ^NSEI  = Nifty 50
# ^NSEBANK = Bank Nifty
# ^BSESN = BSE Sensex
# ----------------------------------------------------------
INDEXES = [
    "^NSEI",
    "^NSEBANK",
    "^BSESN",
]

# TradingView symbol map for indexes
INDEXES_TV = {
    "^NSEI":    "NSE:NIFTY",
    "^NSEBANK": "NSE:BANKNIFTY",
    "^BSESN":   "BSE:SENSEX",
}

# Display names for indexes
INDEXES_DISPLAY = {
    "^NSEI":    "NIFTY 50",
    "^NSEBANK": "BANK NIFTY",
    "^BSESN":   "SENSEX",
}


# ----------------------------------------------------------
# NSE STOCKS  (F&O watchlist – expand to 200 as needed)
# ----------------------------------------------------------
STOCKS = [
    "RELIANCE.NS",
    "INDUSINDBK.NS",
    "MUTHOOTFIN.NS",
    "CDSL.NS",
    "BSE.NS",
    "SYRMA.NS",
    "HDFCBANK.NS",
    "INFY.NS",
    "TCS.NS",
    "ICICIBANK.NS",
    "AXISBANK.NS",
    "KOTAKBANK.NS",
    "SBIN.NS",
    "BAJFINANCE.NS",
    "MARUTI.NS",
    "TATAmotors.NS",
    "ADANIPORTS.NS",
    "WIPRO.NS",
    "SUNPHARMA.NS",
    "TITAN.NS",
]

# Display helper for stocks
def stock_display(symbol: str) -> str:
    return symbol.replace(".NS", "")


# ----------------------------------------------------------
# COMMODITIES  (MCX via yfinance)
# ----------------------------------------------------------
# GC=F   = Gold Futures (COMEX / dollar-based, usable for signals)
# SI=F   = Silver Futures (COMEX)
# HG=F   = Copper Futures
# ZN=F   = Zinc (no direct yfinance; skip for now)
# CL=F   = Crude Oil WTI
#
# For Indian MCX rupee prices, Upstox API will replace these.
# These symbols are suitable for RSI signal generation today.
# ----------------------------------------------------------
COMMODITIES = [
    "GC=F",
    "SI=F",
    "HG=F",
    "CL=F",
]

# Display names
COMMODITIES_DISPLAY = {
    "GC=F": "GOLD",
    "SI=F": "SILVER",
    "HG=F": "COPPER",
    "CL=F": "CRUDE OIL",
}

# TradingView symbol map for commodities
COMMODITIES_TV = {
    "GC=F": "COMEX:GC1!",
    "SI=F": "COMEX:SI1!",
    "HG=F": "COMEX:HG1!",
    "CL=F": "NYMEX:CL1!",
}


# ----------------------------------------------------------
# Legacy: keep WATCHLIST alias so main.py still works
# ----------------------------------------------------------
WATCHLIST = STOCKS
