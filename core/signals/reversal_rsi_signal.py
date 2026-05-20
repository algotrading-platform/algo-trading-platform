# ============================================================
# core/signals/reversal_rsi_signal.py
#
# Reversal confirmation logic (Jwala-approved):
#
#   BUY  : RSI was below 30 (trigger), then bounced up for
#          2 consecutive candles AND crossed back above 30.
#          This avoids buying into a continuing downtrend.
#
#   SELL : RSI was above 70 (trigger), then turned down for
#          2 consecutive candles AND crossed back below 70.
#          This avoids selling into a continuing uptrend.
#
#   HOLD : Everything else.
#
# Deduplication (previous_signal) is handled by SignalLogger,
# NOT here. This method always returns the market state so the
# logger can decide whether to record it.
# ============================================================


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
        # BUY: RSI dipped below 30, now recovering
        #   prev2 < 30  → was in oversold zone
        #   prev  > prev2  → first bounce candle
        #   current > prev → second bounce candle (confirmation)
        #   current > 30   → crossed back out of oversold zone
        # --------------------------------------------------
        if (
            prev2 < 30
            and prev > prev2
            and current > prev
            and current > 30
        ):
            return "BUY"

        # --------------------------------------------------
        # SELL: RSI pushed above 70, now reversing
        #   prev2 > 70  → was in overbought zone
        #   prev  < prev2  → first drop candle
        #   current < prev → second drop candle (confirmation)
        #   current < 70   → crossed back out of overbought zone
        # --------------------------------------------------
        if (
            prev2 > 70
            and prev < prev2
            and current < prev
            and current < 70
        ):
            return "SELL"

        return "HOLD"
