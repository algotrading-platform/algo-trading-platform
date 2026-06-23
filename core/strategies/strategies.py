# ============================================================
# core/strategies/strategies.py
#
# All trading strategies:
#
#  1. RSIReversalStrategy      — RSI oversold/overbought reversal
#  2. RSIPivotStrategy         — RSI reversal confirmed by pivot level
#  3. BollingerStrategy        — Bollinger Band squeeze breakout
#  4. EMACrossoverStrategy     — EMA crossover with trend confirmation
#  5. MACDStrategy             — MACD line crossover signal
#  6. VolumeBreakoutStrategy   — Price breakout with volume surge
#  7. ArbitrageStrategy        — Cash-Futures spread arbitrage
#
# Each strategy returns a SignalResult with:
#   - signal:    BUY | SELL | HOLD
#   - strength:  STRONG | MODERATE | WEAK
#   - reason:    Plain-English explanation
#   - indicators: Key values used in decision
# ============================================================

import pandas as pd
from core.strategies.base_strategy import BaseStrategy, SignalResult
from core.indicators.indicators import (
    add_rsi, add_pivot_points, add_bollinger_bands,
    add_ema, add_macd, add_volume_analysis,
    get_nearest_level, get_ema_trend, is_volume_confirmed,
)


# ============================================================
# 1. RSI REVERSAL STRATEGY
# ============================================================

class RSIReversalStrategy(BaseStrategy):
    """
    Buy when RSI bounces back above 30 after being oversold.
    Sell when RSI drops back below 70 after being overbought.
    Requires 2 consecutive confirmation candles.
    """
    name        = "RSI Reversal"
    description = (
        "Identifies momentum reversals using RSI 14. "
        "BUY when RSI recovers from oversold (<30) zone with "
        "2 confirmation candles. SELL when RSI drops from "
        "overbought (>70) zone with 2 confirmation candles."
    )

    def generate_signal(self, df: pd.DataFrame) -> SignalResult:
        if len(df) < 20:
            return SignalResult("HOLD", "WEAK", "Insufficient data", strategy=self.name)

        df = add_rsi(df)
        df.dropna(subset=["RSI"], inplace=True)

        if len(df) < 3:
            return SignalResult("HOLD", "WEAK", "Insufficient RSI data", strategy=self.name)

        current = float(df["RSI"].iloc[-1])
        prev    = float(df["RSI"].iloc[-2])
        prev2   = float(df["RSI"].iloc[-3])
        price   = float(df["Close"].iloc[-1])

        indicators = {
            "RSI": round(current, 2),
            "RSI_prev": round(prev, 2),
            "Price": round(price, 2),
        }

        # BUY: RSI bounced from oversold
        if prev2 < 35 and prev > prev2 and current > prev and current > 35:
            strength = "STRONG" if prev2 < 28 else "MODERATE"
            reason = (
                f"RSI recovered from oversold zone. "
                f"RSI was {round(prev2,1)} (below 30), now rising to {round(current,1)}. "
                f"Two consecutive up candles confirm reversal."
            )
            return SignalResult("BUY", strength, reason, indicators, self.name)

        # SELL: RSI reversed from overbought
        if prev2 > 75 and prev < prev2 and current < prev and current < 75:
            strength = "STRONG" if prev2 > 80 else "MODERATE"
            reason = (
                f"RSI reversed from overbought zone. "
                f"RSI was {round(prev2,1)} (above 70), now falling to {round(current,1)}. "
                f"Two consecutive down candles confirm reversal."
            )
            return SignalResult("SELL", strength, reason, indicators, self.name)

        return SignalResult("HOLD", "WEAK", f"RSI at {round(current,1)} — no reversal pattern", indicators, self.name)


# ============================================================
# 2. RSI + PIVOT CONFLUENCE STRATEGY
# ============================================================

