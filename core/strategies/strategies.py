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
    add_ema, add_macd, add_volume_analysis, add_atr,
    get_nearest_level, get_ema_trend, is_volume_confirmed,
)


# ============================================================
# 1. RSI REVERSAL STRATEGY
# ============================================================

class RSIReversalStrategy(BaseStrategy):
    """
    REPLACES the fixed 2-candle confirmation (Jwala, Jul 23, in direct
    reply to the finding that RSI was reaching 20/80 regularly but the
    2-candle window almost never caught it): "If rsi falls below 20 we
    start tracking the stock, Now if Price crosses 50 MA then buy. It
    can [take] multiple candles so that rsi starts to rise and also
    the price."

    NEW logic:
      1. TRACK — the moment RSI dips below 20 (or above 80 for sell),
         that starts an open-ended "watch", not a fixed 2-candle rule.
      2. TRIGGER — while that watch holds and RSI is rising (BUY) /
         falling (SELL), the signal fires the moment price actually
         CROSSES its 50-MA (prev candle at/below it, current candle
         above it — not merely "is currently above/below", the real
         crossing candle). This is what makes it fire only ONCE per
         setup without needing extra "already fired" bookkeeping —
         candles after the cross are no longer a fresh crossover.
      3. Deliberately unbounded in candle count, per Jwala's own
         "it can be multiple candles" — TRACKING_WINDOW below is an
         engineering cap on how far back a dip still counts as "the
         same setup", not a rule Jwala specified a number for.

    Implemented WITHOUT new persistent state — generate_signal()
    already gets the full historical dataframe each call, so "was
    there an unresolved dip recently" is answered by looking back
    through THIS SAME dataframe, not by remembering anything between
    scan cycles.

    [JUDGMENT CALL — NEEDS CONFIRMATION] TRACKING_WINDOW = 10 candles:
    Jwala said "multiple candles" with no exact upper bound. 10 is an
    engineering default — a dip further back than this is treated as
    stale / a different setup. Flag if a different number is intended.
    """
    name        = "RSI + MA"  # was "RSI Reversal" (Jwala, Jul 24: "old ones say
                               # rsi new ones say rsi+ma" — forward-only rename,
                               # historical trades keep the old label, no backfill)
    description = (
        "Tracks a stock once RSI dips below 20 (or rises above 80), "
        "then fires BUY/SELL the moment price crosses its 50-period "
        "SMA while RSI is recovering in that direction. Can take "
        "several candles — not a fixed 2-candle window."
    )

    RSI_OVERSOLD   = 20
    RSI_OVERBOUGHT = 80
    # [JUDGMENT CALL — NEEDS CONFIRMATION] unchanged from the previous
    # revision — Jwala hasn't specified a separate STRONG cutoff.
    RSI_STRONG_OVERSOLD   = 15
    RSI_STRONG_OVERBOUGHT = 85
    MA_PERIOD = 50
    TRACKING_WINDOW = 10   # [JUDGMENT CALL] see docstring above

    def generate_signal(self, df: pd.DataFrame) -> SignalResult:
        need = self.MA_PERIOD + self.TRACKING_WINDOW
        if len(df) < need:
            return SignalResult("HOLD", "WEAK", "Insufficient data", strategy=self.name)

        # MA50 computed on the FULL frame before the RSI dropna
        # truncates it (see earlier fix note — getting this order
        # backwards means rolling(50) never finds 50 real rows).
        df["MA50"] = df["Close"].rolling(self.MA_PERIOD).mean()

        df = add_rsi(df)
        df.dropna(subset=["RSI"], inplace=True)

        if len(df) < self.TRACKING_WINDOW + 2:
            return SignalResult("HOLD", "WEAK", "Insufficient RSI data", strategy=self.name)

        if pd.isna(df["MA50"].iloc[-1]):
            return SignalResult("HOLD", "WEAK", "Insufficient data for 50-MA", strategy=self.name)

        window = self.TRACKING_WINDOW + 1
        rsi_series   = df["RSI"].tail(window).reset_index(drop=True)
        price_series = df["Close"].tail(window).reset_index(drop=True)
        ma_series    = df["MA50"].tail(window).reset_index(drop=True)

        current    = float(rsi_series.iloc[-1])
        prev_rsi   = float(rsi_series.iloc[-2])
        price      = float(price_series.iloc[-1])
        prev_price = float(price_series.iloc[-2])
        ma50       = float(ma_series.iloc[-1])
        prev_ma50  = float(ma_series.iloc[-2])

        indicators = {
            "RSI": round(current, 2),
            "RSI_prev": round(prev_rsi, 2),
            "Price": round(price, 2),
            "MA50": round(ma50, 2),
            "Above_MA50": price > ma50,
        }

        # Was there a qualifying dip/rise anywhere in the tracking
        # window BEFORE the current candle?
        prior_rsi = rsi_series.iloc[:-1]
        recent_oversold_dip    = (prior_rsi < self.RSI_OVERSOLD).any()
        recent_overbought_rise = (prior_rsi > self.RSI_OVERBOUGHT).any()
        deepest_dip  = float(prior_rsi.min()) if recent_oversold_dip else None
        highest_rise = float(prior_rsi.max()) if recent_overbought_rise else None

        rsi_rising  = current > prev_rsi
        rsi_falling = current < prev_rsi

        crossed_above_ma = prev_price <= prev_ma50 and price > ma50
        crossed_below_ma = prev_price >= prev_ma50 and price < ma50

        # BUY: was tracking an oversold dip, RSI now rising, price just crossed above the 50-MA
        if recent_oversold_dip and rsi_rising and crossed_above_ma:
            strength = "STRONG" if deepest_dip < self.RSI_STRONG_OVERSOLD else "MODERATE"
            reason = (
                f"Tracked oversold dip (RSI reached {round(deepest_dip,1)}), RSI now rising "
                f"({round(prev_rsi,1)} -> {round(current,1)}), price just crossed above its "
                f"50-MA (₹{round(prev_price,2)} -> ₹{round(price,2)}, MA ₹{round(ma50,2)})."
            )
            return SignalResult("BUY", strength, reason, indicators, self.name)

        # SELL: was tracking an overbought rise, RSI now falling, price just crossed below the 50-MA
        if recent_overbought_rise and rsi_falling and crossed_below_ma:
            strength = "STRONG" if highest_rise > self.RSI_STRONG_OVERBOUGHT else "MODERATE"
            reason = (
                f"Tracked overbought rise (RSI reached {round(highest_rise,1)}), RSI now falling "
                f"({round(prev_rsi,1)} -> {round(current,1)}), price just crossed below its "
                f"50-MA (₹{round(prev_price,2)} -> ₹{round(price,2)}, MA ₹{round(ma50,2)})."
            )
            return SignalResult("SELL", strength, reason, indicators, self.name)

        # Informative near-miss reasons — same purpose the old "blocked
        # by trend filter" message served: makes it possible to verify
        # the tracking logic is doing real work, not just returning HOLD.
        if recent_oversold_dip and rsi_rising and not crossed_above_ma:
            return SignalResult("HOLD", "WEAK",
                f"Tracking oversold dip (RSI reached {round(deepest_dip,1)}), RSI rising, "
                f"price ₹{round(price,2)} hasn't crossed its 50-MA (₹{round(ma50,2)}) yet",
                indicators, self.name)
        if recent_overbought_rise and rsi_falling and not crossed_below_ma:
            return SignalResult("HOLD", "WEAK",
                f"Tracking overbought rise (RSI reached {round(highest_rise,1)}), RSI falling, "
                f"price ₹{round(price,2)} hasn't crossed its 50-MA (₹{round(ma50,2)}) yet",
                indicators, self.name)

        return SignalResult("HOLD", "WEAK", f"RSI at {round(current,1)} — no active tracking setup", indicators, self.name)


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
            # `price` is the denominator here too -- a bad/zero tick (a
            # real occurrence around broker data gaps) threw an unguarded
            # ZeroDivisionError straight out of generate_signal, unlike
            # every other strategy in this file, which degrades to HOLD
            # on bad data instead (found in the 2026-08-25 audit).
            if level_price <= 0 or price <= 0:
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

    # Floor below which BB%B is numerically unreliable, not a real signal
    # (found in the 2026-08-25 audit): BB%B = (price-lower)/(upper-lower),
    # so once the band width collapses toward zero (flat/stale price —
    # this codebase has a documented history of stale-feed bugs), a
    # single-tick move can swing %B past 1.0 or below 0.0 with no real
    # price movement behind it. Deliberately well below the 0.1% squeeze
    # threshold used below, so genuine tight-but-real squeeze setups
    # still fire — this only rejects near-zero/degenerate width.
    MIN_BAND_WIDTH_PCT = 0.05

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

        if bb_width < self.MIN_BAND_WIDTH_PCT:
            return SignalResult(
                "HOLD", "WEAK",
                f"Bands too collapsed for a reliable BB%B reading "
                f"(width={round(bb_width,4)}% < {self.MIN_BAND_WIDTH_PCT}% floor) — "
                f"likely a flat/stale price, not a real squeeze.",
                indicators, self.name,
            )

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
        prev2  = df.iloc[-3] if len(df) >= 3 else None

        macd        = float(latest["MACD"])
        signal_line = float(latest["MACD_SIGNAL"])
        hist        = float(latest["MACD_HIST"])
        prev_macd   = float(prev["MACD"])
        prev_signal = float(prev["MACD_SIGNAL"])
        prev_hist   = float(prev["MACD_HIST"])
        prev2_hist  = float(prev2["MACD_HIST"]) if prev2 is not None else None
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
            # "hist > prev_hist" is ALWAYS true immediately after ANY
            # bullish crossover -- MACD_HIST is defined as MACD-SIGNAL,
            # so prev_macd<=prev_signal implies prev_hist<=0, and
            # macd>signal_line implies hist>0, making hist>prev_hist a
            # tautology, not a confirmation (found in the 2026-08-25
            # audit — proven with zero counterexamples across synthetic
            # crossovers). Real confirmation instead checks whether the
            # histogram was ALREADY trending up going into this candle
            # (prev_hist > prev2_hist) — momentum building before the
            # cross confirmed, not a restatement of the cross itself.
            hist_rising = prev2_hist is not None and prev_hist > prev2_hist
            strength = "STRONG" if (macd < 0 and hist_rising) else "MODERATE"
            reason = (
                f"MACD Bullish Crossover: MACD ({round(macd,4)}) crossed "
                f"above signal line ({round(signal_line,4)}). "
                f"Histogram: {round(hist,4)} "
                f"({'rising — momentum building' if hist_rising else 'watch for confirmation'})."
            )
            return SignalResult("BUY", strength, reason, indicators, self.name)

        if bearish_cross:
            # Mirror of the bullish-side fix above — see that comment.
            hist_falling = prev2_hist is not None and prev_hist < prev2_hist
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
    "RSI + MA":              RSIReversalStrategy(),  # registry key renamed to match
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

