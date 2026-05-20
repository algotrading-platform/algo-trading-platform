# ============================================================
# app/dashboard/dashboard.py
#
# Production Algo Trading Dashboard
# ----------------------------------
# Architecture:
#   Three separate tables as Jwala requested:
#     1. Indexes   (Nifty 50, Bank Nifty, Sensex)
#     2. Stocks    (NSE F&O watchlist)
#     3. Commodities (Gold, Silver, Copper, Crude)
#
# Key behaviours:
#   - Only BUY / SELL rows shown in signal tables (no HOLD noise)
#   - Signal logging + alerts ONLY during market hours
#   - Market hours gate: 9:15 – 15:30 IST, Mon–Fri
#   - Auto-refresh every 60 seconds regardless of timeframe
#   - TradingView links carry the selected timeframe
#   - Telegram alert fires on signal state change
#   - All st.dataframe() calls use use_container_width=True
#     (never string values for width/height)
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
# INITIALISE ENGINES  (once per process via session_state)
# ============================================================

if "provider" not in st.session_state:
    st.session_state.provider      = YFinanceProvider()
    st.session_state.rsi_indicator = RSIIndicator()
    st.session_state.signal_engine = ReversalRSISignal()
    st.session_state.backtest      = RSIBacktest()
    st.session_state.logger        = SignalLogger()
    st.session_state.alerts        = AlertManager()

provider      = st.session_state.provider
rsi_indicator = st.session_state.rsi_indicator
signal_engine = st.session_state.signal_engine
backtest_engine = st.session_state.backtest
logger        = st.session_state.logger
alerts        = st.session_state.alerts

# ============================================================
# HELPERS
# ============================================================

def market_open() -> bool:
    ist = pytz.timezone("Asia/Kolkata")
    now = datetime.now(ist)
    if now.weekday() >= 5:          # Saturday / Sunday
        return False
    return time(9, 15) <= now.time() <= time(15, 30)


def tv_url(symbol: str, tv_symbol: str, tf_name: str) -> str:
    """Build a TradingView deep-link with the selected timeframe."""
    interval = TV_INTERVALS.get(tf_name, "D")
    return (
        f"https://www.tradingview.com/chart/"
        f"?symbol={tv_symbol}&interval={interval}"
    )


def _stock_tv(symbol: str) -> str:
    """Convert NSE ticker to TradingView symbol."""
    return f"NSE:{symbol.replace('.NS', '')}"


def process_symbol(
    symbol: str,
    display_name: str,
    tv_symbol: str,
    interval: str,
    tf_name: str,
    period: str = "3mo",
) -> dict | None:
    """
    Fetch data, calculate RSI, generate signal, run backtest.
    Returns a result dict or None if data unavailable.
    """
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

        # --- log + alert only during market hours ---
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
                emoji = "🟢" if alert["signal"] == "BUY" else "🔴"
                st.toast(
                    f"{emoji} {alert['stock']}  "
                    f"{alert['previous']} → {alert['signal']}  "
                    f"RSI: {alert['rsi']}",
                    icon=emoji,
                )

        # --- backtest ---
        trades     = backtest_engine.run(df)
        completed  = [t for t in trades if "PnL" in t]
        pnl        = sum(t.get("PnL", 0) for t in completed)
        wins       = [t for t in completed if t.get("PnL", 0) > 0]
        winrate    = round(len(wins) / len(completed) * 100, 1) if completed else 0.0

        return {
            "Symbol":      symbol,
            "Name":        display_name,
            "Close":       round(float(latest["Close"]), 2),
            "RSI":         round(float(latest["RSI"]), 2),
            "Signal":      signal,
            "Trades":      len(completed),
            "PnL":         round(pnl, 2),
            "Win Rate %":  winrate,
            "_tv_url":     tv_url(symbol, tv_symbol, tf_name),
        }

    except Exception as e:
        return None          # silently skip broken symbols


def render_signal_table(results: list[dict], title: str) -> None:
    """
    Render a section with:
      - KPI row (total scanned, BUY count, SELL count, avg win rate)
      - Table showing ONLY BUY / SELL rows (Jwala requirement)
      - TradingView chart buttons per row
    """
    st.subheader(title)

    if not results:
        st.info("No data available for this category.")
        return

    df_all = pd.DataFrame(results)
    total  = len(df_all)
    buys   = (df_all["Signal"] == "BUY").sum()
    sells  = (df_all["Signal"] == "SELL").sum()
    avg_wr = round(df_all["Win Rate %"].mean(), 1)

    # --- KPI row ---
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Scanned",      total)
    k2.metric("🟢 BUY",       int(buys))
    k3.metric("🔴 SELL",      int(sells))
    k4.metric("Avg Win Rate", f"{avg_wr}%")
    k5.metric("PnL (sum)",    round(df_all["PnL"].sum(), 2))

    # --- filter: only actionable signals ---
    df_signals = df_all[df_all["Signal"].isin(["BUY", "SELL"])].copy()

    if df_signals.empty:
        st.success("✅ No active signals right now — all positions HOLD.")
        return

    # --- render rows ---
    header = st.columns([2, 1.5, 1, 1, 1, 1, 1, 1])
    labels = ["Name", "Close", "RSI", "Signal",
              "Trades", "PnL", "Win %", "Chart"]
    for col, label in zip(header, labels):
        col.markdown(f"**{label}**")

    st.divider()

    for _, row in df_signals.iterrows():
        c = st.columns([2, 1.5, 1, 1, 1, 1, 1, 1])

        c[0].write(row["Name"])
        c[1].write(f"₹{row['Close']:,.2f}")
        c[2].write(row["RSI"])

        if row["Signal"] == "BUY":
            c[3].success("BUY")
        else:
            c[3].error("SELL")

        c[4].write(row["Trades"])
        c[5].write(row["PnL"])
        c[6].write(f"{row['Win Rate %']}%")
        c[7].link_button("📈", row["_tv_url"], use_container_width=True)


