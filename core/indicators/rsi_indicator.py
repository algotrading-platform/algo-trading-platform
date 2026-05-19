from ta.momentum import RSIIndicator as TA_RSI


class RSIIndicator:

    def calculate(self, close_prices):

        indicator = TA_RSI(close=close_prices, window=14)

        return indicator.rsi()