class RSIPivotStrategy(BaseStrategy):
    """
    RSI reversal confirmed by proximity to pivot support/resistance.
    BUY only when RSI oversold AND price near S1 or S2.
    SELL only when RSI overbought AND price near R1 or R2.
    Much higher quality signals than RSI alone.
    """
    name        = "RSI + Pivot Confluence"
    description = (
        "Combines RSI reversal signals with Pivot Point levels. "
        "BUY only when RSI recovers from oversold zone AND price "
        "is within 1% of S1/S2 support. SELL only when RSI drops "
        "from overbought AND price is within 1% of R1/R2 resistance. "
        "Significantly reduces false signals."
    )

    PROXIMITY_PCT = 1.0  # signal only within 1% of pivot level

    def generate_signal(self, df: pd.DataFrame) -> SignalResult:
        if len(df) < 25:
            return SignalResult("HOLD", "WEAK", "Insufficient data", strategy=self.name)

        df = add_rsi(df)
        df = add_pivot_points(df)
        df.dropna(subset=["RSI", "PP"], inplace=True)

        if len(df) < 3:
            return SignalResult("HOLD", "WEAK", "Insufficient data after indicators", strategy=self.name)

        current = float(df["RSI"].iloc[-1])
        prev    = float(df["RSI"].iloc[-2])
        prev2   = float(df["RSI"].iloc[-3])
        price   = float(df["Close"].iloc[-1])

        pivot_info = get_nearest_level(price, df)
        levels     = pivot_info.get("levels", {})

        indicators = {
            "RSI": round(current, 2),
            "Price": round(price, 2),
            "PP":  round(levels.get("PP", 0), 2),
            "S1":  round(levels.get("S1", 0), 2),
            "S2":  round(levels.get("S2", 0), 2),
            "R1":  round(levels.get("R1", 0), 2),
            "R2":  round(levels.get("R2", 0), 2),
        }

        # Check RSI reversal
        rsi_buy  = prev2 < 35 and prev > prev2 and current > prev and current > 35
        rsi_sell = prev2 > 75 and prev < prev2 and current < prev and current < 75

        def near_level(level_price: float) -> bool:
            if level_price <= 0:
                return False
            dist_pct = abs(price - level_price) / price * 100
            return dist_pct <= self.PROXIMITY_PCT

        if rsi_buy:
            s1 = levels.get("S1", 0)
            s2 = levels.get("S2", 0)
            near_support = near_level(s1) or near_level(s2)

            if near_support:
                support_level = "S1" if near_level(s1) else "S2"
                support_price = round(s1 if near_level(s1) else s2, 2)
                reason = (
                    f"STRONG confluence signal: RSI reversed from oversold "
                    f"({round(prev2,1)} → {round(current,1)}) AND price "
                    f"₹{round(price,2)} is near {support_level} support "
                    f"₹{support_price}. Double confirmation."
                )
                return SignalResult("BUY", "STRONG", reason, indicators, self.name)
            else:
                return SignalResult(
                    "HOLD", "WEAK",
                    f"RSI reversed from oversold but price not near support. "
                    f"Nearest support S1=₹{round(levels.get('S1',0),2)}",
                    indicators, self.name
                )

        if rsi_sell:
            r1 = levels.get("R1", 0)
            r2 = levels.get("R2", 0)
            near_resistance = near_level(r1) or near_level(r2)

            if near_resistance:
                res_level = "R1" if near_level(r1) else "R2"
                res_price = round(r1 if near_level(r1) else r2, 2)
                reason = (
                    f"STRONG confluence signal: RSI reversed from overbought "
                    f"({round(prev2,1)} → {round(current,1)}) AND price "
                    f"₹{round(price,2)} is near {res_level} resistance "
                    f"₹{res_price}. Double confirmation."
                )
                return SignalResult("SELL", "STRONG", reason, indicators, self.name)
            else:
                return SignalResult(
                    "HOLD", "WEAK",
                    f"RSI reversed from overbought but price not near resistance.",
                    indicators, self.name
                )

        return SignalResult("HOLD", "WEAK", f"RSI at {round(current,1)} — no confluence setup", indicators, self.name)


# ============================================================
# 3. BOLLINGER BANDS STRATEGY
# ============================================================