# ============================================================
# SIDEBAR
# ============================================================

with st.sidebar:
    st.image(
        "https://upload.wikimedia.org/wikipedia/commons/"
        "4/44/National_Stock_Exchange_India_logo.svg",
        width=120,
    )
    st.markdown("## ⚙️ Settings")

    selected_tf = st.selectbox(
        "Timeframe",
        list(TIMEFRAMES.keys()),
        index=2,   # default: 1 Hour
    )

    st.markdown("---")
    st.markdown("### 📂 Category")
    show_indexes     = st.checkbox("Indexes",     value=True)
    show_stocks      = st.checkbox("Stocks",      value=True)
    show_commodities = st.checkbox("Commodities", value=True)

    st.markdown("---")

    # Telegram setup reminder
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if tg_token:
        st.success("✅ Telegram connected")
    else:
        st.warning(
            "⚠️ Telegram not configured.\n\n"
            "Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` "
            "to your `.env` file."
        )

interval = TIMEFRAMES[selected_tf]

# ============================================================
# HEADER
# ============================================================

col_title, col_status = st.columns([4, 1])

with col_title:
    st.title("📈 Algo Trading Dashboard")
    st.caption(
        f"Last refreshed: "
        f"{datetime.now(pytz.timezone('Asia/Kolkata')).strftime('%d %b %Y  %H:%M:%S IST')}"
        f"  ·  Timeframe: **{selected_tf}**"
        f"  ·  Auto-refresh: 60s"
    )

with col_status:
    st.markdown("")
    st.markdown("")
    if market_open():
        st.success("🟢 Market Open")
    else:
        st.warning("🔴 Market Closed")

st.divider()

# ============================================================
# DATA COLLECTION
# ============================================================

with st.spinner("Fetching market data..."):

    index_results     = []
    stock_results     = []
    commodity_results = []

    # --- Indexes ---
    if show_indexes:
        for sym in INDEXES:
            r = process_symbol(
                symbol=sym,
                display_name=INDEXES_DISPLAY.get(sym, sym),
                tv_symbol=INDEXES_TV.get(sym, sym),
                interval=interval,
                tf_name=selected_tf,
                period="3mo",
            )
            if r:
                index_results.append(r)

    # --- Stocks ---
    if show_stocks:
        for sym in STOCKS:
            r = process_symbol(
                symbol=sym,
                display_name=stock_display(sym),
                tv_symbol=_stock_tv(sym),
                interval=interval,
                tf_name=selected_tf,
                period="3mo",
            )
            if r:
                stock_results.append(r)

    # --- Commodities ---
    if show_commodities:
        for sym in COMMODITIES:
            r = process_symbol(
                symbol=sym,
                display_name=COMMODITIES_DISPLAY.get(sym, sym),
                tv_symbol=COMMODITIES_TV.get(sym, sym),
                interval=interval,
                tf_name=selected_tf,
                period="3mo",
            )
            if r:
                commodity_results.append(r)


# ============================================================
# RENDER TABLES
# ============================================================

if show_indexes:
    render_signal_table(index_results, "🏦 Indexes")
    st.markdown("")

if show_stocks:
    render_signal_table(stock_results, "📊 NSE Stocks (F&O Watchlist)")
    st.markdown("")

if show_commodities:
    render_signal_table(commodity_results, "🥇 Commodities (MCX)")
    st.markdown("")


# ============================================================
# SIGNAL HISTORY
# ============================================================

st.divider()
st.subheader("📋 Signal History (Last 7 Days)")

try:
    logs = logger.get_logs()

    if logs.empty:
        st.info("No signal history yet. Signals are logged during market hours.")
    else:
        # filter to selected timeframe
        logs_tf = logs[logs["Timeframe"] == selected_tf]

        if logs_tf.empty:
            st.info(f"No signals logged for **{selected_tf}** timeframe yet.")
        else:
            # colour the Signal column
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
    "⚠️ This dashboard is for informational and research purposes only. "
    "It does not constitute financial advice. Trade at your own risk."
)