# ============================================================
# 3 BAR PLAY — Jwala, Jul 24 email
#
# Reference: https://www.youtube.com/watch?v=xEjUd82NVVg
# Pattern (Jwala's own written spec):
#   Bar 1 (igniting)  — wide-range candle, strong volume, clear
#     directional move.
#   Bar 2 (pullback, sometimes + Bar 3) — small/narrow range, must
#     NOT retrace more than 50% of bar 1's range. Highs roughly
#     equal for longs / lows roughly equal for shorts.
#   Final bar (trigger) — breaks above the pullback high (long) or
#     below the pullback low (short) = entry.
#
# Pattern-detection logic below follows Jwala's own attached
# reference Python almost exactly (detect_3_bar_play /
# apply_risk_reward). One real discrepancy between his written spec
# and his own code, resolved in favor of the SPEC: his bullet list
# explicitly calls for a volume filter ("only take signals where bar
# 1 volume is above average, e.g. 20-period avg") but his reference
# code doesn't implement it at all. Added here to match what he
# actually asked for, not just what the code happened to do.
# ============================================================

class ThreeBarPlayStrategy(BaseStrategy):
    """
    Detects an "igniting" bar + shallow pullback + breakout trigger —
    a 3-bar continuation pattern — filtered by above-average volume
    on the igniting bar (Jwala's spec; not in his reference code, see
    module note above).

    [JUDGMENT CALL — NEEDS CONFIRMATION] Two things Jwala's spec left
    open, defaulted here rather than guessed silently:
      - REWARD_MULTIPLE: spec says "2x-3x the risk, configurable" —
        no single number given. Defaulted to 2.0 (the conservative
        end), matching his own reference code's example usage
        (`apply_risk_reward(signals, reward_multiple=2)`).
      - Strength grading: no spec given at all for STRONG vs
        MODERATE. Defaulted: STRONG if bar 1's volume is >= 2x its
        20-period average, else MODERATE.

    RESOLVED (Jul 24): this pattern uses its OWN natural stop-loss —
    the pullback bar's opposite extreme ("Pattern_Stop" below, exposed
    via `indicators`) — rather than RMS's generic 1%-of-entry stop.
    strategy_engine.py's _run_paper_trading() extracts "Pattern_Stop"
    from indicators and passes it into RMS.evaluate() as custom_stop,
    which uses it directly as the real stop-loss and recomputes target
    from the ACTUAL fill price (see rms.py's evaluate() docstring).
    "Pattern_Target" and "Pattern_Entry" below stay reference/
    informational only — they reflect the pattern's own theoretical
    numbers (computed against bar2's high/low), not the real fill
    price, which is why RMS recalculates the real target itself rather
    than trusting these directly.
    """
    # Relabeled "Experiment 3 Bar Play" (was "3 Bar Play") — client
    # feedback: this pattern's calibration is unproven ("miscalculated
    # but also working fine"), so it's kept running exactly as-is but
    # now clearly marked experimental. The plain "3 Bar Play" name is
    # freed up for ThreeBarContractionStrategy below. Old historical
    # signals/paper_positions rows keep the literal string '3 Bar Play'
    # — not backfilled, same forward-only precedent as the earlier
    # "RSI Reversal" -> "RSI + MA" rename.
    name = "Experiment 3 Bar Play"
    description = (
        "Detects an igniting bar + shallow pullback + breakout "
        "trigger, filtered by above-average volume on the igniting "
        "bar. Intended mainly for 1-10 min charts near market open. "
        "Experimental — calibration not yet validated."
    )

    RETRACE_LIMIT        = 0.5   # pullback can't retrace >50% of bar1's range
    VOLUME_LOOKBACK      = 20    # 20-period average, per spec
    REWARD_MULTIPLE      = 2.0   # [JUDGMENT CALL] see docstring
    STRONG_VOLUME_MULTIPLE = 2.0 # [JUDGMENT CALL] see docstring

    def generate_signal(self, df: pd.DataFrame) -> SignalResult:
        need = self.VOLUME_LOOKBACK + 3
        if df is None or df.empty or len(df) < need:
            return SignalResult("HOLD", "WEAK", f"Insufficient data (need {need}+ candles)", strategy=self.name)

        if "Volume" not in df.columns:
            return SignalResult("HOLD", "WEAK", "Volume data not available", strategy=self.name)

        try:
            df = df.copy()
            df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce")
            df.dropna(subset=["Volume"], inplace=True)
            if len(df) < need:
                return SignalResult("HOLD", "WEAK", "Insufficient volume data", strategy=self.name)

            bar1 = df.iloc[-3]   # igniting bar
            bar2 = df.iloc[-2]   # pullback bar
            bar3 = df.iloc[-1]   # trigger bar

            bar1_range = float(bar1["High"] - bar1["Low"])
            if bar1_range <= 0:
                return SignalResult("HOLD", "WEAK", "Zero-range igniting bar", strategy=self.name)

            # Volume filter (Jwala's spec, not his reference code) —
            # bar1's volume vs the 20-period average from the candles
            # strictly BEFORE bar1 (excludes bar1 itself).
            baseline_window = df["Volume"].iloc[-(self.VOLUME_LOOKBACK + 3):-3]
            if len(baseline_window) < self.VOLUME_LOOKBACK:
                return SignalResult("HOLD", "WEAK", "Insufficient volume baseline", strategy=self.name)

            avg_volume   = float(baseline_window.mean())
            bar1_volume  = float(bar1["Volume"])
            volume_ratio = bar1_volume / avg_volume if avg_volume > 0 else 0.0

            indicators = {
                "Bar1_Range":    round(bar1_range, 2),
                "Bar1_Volume":   int(bar1_volume),
                "Avg_Volume_20": int(avg_volume),
                "Volume_Ratio":  round(volume_ratio, 2),
            }

            if volume_ratio <= 1.0:
                return SignalResult(
                    "HOLD", "WEAK",
                    f"Igniting bar volume ({int(bar1_volume):,}) not above its 20-period "
                    f"average ({int(avg_volume):,}) — volume filter not met",
                    indicators, self.name,
                )

            is_bullish_ignite = bar1["Close"] > bar1["Open"]
            is_bearish_ignite = bar1["Close"] < bar1["Open"]

            # LONG: pullback low didn't retrace >50% of bar1's range, trigger breaks pullback high
            pullback_ok_long = (bar1["High"] - bar2["Low"]) <= self.RETRACE_LIMIT * bar1_range
            trigger_long = bar3["High"] > bar2["High"]

            if is_bullish_ignite and pullback_ok_long and trigger_long:
                entry, stop = float(bar2["High"]), float(bar2["Low"])
                risk = abs(entry - stop)
                target = entry + self.REWARD_MULTIPLE * risk
                strength = "STRONG" if volume_ratio >= self.STRONG_VOLUME_MULTIPLE else "MODERATE"
                indicators.update({"Pattern_Entry": round(entry, 2), "Pattern_Stop": round(stop, 2),
                                    "Pattern_Target": round(target, 2)})
                reason = (
                    f"Experiment 3-Bar Play LONG: igniting bar at {volume_ratio:.1f}x avg volume, "
                    f"pullback held within {self.RETRACE_LIMIT*100:.0f}% of bar1's range, "
                    f"trigger broke above pullback high (₹{bar2['High']:.2f})."
                )
                return SignalResult("BUY", strength, reason, indicators, self.name)

            # SHORT: mirror
            pullback_ok_short = (bar2["High"] - bar1["Low"]) <= self.RETRACE_LIMIT * bar1_range
            trigger_short = bar3["Low"] < bar2["Low"]

            if is_bearish_ignite and pullback_ok_short and trigger_short:
                entry, stop = float(bar2["Low"]), float(bar2["High"])
                risk = abs(entry - stop)
                target = entry - self.REWARD_MULTIPLE * risk
                strength = "STRONG" if volume_ratio >= self.STRONG_VOLUME_MULTIPLE else "MODERATE"
                indicators.update({"Pattern_Entry": round(entry, 2), "Pattern_Stop": round(stop, 2),
                                    "Pattern_Target": round(target, 2)})
                reason = (
                    f"Experiment 3-Bar Play SHORT: igniting bar at {volume_ratio:.1f}x avg volume, "
                    f"pullback held within {self.RETRACE_LIMIT*100:.0f}% of bar1's range, "
                    f"trigger broke below pullback low (₹{bar2['Low']:.2f})."
                )
                return SignalResult("SELL", strength, reason, indicators, self.name)

            return SignalResult("HOLD", "WEAK", "No qualifying 3-bar pattern on the latest bars",
                                 indicators, self.name)

        except Exception as e:
            return SignalResult("HOLD", "WEAK", f"Experiment 3-Bar Play calculation error: {e}", strategy=self.name)