class BollingerStrategy(BaseStrategy):
    """
    Buy when price touches lower Bollinger Band and starts recovering.
    Sell when price touches upper Bollinger Band and starts declining.
    Uses BB %B indicator for precision.
    """
    name        = "Bollinger Bands"
    description = (
        "Uses Bollinger Bands (20,2) to identify mean-reversion opportunities. "
        "BUY when price touches/breaks lower band (BB%B < 0.05) and next "
        "candle shows recovery. SELL when price touches upper band "
        "(BB%B > 0.95) and next candle shows decline. "
        "Works best in ranging/consolidating markets."
    )

    def generate_signal(self, df: pd.DataFrame) -> SignalResult:
        if len(df) < 25:
            return SignalResult("HOLD", "WEAK", "Insufficient data", strategy=self.name)

        df = add_bollinger_bands(df)
        df.dropna(subset=["BB_UPPER", "BB_LOWER", "BB_PCT"], inplace=True)

        if len(df) < 3:
            return SignalResult("HOLD", "WEAK", "Insufficient data after BB", strategy=self.name)

        latest   = df.iloc[-1]
        prev     = df.iloc[-2]
        price    = float(latest["Close"])
        bb_pct   = float(latest["BB_PCT"])
        bb_upper = float(latest["BB_UPPER"])
        bb_lower = float(latest["BB_LOWER"])
        bb_mid   = float(latest["BB_MIDDLE"])
        bb_width = float(latest["BB_WIDTH"])

        prev_price  = float(prev["Close"])
        prev_bb_pct = float(prev["BB_PCT"])

        indicators = {
            "BB_PCT":   round(bb_pct, 3),
            "BB_UPPER": round(bb_upper, 2),
            "BB_LOWER": round(bb_lower, 2),
            "BB_MID":   round(bb_mid, 2),
            "BB_WIDTH": round(bb_width, 2),
            "Price":    round(price, 2),
        }

        # BUY: price was at/below lower band, now recovering
        if prev_bb_pct <= 0.05 and bb_pct > prev_bb_pct and price > prev_price:
            squeeze = bb_width < 0.1
            strength = "STRONG" if prev_bb_pct < 0 else "MODERATE"
            reason = (
                f"Price touched lower Bollinger Band "
                f"(BB%B={round(prev_bb_pct,3)}) and is now recovering. "
                f"Price ₹{round(price,2)} bouncing from lower band "
                f"₹{round(bb_lower,2)}. "
                f"{'Narrow bands suggest squeeze breakout.' if squeeze else ''}"
            )
            return SignalResult("BUY", strength, reason, indicators, self.name)

        # SELL: price was at/above upper band, now declining
        if prev_bb_pct >= 0.95 and bb_pct < prev_bb_pct and price < prev_price:
            strength = "STRONG" if prev_bb_pct > 1.0 else "MODERATE"
            reason = (
                f"Price touched upper Bollinger Band "
                f"(BB%B={round(prev_bb_pct,3)}) and is now declining. "
                f"Price ₹{round(price,2)} reversing from upper band "
                f"₹{round(bb_upper,2)}."
            )
            return SignalResult("SELL", strength, reason, indicators, self.name)

        return SignalResult(
            "HOLD", "WEAK",
            f"Price at BB%B={round(bb_pct,2)} — not at band extremes",
            indicators, self.name
        )


# ============================================================
# 4. EMA CROSSOVER STRATEGY
# ============================================================

class EMACrossoverStrategy(BaseStrategy):
    """
    Buy when short EMA crosses above long EMA (golden cross).
    Sell when short EMA crosses below long EMA (death cross).
    Confirms with price position relative to trend.
    """
    name        = "EMA Crossover"
    description = (
        "Uses 9-period and 20-period EMA crossover. "
        "BUY signal (Golden Cross) when EMA9 crosses above EMA20 "
        "AND price is above EMA50 (trend confirmation). "
        "SELL signal (Death Cross) when EMA9 crosses below EMA20 "
        "AND price is below EMA50. Trend-following strategy."
    )

    def generate_signal(self, df: pd.DataFrame) -> SignalResult:
        if len(df) < 55:
            return SignalResult("HOLD", "WEAK", "Insufficient data for EMA50", strategy=self.name)

        df = add_ema(df, periods=[9, 20, 50])
        df.dropna(subset=["EMA_9", "EMA_20", "EMA_50"], inplace=True)

        if len(df) < 2:
            return SignalResult("HOLD", "WEAK", "Insufficient data after EMA", strategy=self.name)

        latest = df.iloc[-1]
        prev   = df.iloc[-2]

        price  = float(latest["Close"])
        ema9   = float(latest["EMA_9"])
        ema20  = float(latest["EMA_20"])
        ema50  = float(latest["EMA_50"])

        prev_ema9  = float(prev["EMA_9"])
        prev_ema20 = float(prev["EMA_20"])

        trend = get_ema_trend(df)

        indicators = {
            "EMA_9":  round(ema9, 2),
            "EMA_20": round(ema20, 2),
            "EMA_50": round(ema50, 2),
            "Trend":  trend,
            "Price":  round(price, 2),
        }

        # Golden Cross: EMA9 crosses above EMA20
        golden_cross = prev_ema9 <= prev_ema20 and ema9 > ema20

        # Death Cross: EMA9 crosses below EMA20
        death_cross = prev_ema9 >= prev_ema20 and ema9 < ema20

        if golden_cross:
            in_uptrend = price > ema50
            strength   = "STRONG" if in_uptrend else "MODERATE"
            reason = (
                f"Golden Cross: EMA9 (₹{round(ema9,2)}) crossed above "
                f"EMA20 (₹{round(ema20,2)}). "
                f"{'Price above EMA50 confirms uptrend.' if in_uptrend else 'Note: price below EMA50, use caution.'}"
            )
            return SignalResult("BUY", strength, reason, indicators, self.name)

        if death_cross:
            in_downtrend = price < ema50
            strength     = "STRONG" if in_downtrend else "MODERATE"
            reason = (
                f"Death Cross: EMA9 (₹{round(ema9,2)}) crossed below "
                f"EMA20 (₹{round(ema20,2)}). "
                f"{'Price below EMA50 confirms downtrend.' if in_downtrend else 'Note: price above EMA50, use caution.'}"
            )
            return SignalResult("SELL", strength, reason, indicators, self.name)

        crossover_type = "EMA9 above EMA20" if ema9 > ema20 else "EMA9 below EMA20"
        return SignalResult(
            "HOLD", "WEAK",
            f"No crossover. {crossover_type}. Trend: {trend}",
            indicators, self.name
        )


