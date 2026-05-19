class ReversalRSISignal:

    def generate_signal(self, rsi_series):

        if len(rsi_series) < 3:
            return "HOLD"

        current_rsi = rsi_series.iloc[-1]
        previous_rsi = rsi_series.iloc[-2]

        # -----------------------------------
        # BUY REVERSAL LOGIC
        # -----------------------------------

        if previous_rsi < 30 and current_rsi > previous_rsi:
            return "BUY"

        # -----------------------------------
        # SELL REVERSAL LOGIC
        # -----------------------------------

        elif previous_rsi > 70 and current_rsi < previous_rsi:
            return "SELL"

        # -----------------------------------
        # DEFAULT
        # -----------------------------------

        return "HOLD"