STRATEGIES["Experiment 3 Bar Play"] = ThreeBarPlayStrategy()
STRATEGY_NAMES = list(STRATEGIES.keys())

# ============================================================
# 3 BAR PLAY (v2) — flag/pennant continuation
#
# REPLACES the earlier volatility-contraction ("coil") reading of
# "3 Bar Play" — that was a different pattern family (NR3-style
# squeeze) that had ended up under this name by mistake. This is
# the pattern Jwala actually walked through live (call recording,
# Aug 25) using three chart references (ETHUSD, IDEA, AAPL) as
# examples — a classic bull/bear flag:
#
#   Bar 1 (explosive) — a big, wide-range directional candle. The
#     flagpole.
#   Bar 2, optionally Bar 3 (consolidation) — one OR two small
#     candles that hover near bar 1's head: must not retrace deep
#     back into bar 1 (RETRACE_LIMIT of bar 1's range, the low
#     side), and must not already push far past bar 1's high
#     either (OVERSHOOT_LIMIT — "a few points above at most"; if
#     it already broke out convincingly here, it isn't a pause).
#     Jwala: "if this candle had become a big red candle, it would
#     have moved downwards" — a deep give-back invalidates the
#     pattern, it isn't just a bigger discount entry.
#   Trigger — the very next candle (3rd or 4th, never later —
#     "not in 5th or 6th candle") crosses BAR 1's high (long) or
#     low (short). That's the key correction vs. the continuation
#     logic in ThreeBarPlayStrategy above, which triggers off the
#     pullback bar's extreme instead of bar 1's.
#
# Stop: the far side of the consolidation bar(s) — judgment call
# (not stated on the first call), consistent with how flags are
# stopped out in practice (below/above the base of the flag, not
# the full flagpole).
#
# Target (Aug 26 follow-up call): an EXACT 70% of bar 1's range,
# projected from the breakout — not the approximate reward-ratio
# proxy used before. Exposed as "Pattern_Target_Exact" and wired
# through strategy_engine.py's _run_paper_trading() -> rms.py's
# evaluate(custom_target=...), which uses it as the literal target
# price instead of recomputing one from stop_dist x reward_ratio.
# ("Pattern_Target" is also still set, same value, kept only for
# display consistency with the Experiment strategy's convention —
# it is NOT what gets read for enforcement; Pattern_Target_Exact is.)
#
# Flagpole size (Aug 26 follow-up call): bar 1's range must be at
# least 3x the 14-period ATR (Average True Range) — Jwala's own
# words: "the flagpole candle should be a big candle, bigger than
# the average," landing on ATR specifically ("a measure of how much
# a stock normally moves... one of the most useful indicators for
# setting stop losses") after floating a hand-rolled 15/30-candle
# average first. ATR period defaults to the standard 14 (he didn't
# settle on a period once he named ATR itself; 14 matches both
# convention and his own RSI-14 analogy on the same call). This is
# a genuinely new filter — the original Aug 25 build only checked
# bar 1's VOLUME, never its actual range, against anything.
#
# Volume: this call didn't restate a volume rule, but Jwala's
# earlier written spec (used for the Experiment strategy) requires
# the explosive bar's volume above its 20-period average — kept
# here for consistency across his own "3 bar play" family, checked
# alongside (not instead of) the new ATR check. Consolidation
# volume should be lower than the explosive bar's (a real pause
# shows volume drying up, not fresh selling/buying) — the standard
# "flag" tell that distinguishes a pause from a reversal.
# ============================================================