# ============================================================
# 5. MACD STRATEGY
# ============================================================

class MACDStrategy(BaseStrategy):
    """
    Buy when MACD line crosses above signal line (bullish crossover).
    Sell when MACD line crosses below signal line (bearish crossover).
    Confirms with histogram momentum.
    """
    name        = "MACD"
    description = (
        "Uses MACD (12,26,9) crossover signals. "
        "BUY when MACD line crosses above signal line AND "
        "histogram turns positive (momentum building). "
        "SELL when MACD line crosses below signal line AND "
        "histogram turns negative. Works best on 1H and 1D timeframes."
    )

    def generate_signal(self, df: pd.DataFrame) -> SignalResult:
        if len(df) < 35:
            return SignalResult("HOLD", "WEAK", "Insufficient data for MACD", strategy=self.name)

        df = add_macd(df)
        df.dropna(subset=["MACD", "MACD_SIGNAL", "MACD_HIST"], inplace=True)

        if len(df) < 2:
            return SignalResult("HOLD", "WEAK", "Insufficient data after MACD", strategy=self.name)

        latest = df.iloc[-1]
        prev   = df.iloc[-2]

        macd        = float(latest["MACD"])
        signal_line = float(latest["MACD_SIGNAL"])
        hist        = float(latest["MACD_HIST"])
        prev_macd   = float(prev["MACD"])
        prev_signal = float(prev["MACD_SIGNAL"])
        prev_hist   = float(prev["MACD_HIST"])
        price       = float(latest["Close"])

        indicators = {
            "MACD":        round(macd, 4),
            "MACD_Signal": round(signal_line, 4),
            "MACD_Hist":   round(hist, 4),
            "Price":       round(price, 2),
        }

        # Bullish crossover: MACD crosses above signal
        bullish_cross = prev_macd <= prev_signal and macd > signal_line
        # Bearish crossover: MACD crosses below signal
        bearish_cross = prev_macd >= prev_signal and macd < signal_line

        if bullish_cross:
            # Extra strength if histogram is rising
            hist_rising = hist > prev_hist
            strength = "STRONG" if (macd < 0 and hist_rising) else "MODERATE"
            reason = (
                f"MACD Bullish Crossover: MACD ({round(macd,4)}) crossed "
                f"above signal line ({round(signal_line,4)}). "
                f"Histogram: {round(hist,4)} "
                f"({'rising — momentum building' if hist_rising else 'watch for confirmation'})."
            )
            return SignalResult("BUY", strength, reason, indicators, self.name)

        if bearish_cross:
            hist_falling = hist < prev_hist
            strength = "STRONG" if (macd > 0 and hist_falling) else "MODERATE"
            reason = (
                f"MACD Bearish Crossover: MACD ({round(macd,4)}) crossed "
                f"below signal line ({round(signal_line,4)}). "
                f"Histogram: {round(hist,4)} "
                f"({'falling — momentum weakening' if hist_falling else 'watch for confirmation'})."
            )
            return SignalResult("SELL", strength, reason, indicators, self.name)

        position = "above" if macd > signal_line else "below"
        return SignalResult(
            "HOLD", "WEAK",
            f"MACD {position} signal line. No crossover.",
            indicators, self.name
        )


