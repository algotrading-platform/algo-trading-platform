class RSIBacktest:

    def run(self, df):

        trades = []

        position = None
        buy_price = 0

        for i in range(2, len(df)):

            current_rsi = (
                df["RSI"]
                .iloc[i]
            )

            previous_rsi = (
                df["RSI"]
                .iloc[i-1]
            )

            close = (
                df["Close"]
                .iloc[i]
            )


            # ---------------------
            # BUY REVERSAL
            # ---------------------

            if (

                previous_rsi < 30

                and

                current_rsi
                >
                previous_rsi

                and

                position is None

            ):

                position = "BUY"

                buy_price = close


                trades.append({

                    "Type":"BUY",

                    "Price":
                    round(
                        close,
                        2
                    )

                })


            # ---------------------
            # SELL REVERSAL
            # ---------------------

            elif (

                previous_rsi > 70

                and

                current_rsi
                <
                previous_rsi

                and

                position=="BUY"

            ):

                pnl = (

                    close

                    -

                    buy_price

                )


                pnl_percent = (

                    pnl

                    /

                    buy_price

                ) *100


                trades.append({

                    "Type":"SELL",

                    "Price":
                    round(
                        close,
                        2
                    ),

                    "PnL":
                    round(
                        pnl,
                        2
                    ),

                    "PnL %":
                    round(
                        pnl_percent,
                        2
                    )

                })


                position=None


        return trades