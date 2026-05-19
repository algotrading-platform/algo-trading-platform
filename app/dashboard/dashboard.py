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
from core.alerts.alert_manager import AlertManager

from configs.watchlist import WATCHLIST
from configs.timeframes import TIMEFRAMES


st.set_page_config(

    page_title="Algo Dashboard",

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

alerts = AlertManager()


def market_open():

    ist = pytz.timezone(
        "Asia/Kolkata"
    )

    now = datetime.now(ist)

    if now.weekday() >= 5:

        return False

    return (

        time(9,15)

        <= now.time()

        <=

        time(15,30)

    )


selected_timeframe = st.sidebar.selectbox(

    "Select Timeframe",

    list(TIMEFRAMES.keys())

)

interval = TIMEFRAMES[
    selected_timeframe
]


st.title(
    "Algo Trading Dashboard"
)

st.subheader(

    f"Current Timeframe: {selected_timeframe}"

)


if market_open():

    st.success(
        "🟢 Market Open"
    )

else:

    st.warning(
        "🔴 Market Closed"
    )


results=[]


for stock in WATCHLIST:

    try:

        df = provider.fetch_data(

            symbol=stock,

            interval=interval,

            period="1mo"

        )


        if df.empty:
            continue


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


        previous_logs = logger.get_logs()

        duplicate=False


        if not previous_logs.empty:

            prev = previous_logs[

                (previous_logs["Stock"]

                 == stock)

                &

                (

                 previous_logs["Timeframe"]

                 ==

                 selected_timeframe

                )

            ]


            if not prev.empty:

                if (

                    prev.iloc[-1]

                    ["Signal"]

                    ==

                    signal

                ):

                    duplicate=True


        if (

            market_open()

            and

            not duplicate

        ):


            logger.log_signal(

                timeframe=
                selected_timeframe,

                stock=
                stock,

                signal=
                signal,

                rsi=
                latest["RSI"],

                price=
                latest["Close"]

            )


            alert = alerts.check_alert(

                timeframe=
                selected_timeframe,

                stock=
                stock,

                current_signal=
                signal,

                rsi=
                latest["RSI"],

                price=
                latest["Close"]

            )


            if alert:

                st.toast(

f"""🚨 {alert['stock']}

{alert['previous']}
→
{alert['current']}

RSI:
{alert['rsi']}

Price:
{alert['price']}
"""

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


        wins=len(

            [

                x for x in trades

                if x.get(

                    "PnL",0

                ) > 0

            ]

        )


        winrate=0


        if completed:

            winrate = round(

                wins

                /

                completed

                *

                100,

                2

            )


        results.append({

            "Stock":
            stock,

            "Close":
            round(
                latest["Close"],2
            ),

            "RSI":
            round(
                latest["RSI"],2
            ),

            "Signal":
            signal,

            "Trades":
            completed,

            "PnL":
            round(
                pnl,2
            ),

            "Win Rate %":
            winrate,

            "Chart":

            f"https://www.tradingview.com/chart/?symbol=NSE:{stock.replace('.NS','')}"

        })


    except Exception as e:

        results.append({

            "Stock":
            stock,

            "Signal":
            str(e)

        })


dashboard = pd.DataFrame(
    results
)


# ====================================================
# DASHBOARD TABLE
# ====================================================

def color_signal(x):

    if x == "BUY":
        return "background-color:green;color:white"

    elif x == "SELL":
        return "background-color:red;color:white"

    return "background-color:gray;color:white"


try:

    styled_dashboard = dashboard.style.map(

        color_signal,

        subset=["Signal"]

    )

    st.dataframe(

        styled_dashboard,

        use_container_width=True,
        hide_index=True

    )

except:

    st.dataframe(

        dashboard,

        use_container_width=True,
        hide_index=True

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

            "No signal history yet."

        )

    else:

        if "Timeframe" in logs.columns:

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

        f"Signal history unavailable: {e}"

    )


# ====================================================
# TRADINGVIEW LINKS
# ====================================================

st.subheader(

    "TradingView Charts"

)

cols = st.columns(3)

valid_rows = [

    r

    for r in results

    if "Stock" in r

]

for i,row in enumerate(valid_rows):

    stock = row["Stock"]

    chart = (

        f"https://www.tradingview.com/chart/?symbol=NSE:{stock.replace('.NS','')}"

    )

    with cols[i % 3]:

        st.link_button(

            label=stock,

            url=chart,

            use_container_width=True

        )


# ====================================================
# EMPTY DASHBOARD MESSAGE
# ====================================================

if dashboard.empty:

    st.warning(

        "No stock data available."

    )