# ============================================================
# core/indicators/indicators.py
#
# All technical indicators in one module:
#   - RSI (wrapper around existing)
#   - Pivot Points (Standard)
#   - Bollinger Bands
#   - EMA (multiple periods)
#   - MACD
#   - Volume Analysis
#
# All methods accept a pandas DataFrame with OHLCV columns
# and return the same DataFrame with new columns added.
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

    For intraday: uses previous candle
    For daily: uses previous day's candle
    """
    df = df.copy()

    # Use previous candle's High, Low, Close
    prev_high  = df["High"].shift(1)
    prev_low   = df["Low"].shift(1)
    prev_close = df["Close"].shift(1)

    # Pivot Point
    df["PP"] = (prev_high + prev_low + prev_close) / 3

    # Resistance levels
    df["R1"] = (2 * df["PP"]) - prev_low
    df["R2"] = df["PP"] + (prev_high - prev_low)
    df["R3"] = prev_high + 2 * (df["PP"] - prev_low)

    # Support levels
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

    # Find nearest support (below price) and resistance (above price)
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
    """
    Add Bollinger Bands columns.
    Columns added: BB_UPPER, BB_MIDDLE, BB_LOWER, BB_WIDTH, BB_PCT
    """
    df = df.copy()
    bb = TA_BB(close=df["Close"], window=window, window_dev=std)
    df["BB_UPPER"]  = bb.bollinger_hband()
    df["BB_MIDDLE"] = bb.bollinger_mavg()
    df["BB_LOWER"]  = bb.bollinger_lband()
    df["BB_WIDTH"]  = bb.bollinger_wband()
    df["BB_PCT"]    = bb.bollinger_pband()  # 0=lower band, 1=upper band
    return df


# ============================================================
# EMA
# ============================================================

def add_ema(
    df:      pd.DataFrame,
    periods: list[int] = [9, 20, 50, 200],
) -> pd.DataFrame:
    """
    Add EMA columns for multiple periods.
    Columns added: EMA_9, EMA_20, EMA_50, EMA_200
    """
    df = df.copy()
    for period in periods:
        col = f"EMA_{period}"
        df[col] = TA_EMA(close=df["Close"], window=period).ema_indicator()
    return df


def get_ema_trend(df: pd.DataFrame) -> str:
    """
    Returns trend direction based on EMA alignment.
    BULLISH: price > EMA_20 > EMA_50
    BEARISH: price < EMA_20 < EMA_50
    NEUTRAL: mixed
    """
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
    df:          pd.DataFrame,
    fast:        int = 12,
    slow:        int = 26,
    signal_period: int = 9,
) -> pd.DataFrame:
    """
    Add MACD columns.
    Columns added: MACD, MACD_SIGNAL, MACD_HIST
    """
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
    """
    Add volume analysis columns.
    Columns added: VOL_MA, VOL_RATIO, VOL_SURGE
    """
    df = df.copy()

    if "Volume" not in df.columns:
        return df

    df["VOL_MA"]    = df["Volume"].rolling(window=window).mean()
    df["VOL_RATIO"] = df["Volume"] / df["VOL_MA"].replace(0, np.nan)
    df["VOL_SURGE"] = df["VOL_RATIO"] > 1.5  # True when volume > 1.5x average

    return df


def is_volume_confirmed(df: pd.DataFrame) -> bool:
    """Returns True if latest candle has above-average volume."""
    if df.empty or "VOL_RATIO" not in df.columns:
        return True  # assume confirmed if no volume data
    try:
        ratio = float(df["VOL_RATIO"].iloc[-1])
        return ratio >= 1.2  # at least 20% above average
    except Exception:
        return True


# ============================================================
# TREND DETECTION — Jwala's exact logic
# ============================================================

def get_nifty_trend(df_daily: pd.DataFrame) -> str:
    """
    Nifty trend — Jwala's exact specification:
    "Is Nifty at this time higher than yesterday's close or not?"

    Simple intraday comparison:
      Today's close > Yesterday's close → RISING
      Today's close < Yesterday's close → FALLING

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


