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
# TREND DETECTION — Jwala's logic
# ============================================================

def get_daily_trend(df_daily: pd.DataFrame) -> str:
    """
    Determine if a stock/index is in uptrend or downtrend
    based on its daily chart.

    Logic:
      RISING  → price > EMA20 AND 5-day momentum positive
      FALLING → price < EMA20 AND 5-day momentum negative
      NEUTRAL → mixed signals

    Returns: "RISING" | "FALLING" | "NEUTRAL"
    """
    if df_daily is None or df_daily.empty or len(df_daily) < 21:
        return "NEUTRAL"

    try:
        df = add_ema(df_daily.copy(), periods=[20])
        df.dropna(subset=["EMA_20"], inplace=True)

        if df.empty:
            return "NEUTRAL"

        latest  = df.iloc[-1]
        price   = float(latest["Close"])
        ema20   = float(latest["EMA_20"])

        # 5-day momentum
        if len(df) >= 5:
            price_5d = float(df.iloc[-5]["Close"])
            momentum = (price - price_5d) / price_5d * 100
        else:
            momentum = 0.0

        if price > ema20 and momentum >= 0:
            return "RISING"
        elif price < ema20 and momentum <= 0:
            return "FALLING"
        elif price > ema20:
            return "RISING"
        elif price < ema20:
            return "FALLING"

    except Exception:
        pass

    return "NEUTRAL"


def calculate_signal_strength(
    signal:       str,
    nifty_trend:  str,
    stock_trend:  str,
) -> str:
    """
    Determine signal strength based on Nifty + stock daily trends.

    BUY strength:
      Nifty RISING + Stock RISING  → STRONG
      Stock RISING only             → MODERATE
      Nifty RISING only             → MODERATE
      Both FALLING                  → WEAK (will be suppressed)

    SELL strength:
      Nifty FALLING + Stock FALLING → STRONG
      Stock FALLING only            → MODERATE
      Nifty FALLING only            → MODERATE
      Both RISING                   → WEAK (will be suppressed)
    """
    if signal == "BUY":
        if nifty_trend == "RISING" and stock_trend == "RISING":
            return "STRONG"
        elif stock_trend == "RISING":
            return "MODERATE"
        elif nifty_trend == "RISING":
            return "MODERATE"
        else:
            return "WEAK"

    elif signal == "SELL":
        if nifty_trend == "FALLING" and stock_trend == "FALLING":
            return "STRONG"
        elif stock_trend == "FALLING":
            return "MODERATE"
        elif nifty_trend == "FALLING":
            return "MODERATE"
        else:
            return "WEAK"

    return "WEAK"


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