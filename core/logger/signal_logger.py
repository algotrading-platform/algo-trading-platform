import pandas as pd
import os
from datetime import datetime


class SignalLogger:

    def __init__(self):

        os.makedirs("data", exist_ok=True)

        self.log_file="data/signal_logs.csv"

        if not os.path.exists(self.log_file):

            pd.DataFrame(

                columns=[
                    "Timestamp",
                    "Timeframe",
                    "Stock",
                    "Signal",
                    "RSI",
                    "Price"
                ]

            ).to_csv(
                self.log_file,
                index=False
            )


    def log_signal(
        self,
        timeframe,
        stock,
        signal,
        rsi,
        price
    ):

        if signal=="HOLD":
            return


        try:

            df=pd.read_csv(
                self.log_file
            )

        except:

            df=pd.DataFrame()


        if not df.empty:

            last=df[
                (df["Stock"]==stock)
                &
                (df["Timeframe"]==timeframe)
            ]

            if not last.empty:

                prev=last.iloc[-1]["Signal"]

                if prev==signal:
                    return


        row={

            "Timestamp":
            datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            ),

            "Timeframe":
            timeframe,

            "Stock":
            stock,

            "Signal":
            signal,

            "RSI":
            round(rsi,2),

            "Price":
            round(price,2)

        }


        pd.concat(
            [
                df,
                pd.DataFrame([row])
            ]
        ).to_csv(
            self.log_file,
            index=False
        )


    def get_logs(self):

        try:
            return pd.read_csv(
                self.log_file
            )

        except:
            return pd.DataFrame()