# ============================================================
# core/signals/reversal_rsi_signal.py
#
# Reversal confirmation logic (Jwala-approved):
#
#   BUY  : RSI was below 35 (trigger), then bounced up for
#          2 consecutive candles AND crossed back above 25.
#          Deeper oversold threshold = fewer, higher quality signals.
#
#   SELL : RSI was above 65 (trigger), then turned down for
#          2 consecutive candles AND crossed back below 75.
#          Deeper overbought threshold = fewer, higher quality signals.
#
#   HOLD : Everything else.
#
# Changed from 30/70 to 25/75 to reduce signal noise and
# generate only high-conviction reversal opportunities.
#
# Deduplication (previous_signal) is handled by SignalLogger,
# NOT here. This method always returns the market state so the
# logger can decide whether to record it.
# ============================================================

RSI_OVERSOLD  = 35   # BUY trigger level (Jwala 06-Jun-2026: broaden to 35)
RSI_OVERBOUGHT = 75  # SELL trigger level (Jwala 06-Jun-2026)


class ReversalRSISignal:

    def generate_signal(self, rsi_series):
        """
        Parameters
        ----------
        rsi_series : pd.Series
            RSI values in chronological order (oldest → newest).
            Requires at least 3 values.

        Returns
        -------
        str : "BUY" | "SELL" | "HOLD"
        """

        if len(rsi_series) < 3:
            return "HOLD"

        current  = rsi_series.iloc[-1]
        prev     = rsi_series.iloc[-2]
        prev2    = rsi_series.iloc[-3]

        # --------------------------------------------------
        # BUY: RSI dipped below 25, now recovering
        #   prev2 < 35  → was in oversold zone
        #   prev  > prev2  → first bounce candle
        #   current > prev → second bounce candle (confirmation)
        #   current > 35   → crossed back out of oversold zone
        # --------------------------------------------------
        if (
            prev2 < RSI_OVERSOLD
            and prev > prev2
            and current > prev
            and current > RSI_OVERSOLD
        ):
            return "BUY"

        # --------------------------------------------------
        # SELL: RSI pushed above 75, now reversing
        #   prev2 > 75  → was in overbought zone
        #   prev  < prev2  → first drop candle
        #   current < prev → second drop candle (confirmation)
        #   current < 75   → crossed back out of overbought zone
        # --------------------------------------------------
        if (
            prev2 > RSI_OVERBOUGHT
            and prev < prev2
            and current < prev
            and current < RSI_OVERBOUGHT
        ):
            return "SELL"

        return "HOLD"