class ThreeBarFlagStrategy(BaseStrategy):
    """
    Flag/pennant continuation: an explosive bar (the flagpole, its
    range required to be >= 3x the 14-period ATR) followed by 1-2
    small consolidation bars hovering near its high (long) or low
    (short), triggered when the next bar breaks the EXPLOSIVE bar's
    own extreme (not the consolidation bar's). Stop is the far side
    of the consolidation; target is an EXACT 70% of the flagpole's
    range, projected from the breakout.
    """
    name = "3 Bar Play"
    description = (
        "Detects an explosive (flagpole) candle -- range >= 3x ATR(14) "
        "and volume above its 20-period average -- a 1-2 candle "
        "consolidation hovering near its high/low, and a breakout of "
        "the explosive candle's own extreme within the next 1-2 "
        "candles. Target is 70% of the flagpole's range. Per Jwala's "
        "Aug 25 walkthrough and Aug 26 follow-up refinements."
    )

    VOLUME_LOOKBACK    = 20    # 20-period average, per spec
    RETRACE_LIMIT      = 0.5   # consolidation low can't dig >50% back into bar1's range
    OVERSHOOT_LIMIT    = 0.1   # consolidation high can't clear bar1's high by >10% of bar1's range
    ATR_PERIOD         = 14    # standard ATR window (see module note)
    ATR_MULTIPLE       = 3.0   # bar1's range must be >= this many ATRs (Jwala, Aug 26: "three times")
    TARGET_PCT_OF_RANGE = 0.7  # exact target = 70% of bar1's range (Jwala, Aug 26)
    STRONG_VOLUME_MULTIPLE = 2.0

    def _consolidation_ok(self, bar1_high, bar1_low, bar1_range, bar1_volume, bars, bullish: bool):
        """All of `bars` must hover near bar1's head (bullish) or floor (bearish)."""
        for b in bars:
            if float(b["Volume"]) > bar1_volume:
                return False  # a real pause shows volume drying up, not fresh participation
            if bullish:
                if (bar1_high - b["Low"]) > self.RETRACE_LIMIT * bar1_range:
                    return False
                if (b["High"] - bar1_high) > self.OVERSHOOT_LIMIT * bar1_range:
                    return False
            else:
                if (b["High"] - bar1_low) > self.RETRACE_LIMIT * bar1_range:
                    return False
                if (bar1_low - b["Low"]) > self.OVERSHOOT_LIMIT * bar1_range:
                    return False
        return True

    def generate_signal(self, df: pd.DataFrame) -> SignalResult:
        need = self.VOLUME_LOOKBACK + 4
        if df is None or df.empty or len(df) < need:
            return SignalResult("HOLD", "WEAK", f"Insufficient data (need {need}+ candles)", strategy=self.name)

        if "Volume" not in df.columns:
            return SignalResult("HOLD", "WEAK", "Volume data not available", strategy=self.name)

        try:
            df = df.copy()
            df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce")
            df.dropna(subset=["Volume"], inplace=True)
            if len(df) < need:
                return SignalResult("HOLD", "WEAK", "Insufficient volume data", strategy=self.name)

            df_atr = add_atr(df, window=self.ATR_PERIOD)

            brk = df.iloc[-1]  # breakout candidate is always the latest candle

            # Try the tighter (3-bar: 1 consolidation candle) reading first,
            # then the 4-bar (2 consolidation candles) reading — "3rd or 4th
            # candle at max", never further out.
            for n_consol, bar1_idx in ((1, -3), (2, -4)):
                bar1 = df.iloc[bar1_idx]
                consol = [df.iloc[i] for i in range(bar1_idx + 1, -1)]

                bar1_range = float(bar1["High"] - bar1["Low"])
                if bar1_range <= 0:
                    continue

                # ATR computed on the bar strictly BEFORE bar1 -- excludes
                # bar1 itself, same "exclude current" principle as the
                # volume baseline below, so the flagpole's own abnormal
                # range can't inflate the very average it's compared against.
                atr_idx = bar1_idx - 1
                if abs(atr_idx) > len(df_atr):
                    continue
                atr_value = df_atr["ATR"].iloc[atr_idx]
                if pd.isna(atr_value) or atr_value <= 0:
                    continue
                if bar1_range < self.ATR_MULTIPLE * atr_value:
                    continue  # flagpole not big enough vs normal volatility

                baseline_window = df["Volume"].iloc[bar1_idx - self.VOLUME_LOOKBACK: bar1_idx]
                if len(baseline_window) < self.VOLUME_LOOKBACK:
                    continue
                avg_volume  = float(baseline_window.mean())
                bar1_volume = float(bar1["Volume"])
                volume_ratio = bar1_volume / avg_volume if avg_volume > 0 else 0.0
                if volume_ratio <= 1.0:
                    continue  # explosive bar must show above-average volume

                indicators = {
                    "Bar1_Range":    round(bar1_range, 2),
                    "Bar1_Volume":   int(bar1_volume),
                    "Avg_Volume_20": int(avg_volume),
                    "Volume_Ratio":  round(volume_ratio, 2),
                    "Consolidation_Bars": n_consol,
                    "ATR_14":        round(float(atr_value), 2),
                    "ATR_Multiple":  round(bar1_range / atr_value, 2),
                }

                is_bullish_ignite = bar1["Close"] > bar1["Open"]
                is_bearish_ignite = bar1["Close"] < bar1["Open"]
                bar1_high, bar1_low = float(bar1["High"]), float(bar1["Low"])

                if is_bullish_ignite and self._consolidation_ok(bar1_high, bar1_low, bar1_range, bar1_volume, consol, bullish=True):
                    if brk["High"] > bar1_high:
                        entry = bar1_high
                        stop  = min(float(b["Low"]) for b in consol)
                        risk  = abs(entry - stop)
                        if risk <= 0:
                            continue
                        target = entry + self.TARGET_PCT_OF_RANGE * bar1_range
                        strength = "STRONG" if volume_ratio >= self.STRONG_VOLUME_MULTIPLE else "MODERATE"
                        indicators.update({"Pattern_Entry": round(entry, 2), "Pattern_Stop": round(stop, 2),
                                            "Pattern_Target": round(target, 2),
                                            "Pattern_Target_Exact": round(target, 2)})
                        reason = (
                            f"3-Bar Play LONG: explosive candle at {volume_ratio:.1f}x avg volume "
                            f"and {round(bar1_range/atr_value,1)}x ATR({self.ATR_PERIOD}), "
                            f"{n_consol}-candle consolidation held near its high, breakout above "
                            f"explosive candle's high (₹{bar1_high:.2f}). Target = "
                            f"{self.TARGET_PCT_OF_RANGE*100:.0f}% of flagpole range."
                        )
                        return SignalResult("BUY", strength, reason, indicators, self.name)

                if is_bearish_ignite and self._consolidation_ok(bar1_high, bar1_low, bar1_range, bar1_volume, consol, bullish=False):
                    if brk["Low"] < bar1_low:
                        entry = bar1_low
                        stop  = max(float(b["High"]) for b in consol)
                        risk  = abs(entry - stop)
                        if risk <= 0:
                            continue
                        target = entry - self.TARGET_PCT_OF_RANGE * bar1_range
                        strength = "STRONG" if volume_ratio >= self.STRONG_VOLUME_MULTIPLE else "MODERATE"
                        indicators.update({"Pattern_Entry": round(entry, 2), "Pattern_Stop": round(stop, 2),
                                            "Pattern_Target": round(target, 2),
                                            "Pattern_Target_Exact": round(target, 2)})
                        reason = (
                            f"3-Bar Play SHORT: explosive candle at {volume_ratio:.1f}x avg volume "
                            f"and {round(bar1_range/atr_value,1)}x ATR({self.ATR_PERIOD}), "
                            f"{n_consol}-candle consolidation held near its low, breakout below "
                            f"explosive candle's low (₹{bar1_low:.2f}). Target = "
                            f"{self.TARGET_PCT_OF_RANGE*100:.0f}% of flagpole range."
                        )
                        return SignalResult("SELL", strength, reason, indicators, self.name)

            return SignalResult("HOLD", "WEAK", "No qualifying 3-bar flag pattern on the latest bars",
                                 strategy=self.name)

        except Exception as e:
            return SignalResult("HOLD", "WEAK", f"3-Bar Play calculation error: {e}", strategy=self.name)


STRATEGIES["3 Bar Play"] = ThreeBarFlagStrategy()
STRATEGY_NAMES = list(STRATEGIES.keys())