def get_stock_daily_trend(df_daily: pd.DataFrame) -> str:
    """
    Stock 1-month trend — Jwala's exact specification:
    "If daily is bullish — stock rising from April to May — that is strong."
    "For Emami — falling from 470 to 399 for over a month — I wouldn't buy."

    Logic (20D EMA + higher highs):
      RISING  → price > 20D EMA AND second half of month higher than first half
      FALLING → price < 20D EMA AND second half lower than first half
      NEUTRAL → mixed signals

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

        # Higher highs check — split last 20 days into two halves
        recent   = df.iloc[-20:]
        first10  = float(recent.iloc[:10]["Close"].mean())
        second10 = float(recent.iloc[10:]["Close"].mean())
        higher_highs = second10 > first10

        above_ema = price > ema20

        if above_ema and higher_highs:
            return "RISING"
        elif not above_ema and not higher_highs:
            return "FALLING"
        elif above_ema:
            return "RISING"   # EMA is primary indicator
        else:
            return "FALLING"

    except Exception:
        pass

    return "NEUTRAL"


def get_daily_trend(df_daily: pd.DataFrame) -> str:
    """
    Generic trend — used for stocks (calls get_stock_daily_trend).
    Kept for backward compatibility.
    """
    return get_stock_daily_trend(df_daily)


# calculate_signal_strength — see updated version below with volume_ratio

def should_suppress_signal(
    signal:      str,
    nifty_trend: str,
    stock_trend: str,
) -> bool:
    """
    Suppress signal when both index and stock trend oppose it.
    Reduces noise and improves signal quality.

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
# ============================================================

def get_trend_arrow(trend: str) -> str:
    """Convert trend string to arrow."""
    if trend == "RISING":  return "↑"
    if trend == "FALLING": return "↓"
    return "→"


def get_multi_timeframe_trend(
    provider,
    symbol: str,
) -> dict:
    """
    Get 3-timeframe trend for a symbol.
    Returns dict with daily, hourly, 5min trends.

    Jwala's spec:
      Daily  → is stock/nifty trending up over past month?
      Hourly → is it rising in last few hours?
      5min   → is it rising right now?

    Returns:
      {
        "daily":  "RISING" | "FALLING" | "NEUTRAL",
        "hourly": "RISING" | "FALLING" | "NEUTRAL",
        "5min":   "RISING" | "FALLING" | "NEUTRAL",
        "label":  "D↑ H↓ 5m↑"
      }
    """
    result = {"daily": "NEUTRAL", "hourly": "NEUTRAL", "5min": "NEUTRAL", "label": "D→ H→ 5m→"}

    try:
        # Daily trend — stock daily chart (1 month)
        df_daily = provider.fetch_data(symbol=symbol, interval="1d", period="3mo")
        result["daily"] = get_stock_daily_trend(df_daily)
    except Exception:
        pass

    try:
        # Hourly trend — last 5 hourly candles
        df_1h = provider.fetch_data(symbol=symbol, interval="1h", period="5d")
        if df_1h is not None and len(df_1h) >= 3:
            # Simple: current hourly close vs 3 hours ago
            cur   = float(df_1h["Close"].iloc[-1])
            prev3 = float(df_1h["Close"].iloc[-3])
            result["hourly"] = "RISING" if cur > prev3 else ("FALLING" if cur < prev3 else "NEUTRAL")
    except Exception:
        pass

    try:
        # 5min trend — last 6 x 5min candles (30 mins)
        df_5m = provider.fetch_data(symbol=symbol, interval="5m", period="1d")
        if df_5m is not None and len(df_5m) >= 6:
            cur   = float(df_5m["Close"].iloc[-1])
            prev6 = float(df_5m["Close"].iloc[-6])
            result["5min"] = "RISING" if cur > prev6 else ("FALLING" if cur < prev6 else "NEUTRAL")
    except Exception:
        pass

    # Build label: "D↑ H↓ 5m↑"
    d = get_trend_arrow(result["daily"])
    h = get_trend_arrow(result["hourly"])
    m = get_trend_arrow(result["5min"])
    result["label"] = f"D{d} H{h} 5m{m}"

    return result


