from configs.timeframes import TIMEFRAMES
from configs.watchlist import WATCHLIST

from core.logger.signal_logger import (
    SignalLogger
)

from core.signals.reversal_rsi_signal import (
    ReversalRSISignal
)

from core.indicators.rsi_indicator import (
    RSIIndicator
)

from data.providers.yfinance_provider import (
    YFinanceProvider
)


logger = SignalLogger()

provider = YFinanceProvider()

signal_engine = ReversalRSISignal()


def generate_all_signals():

    all_signals = []

    for timeframe_name, interval in TIMEFRAMES.items():

        for stock in WATCHLIST:

            try:

                df = provider.fetch_data(

                    stock,

                    interval

                )

                df["RSI"] = (

                    RSIIndicator()

                    .calculate(

                        df["Close"]

                    )

                )

                latest = df.iloc[-1]

                signal = (

                    signal_engine

                    .generate_signal(

                        df["RSI"]

                    )

                )

                logger.log_signal(

                    stock,

                    timeframe_name,

                    signal,

                    latest["RSI"],

                    latest["Close"]

                )

                all_signals.append({

                    "Stock":
                    stock,

                    "Timeframe":
                    timeframe_name,

                    "Signal":
                    signal,

                    "RSI":
                    latest["RSI"]

                })

            except:

                pass

    return all_signals