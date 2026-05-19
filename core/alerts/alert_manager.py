import pandas as pd
import os


class AlertManager:

    def __init__(self):

        self.alert_file = "data/last_signals.csv"

        if not os.path.exists(self.alert_file):

            pd.DataFrame(

                columns=[

                    "Timeframe",
                    "Stock",
                    "Signal"

                ]

            ).to_csv(

                self.alert_file,
                index=False

            )

    def check_alert(

        self,
        timeframe,
        stock,
        current_signal,
        rsi,
        price

    ):

        df = pd.read_csv(

            self.alert_file

        )


        stock_data = df[

            (df["Stock"] == stock)

            &

            (df["Timeframe"] == timeframe)

        ]


        # first entry

        if stock_data.empty:

            new_row = pd.DataFrame([{

                "Timeframe":
                timeframe,

                "Stock":
                stock,

                "Signal":
                current_signal

            }])

            df = pd.concat(

                [df, new_row],

                ignore_index=True

            )

            df.to_csv(

                self.alert_file,

                index=False

            )

            return None


        previous_signal = (

            stock_data

            .iloc[0]

            ["Signal"]

        )


        if previous_signal != current_signal:

            df.loc[

                (df["Stock"] == stock)

                &

                (df["Timeframe"] == timeframe),

                "Signal"

            ] = current_signal


            df.to_csv(

                self.alert_file,

                index=False

            )


            return {

                "stock":
                stock,

                "timeframe":
                timeframe,

                "previous":
                previous_signal,

                "current":
                current_signal,

                "rsi":
                round(rsi,2),

                "price":
                round(price,2)

            }

        return None