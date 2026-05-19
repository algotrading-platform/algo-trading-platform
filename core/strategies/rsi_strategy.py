class RSIStrategy:

    def generate_signal(self, rsi_value):

        if rsi_value < 30:
            return "BUY"

        elif rsi_value > 70:
            return "SELL"

        return "HOLD"