import sys
import os

sys.path.append(
    os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "../.."
        )
    )
)

import streamlit as st
import pandas as pd
from datetime import datetime, time
import pytz
from streamlit_autorefresh import st_autorefresh

from data.providers.yfinance_provider import YFinanceProvider
from core.indicators.rsi_indicator import RSIIndicator
from core.signals.reversal_rsi_signal import ReversalRSISignal
from core.backtesting.rsi_backtest import RSIBacktest
from core.logger.signal_logger import SignalLogger

from configs.watchlist import WATCHLIST
from configs.timeframes import TIMEFRAMES


# ====================================================
# PAGE CONFIG
# ====================================================

st.set_page_config(
    page_title="Algo Trading Dashboard",
    layout="wide"
)

st_autorefresh(
    interval=60000,
    key="refresh"
)


provider = YFinanceProvider()
signal_engine = ReversalRSISignal()
backtest = RSIBacktest()
logger = SignalLogger()


def market_open():

    ist = pytz.timezone("Asia/Kolkata")
    now = datetime.now(ist)

    if now.weekday() >= 5:
        return False

    return time(9,15) <= now.time() <= time(15,30)


# ====================================================
# SIDEBAR
# ====================================================

selected_timeframe = st.sidebar.selectbox(
    "Select Timeframe",
    list(TIMEFRAMES.keys())
)

interval = TIMEFRAMES[selected_timeframe]


# ====================================================
# HEADER
# ====================================================

st.title("Algo Trading Dashboard")

st.caption(
    f"Last Updated: "
    f"{datetime.now().strftime('%d-%m-%Y %H:%M:%S')}"
)

st.info("Dashboard refreshes every 60 seconds")

st.subheader(
    f"Current Timeframe: {selected_timeframe}"
)

if market_open():

    st.success("🟢 Market Open")

else:

    st.warning("🔴 Market Closed")


results = []


# ====================================================
# STOCK LOOP
# ====================================================

for stock in WATCHLIST:

    try:

        df = provider.fetch_data(
            symbol=stock,
            interval=interval,
            period="1mo"
        )

        if df.empty:
            continue

        df["RSI"] = RSIIndicator().calculate(
            df["Close"]
        )

        latest = df.iloc[-1]

        signal = signal_engine.generate_signal(
            df["RSI"]
        )

        logger.log_signal(

           timeframe=selected_timeframe,

           stock=stock,

           signal=signal,

           rsi=latest["RSI"],

           price=latest["Close"]

        )

        trades = backtest.run(df)

        pnl = sum(
            x.get("PnL",0)
            for x in trades
        )

        completed = len(
            [
                x for x in trades
                if "PnL" in x
            ]
        )

        wins = len(
            [
                x for x in trades
                if x.get("PnL",0) > 0
            ]
        )

        winrate = (

            round(
                wins/completed*100,
                2
            )

            if completed
            else 0

        )

        results.append({

            "Stock":stock,

            "Close":
            round(
                latest["Close"],
                2
            ),

            "RSI":
            round(
                latest["RSI"],
                2
            ),

            "Signal":
            signal,

            "Trades":
            completed,

            "PnL":
            round(
                pnl,
                2
            ),

            "Win Rate %":
            winrate,

            "Chart":
            f"https://www.tradingview.com/chart/?symbol=NSE:{stock.replace('.NS','')}"

        })

    except Exception as e:

        st.error(
            f"{stock}: {e}"
        )


dashboard = pd.DataFrame(results)


# ====================================================
# KPI CARDS
# ====================================================

if not dashboard.empty:

    c1,c2,c3,c4,c5 = st.columns(5)

    c1.metric(
        "Stocks",
        len(dashboard)
    )

    c2.metric(
        "BUY",
        (dashboard["Signal"]=="BUY").sum()
    )

    c3.metric(
        "SELL",
        (dashboard["Signal"]=="SELL").sum()
    )

    c4.metric(
        "Avg Win Rate",
        f"{dashboard['Win Rate %'].mean():.1f}%"
    )

    c5.metric(
        "PnL",
        round(
            dashboard["PnL"].sum(),
            2
        )
    )


# ====================================================
# LIVE SIGNAL TABLE
# ====================================================

st.subheader("Live Signals")

header = st.columns([2,1,1,1,1,1,1,1])

header[0].write("Stock")
header[1].write("Close")
header[2].write("RSI")
header[3].write("Signal")
header[4].write("Trades")
header[5].write("PnL")
header[6].write("Win %")
header[7].write("Chart")

for _, row in dashboard.iterrows():

    cols = st.columns([2,1,1,1,1,1,1,1])

    cols[0].write(row["Stock"])
    cols[1].write(row["Close"])
    cols[2].write(row["RSI"])

    if row["Signal"]=="BUY":

        cols[3].success("BUY")

    elif row["Signal"]=="SELL":

        cols[3].error("SELL")

    else:

        cols[3].info("HOLD")

    cols[4].write(row["Trades"])
    cols[5].write(row["PnL"])
    cols[6].write(f"{row['Win Rate %']}%")

    cols[7].link_button(
        "📈 Chart",
        row["Chart"]
    )


# ====================================================
# SIGNAL HISTORY
# ====================================================

st.subheader(
    "Signal History"
)

try:

    logs = logger.get_logs()

    if logs.empty:

        st.info(
            "No signal history"
        )

    else:

        logs = logs[
            logs["Timeframe"]
            ==
            selected_timeframe
        ]

        st.dataframe(
            logs.sort_values(
                "Timestamp",
                ascending=False
            ),

            use_container_width=True,
            hide_index=True
        )

except Exception as e:

    st.warning(
        f"History unavailable: {e}"
    )


# ====================================================
# EMPTY STATE
# ====================================================

if dashboard.empty:

    st.warning(
        "No stock data available"
    )