# ============================================================
# core/indicators/indicators.py
#
# All technical indicators in one module.
# Fixes applied (2026-06-19):
#   - 5min trend: now uses 15min candle comparison (Jwala spec)
#   - Nifty trend: candle-to-candle comparison at each timeframe
#   - Volume spike threshold: 20x → 5x (500%) per Jwala 18-Jun
#   - Volume lookback: 14 candles exactly
# ============================================================

import pandas as pd
import numpy as np
from ta.momentum import RSIIndicator as TA_RSI
from ta.volatility import BollingerBands as TA_BB
from ta.trend import MACD as TA_MACD, EMAIndicator as TA_EMA


# ============================================================
# RSI
# ============================================================

def add_rsi(df: pd.DataFrame, window: int = 14) -> pd.DataFrame:
    """Add RSI column to DataFrame."""
    df = df.copy()
    indicator = TA_RSI(close=df["Close"], window=window)
    df["RSI"] = indicator.rsi()
    return df


# ============================================================
# PIVOT POINTS (Standard)
# ============================================================

def add_pivot_points(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add standard pivot points using previous candle's OHLC.
    Columns added: PP, R1, R2, R3, S1, S2, S3
    """
    df = df.copy()

    prev_high  = df["High"].shift(1)
    prev_low   = df["Low"].shift(1)
    prev_close = df["Close"].shift(1)

    df["PP"] = (prev_high + prev_low + prev_close) / 3
    df["R1"] = (2 * df["PP"]) - prev_low
    df["R2"] = df["PP"] + (prev_high - prev_low)
    df["R3"] = prev_high + 2 * (df["PP"] - prev_low)
    df["S1"] = (2 * df["PP"]) - prev_high
    df["S2"] = df["PP"] - (prev_high - prev_low)
    df["S3"] = prev_low - 2 * (prev_high - df["PP"])

    return df


def get_nearest_level(price: float, df: pd.DataFrame) -> dict:
    """
    Given current price, find nearest pivot support and resistance.
    Returns dict with nearest S and R levels and distances.
    """
    if df.empty or "PP" not in df.columns:
        return {}

    latest = df.iloc[-1]
    levels = {
        "PP": float(latest.get("PP", 0)),
        "R1": float(latest.get("R1", 0)),
        "R2": float(latest.get("R2", 0)),
        "S1": float(latest.get("S1", 0)),
        "S2": float(latest.get("S2", 0)),
    }

    supports    = {k: v for k, v in levels.items() if v < price and v > 0}
    resistances = {k: v for k, v in levels.items() if v > price and v > 0}

    nearest_s = max(supports.items(),    key=lambda x: x[1]) if supports    else None
    nearest_r = min(resistances.items(), key=lambda x: x[1]) if resistances else None

    result = {"levels": levels}

    if nearest_s:
        dist_pct = round(abs(price - nearest_s[1]) / price * 100, 2)
        result["nearest_support"]          = nearest_s[0]
        result["nearest_support_price"]    = round(nearest_s[1], 2)
        result["nearest_support_dist_pct"] = dist_pct

    if nearest_r:
        dist_pct = round(abs(nearest_r[1] - price) / price * 100, 2)
        result["nearest_resistance"]          = nearest_r[0]
        result["nearest_resistance_price"]    = round(nearest_r[1], 2)
        result["nearest_resistance_dist_pct"] = dist_pct

    return result


# ============================================================
# BOLLINGER BANDS
# ============================================================

def add_bollinger_bands(
    df:     pd.DataFrame,
    window: int   = 20,
    std:    float = 2.0,
) -> pd.DataFrame:
    df = df.copy()
    bb = TA_BB(close=df["Close"], window=window, window_dev=std)
    df["BB_UPPER"]  = bb.bollinger_hband()
    df["BB_MIDDLE"] = bb.bollinger_mavg()
    df["BB_LOWER"]  = bb.bollinger_lband()
    df["BB_WIDTH"]  = bb.bollinger_wband()
    df["BB_PCT"]    = bb.bollinger_pband()
    return df


# ============================================================
# EMA
# ============================================================

def add_ema(
    df:      pd.DataFrame,
    periods: list = [9, 20, 50, 200],
) -> pd.DataFrame:
    df = df.copy()
    for period in periods:
        col = f"EMA_{period}"
        df[col] = TA_EMA(close=df["Close"], window=period).ema_indicator()
    return df


def get_ema_trend(df: pd.DataFrame) -> str:
    if df.empty or "EMA_20" not in df.columns or "EMA_50" not in df.columns:
        return "NEUTRAL"
    latest = df.iloc[-1]
    price  = float(latest["Close"])
    ema20  = float(latest.get("EMA_20", 0))
    ema50  = float(latest.get("EMA_50", 0))
    if price > ema20 > ema50:
        return "BULLISH"
    elif price < ema20 < ema50:
        return "BEARISH"
    return "NEUTRAL"


# ============================================================
# MACD
# ============================================================

def add_macd(
    df:            pd.DataFrame,
    fast:          int = 12,
    slow:          int = 26,
    signal_period: int = 9,
) -> pd.DataFrame:
    df = df.copy()
    macd = TA_MACD(
        close=df["Close"],
        window_fast=fast,
        window_slow=slow,
        window_sign=signal_period,
    )
    df["MACD"]        = macd.macd()
    df["MACD_SIGNAL"] = macd.macd_signal()
    df["MACD_HIST"]   = macd.macd_diff()
    return df


# ============================================================
# VOLUME ANALYSIS
# ============================================================

def add_volume_analysis(
    df:     pd.DataFrame,
    window: int = 20,
) -> pd.DataFrame:
    df = df.copy()
    if "Volume" not in df.columns:
        return df
    df["VOL_MA"]    = df["Volume"].rolling(window=window).mean()
    df["VOL_RATIO"] = df["Volume"] / df["VOL_MA"].replace(0, np.nan)
    df["VOL_SURGE"] = df["VOL_RATIO"] > 1.5
    return df


def is_volume_confirmed(df: pd.DataFrame) -> bool:
    if df.empty or "VOL_RATIO" not in df.columns:
        return True
    try:
        ratio = float(df["VOL_RATIO"].iloc[-1])
        return ratio >= 1.2
    except Exception:
        return True


# ============================================================
# TREND DETECTION — Jwala's EXACT specification
# ============================================================

def get_nifty_trend(df_daily: pd.DataFrame) -> str:
    """
    Nifty DAILY trend — Jwala's exact spec:
    "Is today's close higher than yesterday's close?"

    Returns: "RISING" | "FALLING" | "NEUTRAL"
    """
    if df_daily is None or df_daily.empty or len(df_daily) < 2:
        return "NEUTRAL"
    try:
        today_close     = float(df_daily["Close"].iloc[-1])
        yesterday_close = float(df_daily["Close"].iloc[-2])
        if today_close > yesterday_close:
            return "RISING"
        elif today_close < yesterday_close:
            return "FALLING"
    except Exception:
        pass
    return "NEUTRAL"


def get_nifty_hourly_trend(df_1h: pd.DataFrame) -> str:
    """
    Nifty HOURLY trend — Jwala's exact spec:
    "Is the present hour candle higher than previous hourly candle?"

    Returns: "RISING" | "FALLING" | "NEUTRAL"
    """
    if df_1h is None or df_1h.empty or len(df_1h) < 2:
        return "NEUTRAL"
    try:
        curr = float(df_1h["Close"].iloc[-1])
        prev = float(df_1h["Close"].iloc[-2])
        if curr > prev:
            return "RISING"
        elif curr < prev:
            return "FALLING"
    except Exception:
        pass
    return "NEUTRAL"


def get_nifty_5min_trend(df_15m: pd.DataFrame) -> str:
    """
    Nifty 5MIN trend — Jwala's EXACT spec:
    "For 5 minutes, check the previous 15 minute candle.
     If it is up than the previous 15 minute candle,
     then we'll call that as a five-minute uptrend."

    Uses 15min candle data, compares current vs previous 15min candle.

    Returns: "RISING" | "FALLING" | "NEUTRAL"
    """
    if df_15m is None or df_15m.empty or len(df_15m) < 2:
        return "NEUTRAL"
    try:
        curr = float(df_15m["Close"].iloc[-1])
        prev = float(df_15m["Close"].iloc[-2])
        if curr > prev:
            return "RISING"
        elif curr < prev:
            return "FALLING"
    except Exception:
        pass
    return "NEUTRAL"


def get_stock_daily_trend(df_daily: pd.DataFrame) -> str:
    """
    Stock DAILY trend — Jwala's spec:
    "Stock rising from April to May — that is strong."
    "Falling for over a month — I wouldn't buy."

    Logic: 20D EMA + higher highs check

    Returns: "RISING" | "FALLING" | "NEUTRAL"
    """
    if df_daily is None or df_daily.empty or len(df_daily) < 21:
        return "NEUTRAL"
    try:
        df = add_ema(df_daily.copy(), periods=[20])
        df.dropna(subset=["EMA_20"], inplace=True)
        if len(df) < 20:
            return "NEUTRAL"
        latest = df.iloc[-1]
        price  = float(latest["Close"])
        ema20  = float(latest["EMA_20"])
        recent   = df.iloc[-20:]
        first10  = float(recent.iloc[:10]["Close"].mean())
        second10 = float(recent.iloc[10:]["Close"].mean())
        higher_highs = second10 > first10
        above_ema    = price > ema20
        if above_ema and higher_highs:
            return "RISING"
        elif not above_ema and not higher_highs:
            return "FALLING"
        elif above_ema:
            return "RISING"
        else:
            return "FALLING"
    except Exception:
        pass
    return "NEUTRAL"


def get_stock_hourly_trend(df_1h: pd.DataFrame) -> str:
    """
    Stock HOURLY trend — Jwala's spec:
    "Hourly — check previous hourly candle."

    Returns: "RISING" | "FALLING" | "NEUTRAL"
    """
    if df_1h is None or df_1h.empty or len(df_1h) < 2:
        return "NEUTRAL"
    try:
        curr = float(df_1h["Close"].iloc[-1])
        prev = float(df_1h["Close"].iloc[-2])
        if curr > prev:
            return "RISING"
        elif curr < prev:
            return "FALLING"
    except Exception:
        pass
    return "NEUTRAL"


def get_stock_5min_trend(df_15m: pd.DataFrame) -> str:
    """
    Stock 5MIN trend — same as Nifty 5min:
    Uses 15min candle comparison per Jwala's spec.

    Returns: "RISING" | "FALLING" | "NEUTRAL"
    """
    return get_nifty_5min_trend(df_15m)


def get_daily_trend(df_daily: pd.DataFrame) -> str:
    """Backward compatibility wrapper."""
    return get_stock_daily_trend(df_daily)


def should_suppress_signal(
    signal:      str,
    nifty_trend: str,
    stock_trend: str,
) -> bool:
    """
    Suppress signal when both index and stock trend oppose it.
    Suppress BUY when: Nifty FALLING + Stock FALLING
    Suppress SELL when: Nifty RISING + Stock RISING
    """
    if signal == "BUY":
        return nifty_trend == "FALLING" and stock_trend == "FALLING"
    if signal == "SELL":
        return nifty_trend == "RISING" and stock_trend == "RISING"
    return False


# ============================================================
# MULTI-TIMEFRAME TREND — Jwala's 3-arrow system
# FIXED: 5min now uses 15min candle per Jwala's spec
# ============================================================

def get_trend_arrow(trend: str) -> str:
    if trend == "RISING":  return "↑"
    if trend == "FALLING": return "↓"
    return "→"


def get_multi_timeframe_trend(
    provider,
    symbol: str,
) -> dict:
    """
    Get 3-timeframe trend for a symbol.
    FIXED per Jwala's 08-Jun-2026 and 18-Jun-2026 specs:
      Daily  → current day close vs previous day close
      Hourly → current hour close vs previous hour close
      5min   → current 15min close vs previous 15min close (NOT 5min!)

    Returns:
      {
        "daily":  "RISING" | "FALLING" | "NEUTRAL",
        "hourly": "RISING" | "FALLING" | "NEUTRAL",
        "5min":   "RISING" | "FALLING" | "NEUTRAL",
        "label":  "D↑ H↓ 5m↑"
      }
    """
    result = {
        "daily":  "NEUTRAL",
        "hourly": "NEUTRAL",
        "5min":   "NEUTRAL",
        "label":  "D→ H→ 5m→",
    }

    try:
        df_daily = provider.fetch_data(symbol=symbol, interval="1d", period="5d")
        result["daily"] = get_nifty_trend(df_daily)
    except Exception:
        pass

    try:
        df_1h = provider.fetch_data(symbol=symbol, interval="1h", period="5d")
        result["hourly"] = get_nifty_hourly_trend(df_1h)
    except Exception:
        pass

    try:
        # CRITICAL FIX: use 15min candles for 5min trend per Jwala
        df_15m = provider.fetch_data(symbol=symbol, interval="15m", period="3d")
        result["5min"] = get_nifty_5min_trend(df_15m)
    except Exception:
        pass

    d = get_trend_arrow(result["daily"])
    h = get_trend_arrow(result["hourly"])
    m = get_trend_arrow(result["5min"])
    result["label"] = f"D{d} H{h} 5m{m}"

    return result


def get_nifty_multi_trend(provider) -> dict:
    """Get 3-timeframe trend for Nifty 50."""
    return get_multi_timeframe_trend(provider, "^NSEI")


# ============================================================
# VOLUME SPIKE DETECTION
# FIXED per Jwala 18-Jun-2026: threshold 20x → 5x (500%)
# ============================================================

VOLUME_SPIKE_STRONG   = 5.0    # 500% = STRONG (Jwala's spec 18-Jun)
VOLUME_SPIKE_MODERATE = 3.0    # 300% = MODERATE


def get_volume_spike_ratio(df: pd.DataFrame) -> float:
    """
    Calculate current 5min candle volume vs previous 14 candle average.
    Returns ratio: 5.0 = 500% spike.

    Jwala 18-Jun-2026:
    "5 minute volume if it is greater than 500% of the
     fourteen previous 5 minute volume average — buy."
    """
    if df is None or df.empty or "Volume" not in df.columns:
        return 0.0
    try:
        df = df.copy()
        df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce")
        df.dropna(subset=["Volume"], inplace=True)
        if len(df) < 15:
            return 0.0
        # 14 previous candles average (excluding current)
        avg_volume  = float(df["Volume"].iloc[-15:-1].mean())
        curr_volume = float(df["Volume"].iloc[-1])
        if avg_volume <= 0:
            return 0.0
        return round(curr_volume / avg_volume, 2)
    except Exception:
        return 0.0


def get_volume_spike_label(ratio: float) -> str:
    """Human readable volume spike description."""
    pct = round(ratio * 100)
    if ratio >= VOLUME_SPIKE_STRONG:
        return f"VOL {pct}% 🔥"
    if ratio >= VOLUME_SPIKE_MODERATE:
        return f"VOL {pct}% ↑"
    return f"VOL {pct}%"


# ============================================================
# SIGNAL STRENGTH — Jwala's EXACT spec (18-Jun-2026)
# ============================================================

def calculate_signal_strength(
    signal:        str,
    nifty_trend:   str,
    stock_trend:   str,
    volume_ratio:  float = 0.0,
    tf_name:       str   = "",
    nifty_hourly:  str   = "NEUTRAL",
    nifty_5min:    str   = "NEUTRAL",
    stock_hourly:  str   = "NEUTRAL",
    stock_5min:    str   = "NEUTRAL",
    rsi_val:       float = 50.0,
) -> str:
    """
    Jwala's signal strength spec (18-Jun-2026):

    STRONGEST (1 Hour timeframe):
      - 1 Hour RSI is low (below 35 for BUY)
      - Nifty Hourly rising
      - Nifty 5min rising
      - Stock Hourly rising

    STRONG (5min/other timeframe):
      - RSI is low
      - Nifty 5min rising
      - Stock 5min rising

    MODERATE:
      - RSI condition met
      - One of Nifty or Stock trends align

    WEAK:
      - RSI only, no trend confirmation
    """
    has_vol_spike  = volume_ratio >= VOLUME_SPIKE_STRONG
    rsi_is_low     = rsi_val < 35 if signal == "BUY"  else rsi_val > 65
    is_1h          = "hour" in tf_name.lower() or tf_name == "1 Hour"

    if signal == "BUY":
        # STRONGEST: 1H RSI low + Nifty H rising + Nifty 5m rising + Stock H rising
        if is_1h and rsi_is_low and nifty_hourly == "RISING" and nifty_5min == "RISING" and stock_hourly == "RISING":
            return "VERY STRONG"

        # STRONG: RSI low + Nifty 5m rising + Stock 5m rising
        if rsi_is_low and nifty_5min == "RISING" and stock_5min == "RISING":
            return "STRONG"

        # STRONG: volume spike
        if has_vol_spike:
            return "STRONG"

        # MODERATE: trends aligned
        if nifty_trend == "RISING" and stock_trend == "RISING":
            return "MODERATE"
        if nifty_trend == "RISING" or stock_trend == "RISING":
            return "MODERATE"

        return "WEAK"

    elif signal == "SELL":
        # STRONGEST: 1H RSI high + Nifty H falling + Nifty 5m falling + Stock H falling
        if is_1h and rsi_is_low and nifty_hourly == "FALLING" and nifty_5min == "FALLING" and stock_hourly == "FALLING":
            return "VERY STRONG"

        # STRONG: RSI high + Nifty 5m falling + Stock 5m falling
        if rsi_is_low and nifty_5min == "FALLING" and stock_5min == "FALLING":
            return "STRONG"

        if has_vol_spike:
            return "STRONG"

        if nifty_trend == "FALLING" and stock_trend == "FALLING":
            return "MODERATE"
        if nifty_trend == "FALLING" or stock_trend == "FALLING":
            return "MODERATE"

        return "WEAK"

    return "WEAK"