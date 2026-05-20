# ============================================================
# configs/timeframes.py
# ============================================================

TIMEFRAMES = {
    "5 Minutes":  "5m",
    "15 Minutes": "15m",
    "1 Hour":     "1h",
    "1 Day":      "1d",
    "1 Week":     "1wk",
}

# TradingView interval mapping for deep-link URLs
# Used in dashboard to build chart URLs with correct timeframe
TV_INTERVALS = {
    "5 Minutes":  "5",
    "15 Minutes": "15",
    "1 Hour":     "60",
    "1 Day":      "D",
    "1 Week":     "W",
}