# ============================================================
# 6. VOLUME BREAKOUT STRATEGY
# ============================================================

class VolumeBreakoutStrategy(BaseStrategy):
    """
    Identifies price breakouts confirmed by high volume.
    Buy when price breaks above 20-period high with volume surge.
    Sell when price breaks below 20-period low with volume surge.
    """
    name        = "Volume Breakout"
    description = (
        "Identifies genuine price breakouts confirmed by volume. "
        "BUY when price breaks above 20-period high AND volume is "
        "1.5x above average (confirms institutional buying). "
        "SELL when price breaks below 20-period low with volume surge. "
        "Filters out false breakouts caused by low-volume moves."
    )

    LOOKBACK     = 20
    VOL_MULTIPLE = 1.5

    def generate_signal(self, df: pd.DataFrame) -> SignalResult:
        if len(df) < self.LOOKBACK + 5:
            return SignalResult("HOLD", "WEAK", "Insufficient data", strategy=self.name)

        df = add_volume_analysis(df, window=self.LOOKBACK)
        df.dropna(subset=["VOL_MA"], inplace=True)

        if len(df) < self.LOOKBACK:
            return SignalResult("HOLD", "WEAK", "Insufficient data after volume calc", strategy=self.name)

        latest    = df.iloc[-1]
        price     = float(latest["Close"])
        volume    = float(latest.get("Volume", 0))
        vol_ma    = float(latest.get("VOL_MA", 0))
        vol_ratio = float(latest.get("VOL_RATIO", 0))

        # Lookback high/low (excluding current candle)
        lookback_df  = df.iloc[-(self.LOOKBACK+1):-1]
        period_high  = float(lookback_df["High"].max())
        period_low   = float(lookback_df["Low"].min())

        indicators = {
            "Price":       round(price, 2),
            "Period_High": round(period_high, 2),
            "Period_Low":  round(period_low, 2),
            "Vol_Ratio":   round(vol_ratio, 2),
            "Vol_MA":      round(vol_ma, 0),
        }

        vol_confirmed = vol_ratio >= self.VOL_MULTIPLE

        # Bullish breakout: price breaks above 20-period high
        if price > period_high and vol_confirmed:
            breakout_pct = round((price - period_high) / period_high * 100, 2)
            strength = "STRONG" if vol_ratio >= 2.0 else "MODERATE"
            reason = (
                f"Bullish breakout: Price ₹{round(price,2)} broke above "
                f"{self.LOOKBACK}-period high ₹{round(period_high,2)} "
                f"(+{breakout_pct}%). Volume {round(vol_ratio,1)}x above average "
                f"confirms institutional participation."
            )
            return SignalResult("BUY", strength, reason, indicators, self.name)

        # Bearish breakdown: price breaks below 20-period low
        if price < period_low and vol_confirmed:
            breakdown_pct = round((period_low - price) / period_low * 100, 2)
            strength = "STRONG" if vol_ratio >= 2.0 else "MODERATE"
            reason = (
                f"Bearish breakdown: Price ₹{round(price,2)} broke below "
                f"{self.LOOKBACK}-period low ₹{round(period_low,2)} "
                f"(-{breakdown_pct}%). Volume {round(vol_ratio,1)}x above average "
                f"confirms selling pressure."
            )
            return SignalResult("SELL", strength, reason, indicators, self.name)

        if price > period_high and not vol_confirmed:
            return SignalResult(
                "HOLD", "WEAK",
                f"Price broke period high but volume insufficient "
                f"({round(vol_ratio,1)}x — needs {self.VOL_MULTIPLE}x). "
                f"Possible false breakout.",
                indicators, self.name
            )

        return SignalResult(
            "HOLD", "WEAK",
            f"Price ₹{round(price,2)} within range "
            f"[₹{round(period_low,2)} – ₹{round(period_high,2)}]",
            indicators, self.name
        )


# ============================================================
# STRATEGY REGISTRY
# ============================================================

STRATEGIES = {
    "RSI Reversal":          RSIReversalStrategy(),
    "RSI + Pivot Confluence": RSIPivotStrategy(),
    "Bollinger Bands":       BollingerStrategy(),
    "EMA Crossover":         EMACrossoverStrategy(),
    "MACD":                  MACDStrategy(),
    "Volume Breakout":       VolumeBreakoutStrategy(),
}

