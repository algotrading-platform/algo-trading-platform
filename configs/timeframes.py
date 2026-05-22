# ============================================================
# configs/timeframes.py
# ============================================================

TIMEFRAMES = {
    "5 Minutes":  "5m",
    "15 Minutes": "15m",
    "1 Hour":     "1h",
    "1 Day":      "1d",
    "1 Week":     "1wk",
    "1 Month":    "1mo",
}

# TradingView interval mapping for deep-link URLs
TV_INTERVALS = {
    "5 Minutes":  "5",
    "15 Minutes": "15",
    "1 Hour":     "60",
    "1 Day":      "D",
    "1 Week":     "W",
    "1 Month":    "M",
}

# How much historical data to fetch per timeframe
# Chosen to give RSI(14) enough candles + backtest data
# while staying within yfinance limits
PERIOD_MAP = {
    "5 Minutes":  "5d",
    "15 Minutes": "1mo",
    "1 Hour":     "3mo",
    "1 Day":      "1y",
    "1 Week":     "2y",
    "1 Month":    "5y",
}

# ============================================================
# SCHEDULER CONFIG
# Defines WHEN to scan each timeframe after candle close.
# Format: (minute_offset, interval_minutes)
# minute_offset = how many minutes after candle close to scan
# interval_minutes = how often candles close
#
# We always wait 1 minute after candle close for yfinance
# data to propagate before scanning.
# ============================================================

SCHEDULER_CONFIG = {
    # Timeframe      offset  interval
    "5 Minutes":  {"offset": 1, "every_minutes": 5},
    "15 Minutes": {"offset": 1, "every_minutes": 15},
    "1 Hour":     {"offset": 1, "every_minutes": 60},
    "1 Day":      {"offset": 1, "scan_at": "15:31"},  # once daily
    "1 Week":     {"offset": 1, "scan_at": "15:31", "day": "friday"},
    "1 Month":    {"offset": 1, "scan_at": "15:31", "last_day": True},
}
