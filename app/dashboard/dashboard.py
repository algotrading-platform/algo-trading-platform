# ============================================================
# app/dashboard/dashboard.py
# ============================================================

import sys
import os

sys.path.append(
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../..")
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

from configs.instruments import (
    INDEXES, INDEXES_TV, INDEXES_DISPLAY,
    STOCKS, stock_display,
    COMMODITIES, COMMODITIES_TV, COMMODITIES_DISPLAY,
)
from configs.timeframes import TIMEFRAMES, TV_INTERVALS


# ============================================================
# PAGE CONFIG
# ============================================================

st.set_page_config(
    page_title="Algo Trading Dashboard",
    page_icon="📈",
    layout="wide",
)

st_autorefresh(interval=60000, key="dashboard_refresh")


# ============================================================
# PERIOD MAP
# ============================================================

PERIOD_MAP = {
    "5 Minutes":  "5d",
    "15 Minutes": "1mo",
    "1 Hour":     "3mo",
    "1 Day":      "1y",
    "1 Week":     "2y",
}


# ============================================================
# INITIALISE ENGINES
# ============================================================

if "provider" not in st.session_state:
    st.session_state.provider      = YFinanceProvider()
    st.session_state.rsi_indicator = RSIIndicator()
    st.session_state.signal_engine = ReversalRSISignal()
    st.session_state.backtest      = RSIBacktest()
    st.session_state.logger        = SignalLogger()
    st.session_state.alerts        = AlertManager()

provider        = st.session_state.provider
rsi_indicator   = st.session_state.rsi_indicator
signal_engine   = st.session_state.signal_engine
backtest_engine = st.session_state.backtest
logger          = st.session_state.logger
alerts          = st.session_state.alerts


# ============================================================
# HELPERS
# ============================================================

def market_open() -> bool:
    ist = pytz.timezone("Asia/Kolkata")
    now = datetime.now(ist)
    if now.weekday() >= 5:
        return False
    return time(9, 15) <= now.time() <= time(15, 30)


def tv_url(tv_symbol: str, tf_name: str) -> str:
    interval = TV_INTERVALS.get(tf_name, "D")
    return (
        f"https://www.tradingview.com/chart/"
        f"?symbol={tv_symbol}&interval={interval}"
    )


def _stock_tv(symbol: str) -> str:
    return f"NSE:{symbol.replace('.NS', '')}"


def process_symbol(
    symbol: str,
    display_name: str,
    tv_symbol: str,
    interval: str,
    tf_name: str,
    period: str,
) -> dict | None:

    try:
        df = provider.fetch_data(
            symbol=symbol,
            interval=interval,
            period=period,
        )

        if df is None or df.empty or len(df) < 20:
            return None

        df["RSI"] = rsi_indicator.calculate(df["Close"])
        df.dropna(subset=["RSI"], inplace=True)

        if len(df) < 3:
            return None

        latest = df.iloc[-1]
        signal = signal_engine.generate_signal(df["RSI"])

        # Candle timestamp
        candle_time = ""
        if "Datetime" in df.columns:
            candle_time = str(latest["Datetime"])[:16]

        # Log + alert only during market hours
        if market_open():
            logger.log_signal(
                stock=symbol,
                timeframe=tf_name,
                signal=signal,
                rsi=float(latest["RSI"]),
                price=float(latest["Close"]),
            )

            alert = alerts.check_alert(
                timeframe=tf_name,
                stock=symbol,
                current_signal=signal,
                rsi=float(latest["RSI"]),
                price=float(latest["Close"]),
            )

            if alert and alert["signal"] != "HOLD":
                tag = "BUY" if alert["signal"] == "BUY" else "SELL"
                st.toast(
                    f"[{tag}] {alert['stock']}  "
                    f"{alert['previous']} -> {alert['signal']}  "
                    f"RSI: {alert['rsi']}",
                )

        # Backtest
        trades    = backtest_engine.run(df)
        completed = [t for t in trades if "PnL" in t]
        pnl_abs   = sum(t.get("PnL", 0) for t in completed)
        wins      = [t for t in completed if t.get("PnL", 0) > 0]
        winrate   = round(len(wins) / len(completed) * 100, 1) if completed else 0.0

        buy_trades = [t for t in trades if t.get("Type") == "BUY"]
        avg_buy    = (
            sum(t["Price"] for t in buy_trades) / len(buy_trades)
            if buy_trades else 0
        )
        pnl_pct = (
            round((pnl_abs / (avg_buy * len(buy_trades))) * 100, 2)
            if avg_buy > 0 else 0.0
        )

        return {
            "Symbol":      symbol,
            "Name":        display_name,
            "Close":       round(float(latest["Close"]), 2),
            "RSI":         round(float(latest["RSI"]), 2),
            "Signal":      signal,
            "Candle Time": candle_time,
            "Trades":      len(completed),
            "PnL (Rs)":    round(pnl_abs, 2),
            "PnL %":       pnl_pct,
            "Win Rate %":  winrate,
            "_tv_url":     tv_url(tv_symbol, tf_name),
        }

    except Exception:
        return None


def render_signal_table(
    results: list[dict],
    title: str,
    period: str = "",
) -> None:

    st.subheader(title)

    if not results:
        st.info("No data available for this category.")
        return

    df_all    = pd.DataFrame(results)
    total     = len(df_all)
    buys      = int((df_all["Signal"] == "BUY").sum())
    sells     = int((df_all["Signal"] == "SELL").sum())
    avg_wr    = round(df_all["Win Rate %"].mean(), 1)
    total_pnl = round(df_all["PnL (Rs)"].sum(), 2)
    avg_pnl_pct = round(df_all["PnL %"].mean(), 2)

    # KPI row
    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric("Scanned",      total)
    k2.metric("BUY signals",  buys)
    k3.metric("SELL signals", sells)
    k4.metric("Avg Win Rate", f"{avg_wr}%")
    k5.metric(
        f"Backtest PnL ({period})" if period else "Backtest PnL",
        f"Rs. {total_pnl}",
        help="Historical backtest result over the fetched period. Not live PnL.",
    )
    k6.metric(
        "Avg PnL %",
        f"{avg_pnl_pct}%",
        delta=f"{avg_pnl_pct}%",
        help="Average percentage return per completed trade across all instruments.",
    )

    # Filter: only actionable signals
    df_signals = df_all[df_all["Signal"].isin(["BUY", "SELL"])].copy()

    if df_signals.empty:
        st.success("No active signals right now — all instruments HOLD.")
        return

    # Header row
    h = st.columns([2, 1.5, 1, 1, 1.5, 1, 1.2, 1, 1])
    for col, label in zip(
        h,
        ["Name", "Close (Rs)", "RSI", "Signal",
         "Candle Time", "Trades", "PnL (Rs)", "PnL %", "Chart"]
    ):
        col.markdown(f"**{label}**")

    st.divider()

    # Data rows
    for _, row in df_signals.iterrows():
        c = st.columns([2, 1.5, 1, 1, 1.5, 1, 1.2, 1, 1])

        c[0].write(row["Name"])
        c[1].write(f"{row['Close']:,.2f}")
        c[2].write(row["RSI"])

        if row["Signal"] == "BUY":
            c[3].success("BUY")
        else:
            c[3].error("SELL")

        c[4].write(row["Candle Time"] if row["Candle Time"] else "—")
        c[5].write(row["Trades"])

        pnl_abs   = row["PnL (Rs)"]
        pnl_pct   = row["PnL %"]
        pnl_color = "green" if pnl_abs >= 0 else "red"

        c[6].markdown(
            f"<span style='color:{pnl_color}'>{pnl_abs:,.2f}</span>",
            unsafe_allow_html=True,
        )
        c[7].markdown(
            f"<span style='color:{pnl_color}'>{pnl_pct}%</span>",
            unsafe_allow_html=True,
        )

        c[8].link_button("View", row["_tv_url"], use_container_width=True)


# ============================================================
# SIDEBAR
# ============================================================

with st.sidebar:

    st.markdown("### Algo Trading")

    st.markdown("## Settings")

    selected_tf = st.selectbox(
        "Timeframe",
        list(TIMEFRAMES.keys()),
        index=2,
    )

    st.markdown("---")
    st.markdown("### Category")
    show_indexes     = st.checkbox("Indexes",     value=True)
    show_stocks      = st.checkbox("Stocks",      value=True)
    show_commodities = st.checkbox("Commodities", value=True)

    st.markdown("---")

    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if tg_token:
        st.success("Telegram connected")
    else:
        st.warning(
            "Telegram not configured.\n\n"
            "Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID "
            "to your .env file."
        )

    st.markdown("---")
    st.caption(
        "Signals logged only during market hours (9:15 - 15:30 IST).\n\n"
        "Backtest PnL is historical performance over the fetched period — "
        "not live trading profit or loss."
    )

interval     = TIMEFRAMES[selected_tf]
fetch_period = PERIOD_MAP.get(selected_tf, "3mo")


# ============================================================
# HEADER
# ============================================================

col_title, col_status = st.columns([4, 1])

with col_title:
    st.title("Algo Trading Dashboard")
    ist_now = datetime.now(pytz.timezone("Asia/Kolkata"))
    st.caption(
        f"Last refreshed: {ist_now.strftime('%d %b %Y  %H:%M:%S IST')}"
        f"  |  Timeframe: {selected_tf}"
        f"  |  Period: {fetch_period}"
        f"  |  Auto-refresh: 60s"
    )

with col_status:
    st.markdown("")
    st.markdown("")
    if market_open():
        st.success("Market OPEN")
    else:
        st.warning("Market CLOSED")

st.divider()


# ============================================================
# DATA COLLECTION
# ============================================================

with st.spinner("Fetching market data..."):

    index_results     = []
    stock_results     = []
    commodity_results = []

    if show_indexes:
        for sym in INDEXES:
            r = process_symbol(
                symbol=sym,
                display_name=INDEXES_DISPLAY.get(sym, sym),
                tv_symbol=INDEXES_TV.get(sym, sym),
                interval=interval,
                tf_name=selected_tf,
                period=fetch_period,
            )
            if r:
                index_results.append(r)

    if show_stocks:
        for sym in STOCKS:
            r = process_symbol(
                symbol=sym,
                display_name=stock_display(sym),
                tv_symbol=_stock_tv(sym),
                interval=interval,
                tf_name=selected_tf,
                period=fetch_period,
            )
            if r:
                stock_results.append(r)

    if show_commodities:
        for sym in COMMODITIES:
            r = process_symbol(
                symbol=sym,
                display_name=COMMODITIES_DISPLAY.get(sym, sym),
                tv_symbol=COMMODITIES_TV.get(sym, sym),
                interval=interval,
                tf_name=selected_tf,
                period=fetch_period,
            )
            if r:
                commodity_results.append(r)


# ============================================================
# RENDER TABLES
# ============================================================

if show_indexes:
    render_signal_table(index_results, "Indexes", period=fetch_period)
    st.markdown("")

if show_stocks:
    render_signal_table(stock_results, "NSE Stocks (F&O Watchlist)", period=fetch_period)
    st.markdown("")

if show_commodities:
    render_signal_table(commodity_results, "Commodities (MCX)", period=fetch_period)
    st.markdown("")


# ============================================================
# SIGNAL HISTORY
# ============================================================

st.divider()
st.subheader("Signal History (Last 7 Days)")

try:
    logs = logger.get_logs()

    if logs.empty:
        st.info("No signal history yet. Signals are logged during market hours.")
    else:
        logs_tf = logs[logs["Timeframe"] == selected_tf]

        if logs_tf.empty:
            st.info(
                f"No signals logged for {selected_tf} timeframe yet. "
                f"Signals are recorded only during market hours (9:15 - 15:30 IST)."
            )
        else:
            def _color(val):
                if val == "BUY":
                    return "background-color: #1a472a; color: #6fcf97"
                elif val == "SELL":
                    return "background-color: #4a0e0e; color: #eb5757"
                return ""

            st.dataframe(
                logs_tf.style.map(_color, subset=["Signal"]),
                use_container_width=True,
                hide_index=True,
            )

except Exception as e:
    st.warning(f"Signal history unavailable: {e}")


# ============================================================
# FOOTER
# ============================================================

st.divider()
st.caption(
    "This dashboard is for informational and research purposes only. "
    "It does not constitute financial advice. Trade at your own risk."
)