STRATEGY_NAMES = list(STRATEGIES.keys())


def get_strategy(name: str) -> BaseStrategy:
    """Returns strategy instance by name."""
    if name not in STRATEGIES:
        raise ValueError(f"Unknown strategy: {name}. Available: {STRATEGY_NAMES}")
    return STRATEGIES[name]

class VolumeSpikeStrategy(BaseStrategy):
    """
    Volume Spike Strategy — Jwala's latest spec (18-Jun-2026):

    Condition: Current candle volume > 500% (5x) of the previous
               14-candle average volume.
    Signal   : BUY — institutional buying detected.
    Strength : STRONG if volume >= 1000% (10x), else MODERATE.

    Jwala's insight from Business Standard research:
    "5 minute volume if it is greater than 500% of the fourteen
     previous 5 minute volume average — buy."
    "From past 20 days tracking — 8 out of 10 stocks rise on that day.
     Some rising by 20% on same day, 4-5% next day."

    This is an INDEPENDENT strategy — does not require RSI.
    It also reinforces RSI: RSI reversal + volume spike = stronger BUY.
    """

    name = "Volume Spike"
    description = (
        "Detects institutional buying via abnormal volume surge. "
        "Signals BUY when current candle volume exceeds 500% (5x) of "
        "the previous 14-candle average. Based on Jwala's spec."
    )

    # 14 candles average (excluding the current candle)
    LOOKBACK_CANDLES   = 14
    SPIKE_THRESHOLD    = 5.0    # 500% = 5x average  → BUY trigger
    STRONG_THRESHOLD   = 10.0   # 1000% = 10x average → STRONG

    def generate_signal(self, df) -> "SignalResult":
        if df is None or df.empty or len(df) < self.LOOKBACK_CANDLES + 1:
            return SignalResult(
                "HOLD", "WEAK",
                f"Insufficient data (need {self.LOOKBACK_CANDLES + 1}+ candles)",
                strategy=self.name,
            )

        if "Volume" not in df.columns:
            return SignalResult(
                "HOLD", "WEAK",
                "Volume data not available",
                strategy=self.name,
            )

        try:
            df = df.copy()
            df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce")
            df.dropna(subset=["Volume"], inplace=True)

            if len(df) < self.LOOKBACK_CANDLES + 1:
                return SignalResult("HOLD", "WEAK", "Insufficient volume data", strategy=self.name)

            # 2-week average (excluding current candle)
            avg_volume   = float(df["Volume"].iloc[-self.LOOKBACK_CANDLES-1:-1].mean())
            curr_volume  = float(df["Volume"].iloc[-1])
            curr_close   = float(df["Close"].iloc[-1])

            if avg_volume <= 0:
                return SignalResult("HOLD", "WEAK", "Zero average volume", strategy=self.name)

            volume_ratio = curr_volume / avg_volume  # e.g. 5.0 = 500%
            volume_pct   = round(volume_ratio * 100, 0)  # e.g. 500%

            indicators = {
                "Volume":       int(curr_volume),
                "Avg_Volume":   int(avg_volume),
                "Volume_Ratio": round(volume_ratio, 2),
                "Volume_Pct":   volume_pct,
                "Close":        round(curr_close, 2),
            }

            if volume_ratio >= self.SPIKE_THRESHOLD:
                strength = "STRONG" if volume_ratio >= self.STRONG_THRESHOLD else "MODERATE"
                reason = (
                    f"VOLUME SPIKE: {volume_pct:.0f}% of 2-week average "
                    f"({int(curr_volume):,} vs avg {int(avg_volume):,}). "
                    f"Institutional buying detected."
                )
                return SignalResult("BUY", strength, reason, indicators, self.name)

            return SignalResult(
                "HOLD", "WEAK",
                f"Volume {volume_pct:.0f}% of average (need 500%+). "
                f"Current: {int(curr_volume):,} | Avg: {int(avg_volume):,}",
                indicators, self.name,
            )

        except Exception as e:
            return SignalResult("HOLD", "WEAK", f"Volume calculation error: {e}", strategy=self.name)
# ============================================================
# REGISTER LATE-DEFINED STRATEGIES
# VolumeSpikeStrategy is defined after the STRATEGIES dict above,
# so it is registered here. This makes it selectable in the
# dashboard and runnable by the engine like any other strategy.
# ============================================================

STRATEGIES["Volume Spike"] = VolumeSpikeStrategy()
STRATEGY_NAMES = list(STRATEGIES.keys())
