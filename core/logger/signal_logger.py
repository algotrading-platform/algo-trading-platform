import pandas as pd
import os
from datetime import datetime, timedelta


class SignalLogger:

    def __init__(self):

        os.makedirs("data", exist_ok=True)

        self.log_file = "data/signal_logs.csv"

        if not os.path.exists(self.log_file):

            pd.DataFrame(columns=[

                "Timestamp",
                "Timeframe",
                "Stock",
                "Signal",
                "RSI",
                "Price"

            ]).to_csv(
                self.log_file,
                index=False
            )

    # =====================================
    # LOG SIGNAL
    # =====================================

    def log_signal(
            self,
            timeframe,
            stock,
            signal,
            rsi,
            price
    ):

        # Ignore HOLD signals
        if signal == "HOLD":
            return

        df = self.get_logs()

        # ---------------------------------
        # Prevent duplicate consecutive
        # ---------------------------------

        stock_history = df[

            (df["Stock"] == stock)

            &

            (df["Timeframe"] == timeframe)

        ]

        if not stock_history.empty:

            last_signal = (
                stock_history
                .iloc[-1]["Signal"]
            )

            if last_signal == signal:
                return

        new_entry = {

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
            round(rsi, 2),

            "Price":
            round(price, 2)

        }

        df = pd.concat(

            [

                df,

                pd.DataFrame(
                    [new_entry]
                )

            ],

            ignore_index=True

        )

        # Keep only last 7 days
        df["Timestamp"] = pd.to_datetime(
            df["Timestamp"]
        )

        cutoff = datetime.now() - timedelta(
            days=7
        )

        df = df[
            df["Timestamp"] >= cutoff
        ]

        df.to_csv(
            self.log_file,
            index=False
        )

    # =====================================
    # GET LOGS
    # =====================================

    def get_logs(self):

        if not os.path.exists(
                self.log_file):

            return pd.DataFrame()

        df = pd.read_csv(
            self.log_file
        )

        if len(df) == 0:
            return df

        try:

            df["Timestamp"] = pd.to_datetime(
                df["Timestamp"]
            )

            df = df.sort_values(
                "Timestamp",
                ascending=False
            )

        except:
            pass

        return df