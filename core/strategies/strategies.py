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
    Buy when RSI bounces back above 20 after being oversold AND price
    is above its 50-period Simple Moving Average (trend filter).
    Sell when RSI drops back below 80 after being overbought AND
    price is below its 50-period SMA. Requires 2 consecutive
    confirmation candles (unchanged from before this revision).

    MA TREND FILTER — Jwala, Jul 22 call: "the other part for RSI
    that you have already done... we just need to add this
    particular [the MA]. So this is like a filter for if market is
    rising, then we will try to catch at low price or at low RSI."
    Rationale, same-day WhatsApp message: "RSI alone fires 'buy'
    signals even in downtrends (oversold can stay oversold). Adding
    an MA filters those out — you only take the RSI signal when it
    agrees with the broader trend." His own description of the
    average ("look at one candle... average price for the candle...
    sum up for last 50 candles and divide by 50") is the Simple
    Moving Average of Close price — implemented that way, computed
    directly here rather than via a shared indicators helper (no
    add_sma() existed to reuse).

    RSI TRIGGER LEVELS — widened from 35/75 to 20/80 (Jwala, follow-up
    message: "rsi should be 80 and 20") — fewer, higher-conviction
    signals. (35/75 was itself already a widened version of an
    original, simpler 30/70 — this is the second widening.)
    """
    name        = "RSI Reversal"
    description = (
        "Identifies momentum reversals using RSI 14, filtered by a "
        "50-period SMA trend filter. BUY when RSI recovers from "
        "oversold (<20) with price above its 50-SMA. SELL when RSI "
        "drops from overbought (>80) with price below its 50-SMA."
    )

    RSI_OVERSOLD   = 20   # was 35 — Jwala: "rsi should be 80 and 20"
    RSI_OVERBOUGHT = 80   # was 75
    # [JUDGMENT CALL — NEEDS CONFIRMATION] STRONG-grade cutoff: Jwala
    # gave the 20/80 trigger levels but not a separate STRONG cutoff.
    # Kept a clean symmetric 5-point gap from the new trigger.
    RSI_STRONG_OVERSOLD   = 15
    RSI_STRONG_OVERBOUGHT = 85
    MA_PERIOD = 50

    def generate_signal(self, df: pd.DataFrame) -> SignalResult:
        if len(df) < max(20, self.MA_PERIOD):
            return SignalResult("HOLD", "WEAK", "Insufficient data", strategy=self.name)

        # 50-period Simple Moving Average — computed on the FULL
        # dataframe BEFORE the RSI dropna below truncates it. Getting
        # this order backwards (MA after the RSI dropna) means
        # rolling(50) never has 50 real rows to work with regardless
        # of how much data was actually passed in — caught by testing
        # with a dataframe that had plenty of history and still got
        # "insufficient data" every time.
        df["MA50"] = df["Close"].rolling(self.MA_PERIOD).mean()

        df = add_rsi(df)
        df.dropna(subset=["RSI"], inplace=True)

        if len(df) < 3:
            return SignalResult("HOLD", "WEAK", "Insufficient RSI data", strategy=self.name)

        if pd.isna(df["MA50"].iloc[-1]):
            return SignalResult("HOLD", "WEAK", "Insufficient data for 50-MA", strategy=self.name)

        current  = float(df["RSI"].iloc[-1])
        prev     = float(df["RSI"].iloc[-2])
        prev2    = float(df["RSI"].iloc[-3])
        price    = float(df["Close"].iloc[-1])
        ma50     = float(df["MA50"].iloc[-1])
        above_ma = price > ma50
        below_ma = price < ma50

        indicators = {
            "RSI": round(current, 2),
            "RSI_prev": round(prev, 2),
            "Price": round(price, 2),
            "MA50": round(ma50, 2),
            "Above_MA50": above_ma,
        }

        rsi_buy_pattern  = prev2 < self.RSI_OVERSOLD and prev > prev2 and current > prev and current > self.RSI_OVERSOLD
        rsi_sell_pattern = prev2 > self.RSI_OVERBOUGHT and prev < prev2 and current < prev and current < self.RSI_OVERBOUGHT

        # BUY: RSI bounced from oversold AND price above trend filter
        if rsi_buy_pattern and above_ma:
            strength = "STRONG" if prev2 < self.RSI_STRONG_OVERSOLD else "MODERATE"
            reason = (
                f"RSI recovered from oversold zone ({round(prev2,1)} -> {round(current,1)}), "
                f"price ₹{round(price,2)} above 50-MA ₹{round(ma50,2)} (uptrend confirmed). "
                f"Two consecutive up candles confirm reversal."
            )
            return SignalResult("BUY", strength, reason, indicators, self.name)

        # SELL: RSI reversed from overbought AND price below trend filter
        if rsi_sell_pattern and below_ma:
            strength = "STRONG" if prev2 > self.RSI_STRONG_OVERBOUGHT else "MODERATE"
            reason = (
                f"RSI reversed from overbought zone ({round(prev2,1)} -> {round(current,1)}), "
                f"price ₹{round(price,2)} below 50-MA ₹{round(ma50,2)} (downtrend confirmed). "
                f"Two consecutive down candles confirm reversal."
            )
            return SignalResult("SELL", strength, reason, indicators, self.name)

        # Near-misses where RSI reversed but the MA filter blocked it —
        # visible in the HOLD reason so the filter's effect can be
        # validated (Jwala, Jul 22: "the overall quality that we are
        # generating sell signals while the stock is rising, that
        # probably we can check").
        if rsi_buy_pattern and not above_ma:
            return SignalResult("HOLD", "WEAK",
                f"RSI reversal from oversold confirmed but price ₹{round(price,2)} "
                f"is below 50-MA ₹{round(ma50,2)} — blocked by trend filter",
                indicators, self.name)
        if rsi_sell_pattern and not below_ma:
            return SignalResult("HOLD", "WEAK",
                f"RSI reversal from overbought confirmed but price ₹{round(price,2)} "
                f"is above 50-MA ₹{round(ma50,2)} — blocked by trend filter",
                indicators, self.name)

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
    Volume Spike Strategy — updated per Jwala Jul 11 fix.

    Bug found on the call: the original single-candle check fired
    even "when the price is falling, there also volume spikes" — a
    volume surge alone doesn't distinguish institutional accumulation
    from a spike into a sell-off. Fix, per the call:
      "we can include more aspects into it, like we will check for
       three candles, 3 volume candles. And each of them should be
       more than 500%[→2000%]... and also we would keep checking the
       price candles also for these three volume candles. So price
       should be increasing... we would want to get in after at
       least two, 3 candles so that we know that we are entering in
       a buying spree."

    Condition (all three required):
      1. Each of the last CONFIRM_CANDLES candles has volume >=
         SPIKE_THRESHOLD × a SINGLE shared baseline average — the
         LOOKBACK_CANDLES candles immediately before the confirmation
         window (not each candle's own drifting average — see the
         comment at baseline_window below for why that matters).
      2. Close price strictly increases across those same candles.
      3. (unchanged) BUY only — this strategy detects buying
         interest, never generates SELL.

    Threshold raised back from the 500% TESTING value to Jwala's
    original spec ("previously you suggested it to be like around
    2000"). STRONG_THRESHOLD is my own choice (not restated on this
    call) — set meaningfully above the new trigger rather than left
    below it, which the old 500/1000% pairing effectively was once
    the trigger moves to 2000%. Flag if you want a different STRONG
    cutoff.

    This is an INDEPENDENT strategy — does not require RSI.
    It also reinforces RSI: RSI reversal + volume spike = stronger BUY.
    """

    name = "Volume Spike"
    description = (
        "Detects institutional buying via a 3-candle abnormal-volume "
        "confirmation with price rising throughout — not a single "
        "spike, which can occur even as price falls. Based on Jwala's "
        "Jul 11 spec."
    )

    LOOKBACK_CANDLES  = 14    # trailing average window (unchanged)
    CONFIRM_CANDLES   = 3     # consecutive candles required (Jwala Jul 11)
    SPIKE_THRESHOLD   = 20.0  # 2000% = 20x average → BUY trigger (Jwala's original spec, was 500% for testing)
    STRONG_THRESHOLD  = 50.0  # 5000% = 50x average → STRONG (my choice — meaningfully above the new trigger)

    def generate_signal(self, df) -> "SignalResult":
        # Need enough history for one LOOKBACK_CANDLES baseline window
        # PLUS the CONFIRM_CANDLES confirmation window sitting after it.
        need = self.LOOKBACK_CANDLES + self.CONFIRM_CANDLES
        if df is None or df.empty or len(df) < need:
            return SignalResult(
                "HOLD", "WEAK",
                f"Insufficient data (need {need}+ candles)",
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

            if len(df) < need:
                return SignalResult("HOLD", "WEAK", "Insufficient volume data", strategy=self.name)

            # ONE fixed baseline average — the LOOKBACK_CANDLES candles
            # strictly BEFORE the CONFIRM_CANDLES confirmation window —
            # not each candle's own independently-drifting trailing
            # average. This matters: if each of the 3 confirmation
            # candles used its own rolling average, the 2nd and 3rd
            # candles' averages would already include the 1st (and
            # 2nd) spike candles, inflating the denominator and making
            # a genuine 3-candle spree progressively HARDER to confirm
            # the deeper into it you are — the opposite of what a
            # "buying spree" detector should do. Caught by testing a
            # real 3-candle spike pattern, not assumed.
            baseline_window = df.iloc[-(self.LOOKBACK_CANDLES + self.CONFIRM_CANDLES):-self.CONFIRM_CANDLES]
            if len(baseline_window) < self.LOOKBACK_CANDLES:
                return SignalResult("HOLD", "WEAK", "Insufficient baseline window", strategy=self.name)

            avg_volume = float(baseline_window["Volume"].mean())
            if avg_volume <= 0:
                return SignalResult("HOLD", "WEAK", "Zero average volume", strategy=self.name)

            confirm_window = df.iloc[-self.CONFIRM_CANDLES:]
            ratios = (confirm_window["Volume"] / avg_volume).tolist()
            closes = confirm_window["Close"].astype(float).tolist()

            curr_volume  = float(df["Volume"].iloc[-1])
            curr_close   = float(df["Close"].iloc[-1])
            volume_ratio = ratios[-1]
            volume_pct   = round(volume_ratio * 100, 0)

            indicators = {
                "Volume":         int(curr_volume),
                "Avg_Volume":     int(avg_volume) if avg_volume == avg_volume else 0,  # NaN-safe
                "Volume_Ratio":   round(volume_ratio, 2),
                "Volume_Pct":     volume_pct,
                "Close":          round(curr_close, 2),
                "Confirm_Ratios": [round(r, 2) for r in ratios],
                "Confirm_Closes": [round(c, 2) for c in closes],
            }

            all_above_threshold = all(r >= self.SPIKE_THRESHOLD for r in ratios)
            price_rising = all(closes[i] < closes[i + 1] for i in range(len(closes) - 1))

            if all_above_threshold and price_rising:
                strength = "STRONG" if volume_ratio >= self.STRONG_THRESHOLD else "MODERATE"
                reason = (
                    f"VOLUME SPIKE confirmed over {self.CONFIRM_CANDLES} candles: "
                    f"volume {volume_pct:.0f}% of {self.LOOKBACK_CANDLES}-candle average, "
                    f"each of the last {self.CONFIRM_CANDLES} candles above "
                    f"{self.SPIKE_THRESHOLD*100:.0f}%, price rising throughout "
                    f"(₹{closes[0]:.2f} → ₹{closes[-1]:.2f}). "
                    f"Institutional buying spree detected, not a single spike."
                )
                return SignalResult("BUY", strength, reason, indicators, self.name)

            if all_above_threshold and not price_rising:
                return SignalResult(
                    "HOLD", "WEAK",
                    f"Volume above {self.SPIKE_THRESHOLD*100:.0f}% threshold on all "
                    f"{self.CONFIRM_CANDLES} candles, but price did NOT rise "
                    f"throughout (₹{closes[0]:.2f} → ₹{closes[-1]:.2f}) — likely a "
                    f"spike into a falling price, not accumulation. Skipped "
                    f"(Jul 11 fix for exactly this false-positive pattern).",
                    indicators, self.name,
                )

            return SignalResult(
                "HOLD", "WEAK",
                f"Volume {volume_pct:.0f}% of average (need "
                f"{self.SPIKE_THRESHOLD*100:.0f}%+ on all of the last "
                f"{self.CONFIRM_CANDLES} candles). "
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