def get_nifty_multi_trend(provider) -> dict:
    """
    Get 3-timeframe trend for Nifty 50.
    Uses ^NSEI symbol.
    """
    return get_multi_timeframe_trend(provider, "^NSEI")


# ============================================================
# VOLUME SPIKE DETECTION — Jwala's primary confirmation
# ============================================================

VOLUME_SPIKE_STRONG   = 20.0   # 2000% = STRONG confirmation
VOLUME_SPIKE_MODERATE = 10.0   # 1000% = MODERATE confirmation


def get_volume_spike_ratio(df: pd.DataFrame) -> float:
    """
    Calculate current candle volume vs 2-week average.
    Returns ratio: 20.0 = 2000% spike.
    Jwala: "8 out of 10 stocks rise when volume > 2000% of average"
    """
    if df is None or df.empty or "Volume" not in df.columns:
        return 0.0

    try:
        df = df.copy()
        df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce")
        df.dropna(subset=["Volume"], inplace=True)

        if len(df) < 15:
            return 0.0

        # 14 candles = 2 weeks of daily / recent intraday
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
# VOLUME SPIKE DETECTION — Jwala's primary confirmation
# ============================================================

VOLUME_SPIKE_STRONG   = 20.0   # 2000% = STRONG confirmation
VOLUME_SPIKE_MODERATE = 10.0   # 1000% = MODERATE confirmation


def get_volume_spike_ratio(df: pd.DataFrame) -> float:
    """
    Calculate current candle volume vs 2-week average.
    Returns ratio: 20.0 = 2000% spike.
    """
    if df is None or df.empty or "Volume" not in df.columns:
        return 0.0
    try:
        df = df.copy()
        df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce")
        df.dropna(subset=["Volume"], inplace=True)
        if len(df) < 15:
            return 0.0
        avg_volume  = float(df["Volume"].iloc[-15:-1].mean())
        curr_volume = float(df["Volume"].iloc[-1])
        if avg_volume <= 0:
            return 0.0
        return round(curr_volume / avg_volume, 2)
    except Exception:
        return 0.0


def get_volume_spike_label(ratio: float) -> str:
    pct = round(ratio * 100)
    if ratio >= VOLUME_SPIKE_STRONG:   return f"VOL {pct}% 🔥"
    if ratio >= VOLUME_SPIKE_MODERATE: return f"VOL {pct}% ↑"
    return f"VOL {pct}%"


def calculate_signal_strength(
    signal:        str,
    nifty_trend:   str,
    stock_trend:   str,
    volume_ratio:  float = 0.0,
) -> str:
    """
    Clean 2-factor strength — Volume spike is primary confirmation.
    """
    has_vol_spike = volume_ratio >= VOLUME_SPIKE_STRONG

    if signal == "BUY":
        trends_aligned = nifty_trend == "RISING" and stock_trend == "RISING"
        if has_vol_spike and trends_aligned:  return "VERY STRONG"
        if has_vol_spike:                     return "STRONG"
        if trends_aligned:                    return "MODERATE"
        if nifty_trend == "RISING" or stock_trend == "RISING": return "MODERATE"
        return "WEAK"

    elif signal == "SELL":
        trends_aligned = nifty_trend == "FALLING" and stock_trend == "FALLING"
        if has_vol_spike and trends_aligned:  return "VERY STRONG"
        if has_vol_spike:                     return "STRONG"
        if trends_aligned:                    return "MODERATE"
        if nifty_trend == "FALLING" or stock_trend == "FALLING": return "MODERATE"
        return "WEAK"

    return "WEAK"