# ============================================================
# app/dashboard/dashboard.py — FINAL Production Dashboard
# ============================================================

import sys
import os

_project_root = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../..")
)
sys.path.append(_project_root)

from dotenv import load_dotenv
load_dotenv(os.path.join(_project_root, ".env"))

import streamlit as st
import pandas as pd
from datetime import datetime
import pytz
from streamlit_autorefresh import st_autorefresh

from data.providers.yfinance_provider import YFinanceProvider
from core.indicators.rsi_indicator import RSIIndicator
from core.logger.signal_logger import SignalLogger
from core.backtesting.backtest_store import get_results
from core.database import get_last_scan_time as _db_last_scan
from configs.instruments import (
    INDEXES, INDEXES_DISPLAY, INDEXES_TV,
    STOCKS, stock_display,
    COMMODITIES, COMMODITIES_DISPLAY, COMMODITIES_TV,
    COMMODITIES_SKIP_TIMEFRAMES,
)
from configs.timeframes import TIMEFRAMES, TV_INTERVALS, PERIOD_MAP


# ============================================================
# PAGE CONFIG
# ============================================================

st.set_page_config(
    page_title="Algo Trading | Signal Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

st_autorefresh(interval=60000, key="dashboard_refresh")
IST = pytz.timezone("Asia/Kolkata")


# ============================================================
# SESSION STATE
# ============================================================

if "dark_mode"   not in st.session_state: st.session_state.dark_mode   = True
if "selected_tf" not in st.session_state: st.session_state.selected_tf = "1 Hour"

if "provider" not in st.session_state:
    st.session_state.provider      = YFinanceProvider()
    st.session_state.rsi_indicator = RSIIndicator()
    st.session_state.logger        = SignalLogger()

provider      = st.session_state.provider
rsi_indicator = st.session_state.rsi_indicator
logger        = st.session_state.logger


# ============================================================
# CSS — FINAL VERSION
# ============================================================

DARK = """
:root {
    --bg:         #080c18;
    --bg2:        #0d1526;
    --card:       #101828;
    --card2:      #141f34;
    --border:     #1a2840;
    --border2:    #263d60;
    --t1:         #f1f5fb;
    --t2:         #a8b8d0;
    --t3:         #6b7fa0;
    --t4:         #3d5070;
    --blue:       #4a90e2;
    --green:      #1ec9a0;
    --red:        #f05555;
    --amber:      #f7a800;
    --purple:     #9b6dff;
    --buy-bg:     rgba(30,201,160,0.12);
    --buy-br:     rgba(30,201,160,0.40);
    --sell-bg:    rgba(240,85,85,0.12);
    --sell-br:    rgba(240,85,85,0.40);
    --df-bg:      #0d1526;
    --df-hdr:     #101828;
    --df-row-alt: #111c2e;
    --df-text:    #a8b8d0;
}
"""

LIGHT = """
:root {
    --bg:         #eef2f7;
    --bg2:        #ffffff;
    --card:       #ffffff;
    --card2:      #f7fafd;
    --border:     #dde3ed;
    --border2:    #c4cfe0;
    --t1:         #0d1526;
    --t2:         #3d5170;
    --t3:         #7a8fad;
    --t4:         #b0c0d5;
    --blue:       #1a6fd4;
    --green:      #0a9e74;
    --red:        #cc2020;
    --amber:      #c47e00;
    --purple:     #6040cc;
    --buy-bg:     rgba(10,158,116,0.08);
    --buy-br:     rgba(10,158,116,0.35);
    --sell-bg:    rgba(204,32,32,0.08);
    --sell-br:    rgba(204,32,32,0.35);
    --df-bg:      #ffffff;
    --df-hdr:     #f4f7fb;
    --df-row-alt: #f9fbfd;
    --df-text:    #3d5170;
}
"""

SHARED = """
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap');

/* ---- Base ---- */
.stApp {
    background: var(--bg) !important;
    font-family: 'IBM Plex Sans', sans-serif;
}
section[data-testid="stSidebar"] {
    background: var(--bg2) !important;
    border-right: 1px solid var(--border) !important;
}
#MainMenu, footer { visibility: hidden; }
.viewerBadge_container__r5tak { display: none; }
.stApp > header { background: transparent !important; }

/* ---- Global text colour fix ---- */
.stApp p, .stApp div:not([class]),
.stApp span:not([class]) { color: var(--t1); }

/* ---- Selectbox ---- */
.stSelectbox > div > div {
    background: var(--card) !important;
    border-color: var(--border2) !important;
    color: var(--t1) !important;
    font-size: 13px !important;
    font-family: 'IBM Plex Sans', sans-serif !important;
}
.stSelectbox label { color: var(--t2) !important; font-size: 11px !important; }
div[data-baseweb="select"] span { color: var(--t1) !important; }

/* ---- Checkbox ---- */
.stCheckbox label p,
.stCheckbox label span { color: var(--t2) !important; font-size: 13px !important; }

/* ---- Button — theme-aware ---- */
.stButton > button {
    background: var(--card) !important;
    border: 1px solid var(--border2) !important;
    color: var(--t2) !important;
    font-size: 12px !important;
    font-family: 'IBM Plex Sans', sans-serif !important;
    border-radius: 6px !important;
    transition: all 0.2s;
    width: 100%;
}
.stButton > button:hover {
    border-color: var(--blue) !important;
    color: var(--blue) !important;
    background: var(--card2) !important;
}

/* ---- Chart link button — theme-aware ---- */
.stLinkButton > a {
    background: var(--card2) !important;
    border: 1px solid var(--border2) !important;
    color: var(--blue) !important;
    font-size: 12px !important;
    font-family: 'JetBrains Mono', monospace !important;
    border-radius: 6px !important;
    padding: 6px 12px !important;
    text-decoration: none !important;
    transition: all 0.2s;
    letter-spacing: 0.5px;
}
.stLinkButton > a:hover {
    background: var(--blue) !important;
    color: #ffffff !important;
    border-color: var(--blue) !important;
}

/* ---- KPI Metrics ---- */
div[data-testid="metric-container"] {
    background: var(--card) !important;
    border: 1px solid var(--border) !important;
    border-radius: 10px !important;
    padding: 16px 20px !important;
}
div[data-testid="metric-container"] label {
    color: var(--t3) !important;
    font-size: 11px !important;
    text-transform: uppercase !important;
    letter-spacing: 1.5px !important;
    font-weight: 600 !important;
}
div[data-testid="metric-container"] [data-testid="stMetricValue"] {
    color: var(--t1) !important;
    font-family: 'JetBrains Mono', monospace !important;
    font-size: 26px !important;
    font-weight: 600 !important;
}
div[data-testid="metric-container"] [data-testid="stMetricDelta"] {
    font-family: 'JetBrains Mono', monospace !important;
    font-size: 12px !important;
}

/* ---- Signal badges ---- */
.badge-buy {
    display: inline-block;
    background: var(--buy-bg); border: 1px solid var(--buy-br);
    color: var(--green); font-family: 'JetBrains Mono', monospace;
    font-size: 11px; font-weight: 700;
    padding: 4px 14px; border-radius: 4px; letter-spacing: 2px;
}
.badge-sell {
    display: inline-block;
    background: var(--sell-bg); border: 1px solid var(--sell-br);
    color: var(--red); font-family: 'JetBrains Mono', monospace;
    font-size: 11px; font-weight: 700;
    padding: 4px 12px; border-radius: 4px; letter-spacing: 2px;
}

/* ---- Pending badge ---- */
.badge-pending {
    display: inline-block;
    background: rgba(107,127,160,0.12);
    border: 1px dashed var(--t4);
    color: var(--t3); font-family: 'JetBrains Mono', monospace;
    font-size: 10px; font-weight: 500;
    padding: 3px 8px; border-radius: 4px; letter-spacing: 1px;
}

/* ---- Section headers ---- */
.sec-hdr {
    display: flex; align-items: center; gap: 10px;
    padding: 16px 0 12px 0;
    border-bottom: 1px solid var(--border);
    margin-bottom: 14px;
}
.sec-title {
    font-size: 12px; font-weight: 700; color: var(--t2);
    text-transform: uppercase; letter-spacing: 2.5px;
}
.sec-meta {
    font-size: 11px; color: var(--t3);
    margin-left: 6px; font-family: 'JetBrains Mono', monospace;
}

/* ---- Table column headers ---- */
.col-hdr {
    font-size: 11px; font-weight: 600; color: var(--t3);
    text-transform: uppercase; letter-spacing: 1.5px;
    padding: 8px 0; border-bottom: 1px solid var(--border);
}

/* ---- Signal row content ---- */
.stock-name {
    font-family: 'JetBrains Mono', monospace;
    font-size: 14px; font-weight: 700; color: var(--t1); line-height: 1.3;
}
.stock-sym {
    font-size: 11px; color: var(--t3); margin-top: 2px;
    font-family: 'JetBrains Mono', monospace;
}
.row-div { border-top: 1px solid var(--border); margin: 4px 0; opacity: 0.35; }

/* ---- No signal state ---- */
.no-sig {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 8px; padding: 18px 24px; text-align: center;
    color: var(--t3); font-size: 13px; margin-bottom: 20px;
    letter-spacing: 0.3px;
}

/* ---- Backtest card ---- */
.bt-card {
    background: var(--card2); border: 1px solid var(--border);
    border-left: 3px solid var(--blue);
    border-radius: 8px; padding: 14px 20px;
    display: flex; gap: 32px; align-items: center;
    margin-bottom: 16px; flex-wrap: wrap;
}
.bt-label-main {
    font-size: 10px; color: var(--t3); text-transform: uppercase;
    letter-spacing: 1.5px; font-weight: 600; margin-right: 4px;
}
.bt-item  { text-align: center; min-width: 90px; }
.bt-label { font-size: 10px; color: var(--t3); text-transform: uppercase; letter-spacing: 1.5px; margin-bottom: 4px; font-weight: 600; }
.bt-val   { font-family: 'JetBrains Mono', monospace; font-size: 16px; font-weight: 700; color: var(--t1); }
.bt-val.pos { color: var(--green); }
.bt-val.neg { color: var(--red);   }
.bt-val.neu { color: var(--t2);    }

/* ---- Pending backtest message ---- */
.bt-pending {
    background: var(--card2); border: 1px dashed var(--border2);
    border-radius: 8px; padding: 12px 18px;
    color: var(--t3); font-size: 12px;
    font-family: 'JetBrains Mono', monospace;
    margin-bottom: 16px; letter-spacing: 0.5px;
}

/* ---- Market status ---- */
.mkt-open {
    display: inline-flex; align-items: center; gap: 6px;
    background: rgba(30,201,160,0.12); border: 1px solid rgba(30,201,160,0.35);
    color: #1ec9a0; padding: 7px 18px; border-radius: 20px;
    font-size: 12px; font-weight: 700;
    font-family: 'JetBrains Mono', monospace; letter-spacing: 1px;
}
.mkt-closed {
    display: inline-flex; align-items: center; gap: 6px;
    background: rgba(240,85,85,0.12); border: 1px solid rgba(240,85,85,0.35);
    color: #f05555; padding: 7px 18px; border-radius: 20px;
    font-size: 12px; font-weight: 700;
    font-family: 'JetBrains Mono', monospace; letter-spacing: 1px;
}
.pulse {
    width: 8px; height: 8px; border-radius: 50%; background: currentColor;
    animation: pa 2s infinite;
}
@keyframes pa { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:0.3;transform:scale(0.7)} }

/* ---- Scheduler status ---- */
.sched-active {
    display: inline-flex; align-items: center; gap: 6px;
    font-size: 11px; color: var(--green);
    font-family: 'JetBrains Mono', monospace; letter-spacing: 0.5px;
}
.sched-stale {
    font-size: 11px; color: var(--amber);
    font-family: 'JetBrains Mono', monospace; letter-spacing: 0.5px;
}
.sched-never {
    font-size: 11px; color: var(--t3);
    font-family: 'JetBrains Mono', monospace; letter-spacing: 0.5px;
}

/* ---- Telegram status ---- */
.tg-ok {
    background: rgba(30,201,160,0.10); border: 1px solid rgba(30,201,160,0.30);
    border-radius: 6px; padding: 9px 14px;
    font-size: 11px; color: var(--green); font-weight: 600;
    font-family: 'JetBrains Mono', monospace; letter-spacing: 1px; text-align: center;
}
.tg-err {
    background: rgba(240,85,85,0.08); border: 1px solid rgba(240,85,85,0.25);
    border-radius: 6px; padding: 9px 14px;
    font-size: 11px; color: var(--red); font-weight: 600;
    font-family: 'JetBrains Mono', monospace; letter-spacing: 1px; text-align: center;
}

/* ---- Dataframe — fully themed ---- */
.stDataFrame,
.stDataFrame > div,
[data-testid="stDataFrameResizable"] {
    background: var(--df-bg) !important;
    color: var(--df-text) !important;
    border: 1px solid var(--border) !important;
    border-radius: 8px !important;
}
.stDataFrame th, .stDataFrame thead th {
    background: var(--df-hdr) !important;
    color: var(--t3) !important;
    font-size: 11px !important;
    font-weight: 600 !important;
    text-transform: uppercase !important;
    letter-spacing: 1px !important;
    border-bottom: 1px solid var(--border) !important;
}
.stDataFrame td {
    background: var(--df-bg) !important;
    color: var(--df-text) !important;
    font-size: 12px !important;
    font-family: 'JetBrains Mono', monospace !important;
    border-bottom: 1px solid var(--border) !important;
}
.stDataFrame tr:nth-child(even) td {
    background: var(--df-row-alt) !important;
}

/* ---- Scrollbar ---- */
::-webkit-scrollbar { width: 4px; height: 4px; }
::-webkit-scrollbar-track { background: var(--bg); }
::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 2px; }
"""

theme = DARK if st.session_state.dark_mode else LIGHT
st.markdown(f"<style>{theme}{SHARED}</style>", unsafe_allow_html=True)


# ============================================================
# HELPERS
# ============================================================

def market_open() -> bool:
    from datetime import time as dtime
    now = datetime.now(IST)
    if now.weekday() >= 5: return False
    return dtime(9, 15) <= now.time() <= dtime(15, 30)

def tv_url(tv_sym: str, tf: str) -> str:
    return f"https://www.tradingview.com/chart/?symbol={tv_sym}&interval={TV_INTERVALS.get(tf,'D')}"

def _stock_tv(sym: str) -> str:
    return f"NSE:{sym.replace('.NS','')}"

def rsi_style(rsi) -> str:
    try:
        v = float(rsi)
        if v < 30:   return "color:var(--red);font-weight:700;"
        elif v > 70: return "color:var(--amber);font-weight:700;"
        return "color:var(--t2);"
    except: return "color:var(--t3);"

def pnl_class(val) -> str:
    try:    return "pos" if float(val) >= 0 else "neg"
    except: return "neu"

def fmt_pnl(val) -> str:
    try:
        v = float(val)
        return f"{'+'if v>=0 else ''}₹{v:,.2f}"
    except: return None

def fmt_pct(val) -> str:
    try:
        v = float(val)
        return f"{'+'if v>=0 else ''}{v:.1f}%"
    except: return None

def fmt_rsi(val) -> str:
    try:    return str(round(float(val), 2))
    except: return "—"

def fmt_price(val) -> str:
    try:    return f"₹{float(val):,.2f}"
    except: return "—"

def get_latest_signals(tf: str) -> pd.DataFrame:
    logs = logger.get_logs()
    if logs.empty: return pd.DataFrame()
    tf_logs = logs[logs["Timeframe"] == tf].copy()
    if tf_logs.empty: return pd.DataFrame()
    tf_logs["_sort"] = pd.to_datetime(tf_logs["Timestamp"], errors="coerce")
    return (
        tf_logs.sort_values("_sort", ascending=False)
               .groupby("Stock").first()
               .reset_index()
               .drop(columns=["_sort"], errors="ignore")
    )

def get_last_scan_time() -> str:
    """Returns how long ago the scheduler last ran — reads from Supabase."""
    try:
        return _db_last_scan()
    except Exception:
        return None


# ============================================================
# SIDEBAR
# ============================================================

with st.sidebar:
    st.markdown(f"""
    <div style='padding:4px 0 20px;'>
        <div style='font-family:JetBrains Mono,monospace;font-size:16px;
                    font-weight:700;color:var(--blue);letter-spacing:2px;'>ALGO SIGNALS</div>
        <div style='font-size:11px;color:var(--t3);letter-spacing:2px;
                    text-transform:uppercase;margin-top:4px;'>NSE · BSE · MCX</div>
    </div>
    <div style='border-top:1px solid var(--border);margin-bottom:18px;'></div>
    """, unsafe_allow_html=True)

    btn_label = "☀️ Switch to Light" if st.session_state.dark_mode else "🌙 Switch to Dark"
    if st.button(btn_label, use_container_width=True):
        st.session_state.dark_mode = not st.session_state.dark_mode
        st.rerun()

    st.markdown("<div style='margin:14px 0;border-top:1px solid var(--border);'></div>", unsafe_allow_html=True)
    st.markdown('<div style="font-size:11px;font-weight:700;color:var(--t3);text-transform:uppercase;letter-spacing:2px;margin-bottom:8px;">Timeframe</div>', unsafe_allow_html=True)

    _tf_keys = list(TIMEFRAMES.keys())
    _tf_idx  = _tf_keys.index(st.session_state.selected_tf) if st.session_state.selected_tf in _tf_keys else 2
    selected_tf = st.selectbox("Timeframe", _tf_keys, index=_tf_idx, label_visibility="collapsed", key="tf_selectbox")
    st.session_state.selected_tf = selected_tf

    st.markdown("<div style='margin:14px 0;border-top:1px solid var(--border);'></div>", unsafe_allow_html=True)
    st.markdown('<div style="font-size:11px;font-weight:700;color:var(--t3);text-transform:uppercase;letter-spacing:2px;margin-bottom:8px;">Markets</div>', unsafe_allow_html=True)
    show_idx  = st.checkbox("Indexes",     value=True)
    show_stk  = st.checkbox("Stocks",      value=True)
    show_com  = st.checkbox("Commodities", value=True)

    st.markdown("<div style='margin:14px 0;border-top:1px solid var(--border);'></div>", unsafe_allow_html=True)

    tg = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if tg:
        st.markdown('<div class="tg-ok">✓ TELEGRAM CONNECTED</div>', unsafe_allow_html=True)
    else:
        st.markdown('<div class="tg-err">✗ TELEGRAM NOT SET</div>', unsafe_allow_html=True)

    st.markdown("<div style='margin:14px 0;border-top:1px solid var(--border);'></div>", unsafe_allow_html=True)

    # Scheduler status — shows last scan time
    last_scan = get_last_scan_time()
    fetch_period = PERIOD_MAP.get(selected_tf, "3mo")

    try:
        _is_recent = (
            last_scan == "just now" or
            (last_scan and last_scan.endswith("m ago") and int(last_scan.split("m")[0]) <= 10)
        )
    except (ValueError, IndexError):
        _is_recent = False
    if _is_recent:
        sched_html = f'<div class="sched-active"><span style="width:6px;height:6px;border-radius:50%;background:var(--green);display:inline-block;"></span> Last scan: {last_scan}</div>'
    elif last_scan:
        sched_html = f'<div class="sched-stale">⚠ Last scan: {last_scan}</div>'
    else:
        sched_html = '<div class="sched-never">No scans yet today</div>'

    st.markdown(f"""
    <div style='font-size:12px;color:var(--t3);line-height:2.4;'>
        <div>Viewing &nbsp;<span style='color:var(--t2);font-family:JetBrains Mono,monospace;font-weight:600;'>{selected_tf}</span></div>
        <div>Period &nbsp;&nbsp;<span style='color:var(--t2);font-family:JetBrains Mono,monospace;font-weight:600;'>{fetch_period}</span></div>
        <div>Refresh &nbsp;<span style='color:var(--t2);font-family:JetBrains Mono,monospace;font-weight:600;'>60s</span></div>
        <div style='margin-top:4px;'>{sched_html}</div>
    </div>
    """, unsafe_allow_html=True)


ist_now = datetime.now(IST)
is_open = market_open()


# ============================================================
# HEADER
# ============================================================

hl, hr = st.columns([5, 1])
with hl:
    st.markdown(f"""
    <div style='padding:8px 0 4px;'>
        <h1 style='font-family:IBM Plex Sans,sans-serif;font-size:30px;
                   font-weight:700;color:var(--t1);letter-spacing:-0.5px;margin:0;'>
            Signal Dashboard
        </h1>
        <div style='font-size:12px;color:var(--t3);margin-top:6px;font-family:JetBrains Mono,monospace;'>
            {ist_now.strftime('%d %b %Y &nbsp;·&nbsp; %H:%M:%S IST')}
            &nbsp;·&nbsp; {selected_tf} &nbsp;·&nbsp; Auto-refresh 60s
        </div>
    </div>
    """, unsafe_allow_html=True)
with hr:
    st.markdown("<div style='height:18px;'></div>", unsafe_allow_html=True)
    if is_open:
        st.markdown('<div style="text-align:right;"><span class="mkt-open"><span class="pulse"></span>MARKET OPEN</span></div>', unsafe_allow_html=True)
    else:
        st.markdown('<div style="text-align:right;"><span class="mkt-closed"><span class="pulse"></span>MARKET CLOSED</span></div>', unsafe_allow_html=True)

st.markdown("<div style='border-top:1px solid var(--border);margin:16px 0 24px;'></div>", unsafe_allow_html=True)


# ============================================================
# LOAD DATA
# ============================================================

all_logs       = logger.get_logs()
latest_signals = get_latest_signals(selected_tf)
backtest_data  = get_results(selected_tf)
total_buy = total_sell = total_hold = 0


def build_rows(symbols, display_map, tv_map, skip_tf=None):
    global total_buy, total_sell, total_hold
    rows = []
    for sym in symbols:
        if skip_tf and selected_tf in skip_tf:
            continue
        name = display_map.get(sym, sym.replace(".NS",""))
        tv   = tv_map.get(sym, sym)

        if not latest_signals.empty and sym in latest_signals["Stock"].values:
            d     = latest_signals[latest_signals["Stock"] == sym].iloc[0]
            sig   = str(d["Signal"])
            rsi   = d["RSI"]
            price = d["Price"]
            try:
                _ts = pd.to_datetime(d["Timestamp"], utc=True).tz_convert(IST)
                ts  = _ts.strftime("%Y-%m-%d %H:%M IST")
            except Exception:
                ts  = str(d["Timestamp"])[:16]
        else:
            sig = "HOLD"; rsi = "—"; price = "—"; ts = "—"

        bt = {}
        if not backtest_data.empty and sym in backtest_data["Symbol"].values:
            br = backtest_data[backtest_data["Symbol"] == sym].iloc[0]
            trades = int(br.get("Trades", 0))
            if trades > 0:
                bt = {
                    "trades":   trades,
                    "pnl":      br.get("PnL",       0.0),
                    "pnl_pct":  br.get("PnL %",     0.0),
                    "win_rate": br.get("Win Rate %", 0.0),
                }

        if sig == "BUY":    total_buy  += 1
        elif sig == "SELL": total_sell += 1
        else:               total_hold += 1

        rows.append({
            "sym": sym, "name": name, "tv": tv_url(tv, selected_tf),
            "signal": sig, "sig_rsi": rsi, "sig_price": price,
            "ts": ts, "bt": bt,
        })
    return rows


idx_rows = build_rows(INDEXES, INDEXES_DISPLAY, INDEXES_TV) if show_idx else []
stk_rows = build_rows(STOCKS, {s:stock_display(s) for s in STOCKS}, {s:_stock_tv(s) for s in STOCKS}) if show_stk else []
com_rows = build_rows(COMMODITIES, COMMODITIES_DISPLAY, COMMODITIES_TV, skip_tf=COMMODITIES_SKIP_TIMEFRAMES) if show_com else []


# ============================================================
# GLOBAL KPI BAR
# ============================================================

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Total Scanned", total_buy + total_sell + total_hold)
k2.metric("BUY Signals",   total_buy,  delta=f"+{total_buy}"  if total_buy  > 0 else None)
k3.metric("SELL Signals",  total_sell, delta=f"-{total_sell}" if total_sell > 0 else None, delta_color="inverse")
k4.metric("HOLD",          total_hold)
k5.metric("Timeframe",     selected_tf)
st.markdown("<div style='margin:28px 0 10px;'></div>", unsafe_allow_html=True)


# ============================================================
# BACKTEST SUMMARY — with pending state
# ============================================================

def backtest_summary_bar(rows: list[dict], period: str) -> None:
    bt_rows = [r["bt"] for r in rows if r.get("bt") and r["bt"].get("trades", 0) > 0]

    if not bt_rows:
        # Show pending message instead of nothing
        st.markdown(f"""
        <div class="bt-pending">
            Backtest ({period}) — awaiting first scheduler scan for this timeframe
        </div>
        """, unsafe_allow_html=True)
        return

    total_trades = sum(b.get("trades", 0) for b in bt_rows)
    total_pnl    = sum(b.get("pnl",    0) for b in bt_rows)
    avg_wr       = round(sum(b.get("win_rate", 0) for b in bt_rows) / len(bt_rows), 1)
    avg_pct      = round(sum(b.get("pnl_pct",  0) for b in bt_rows) / len(bt_rows), 1)
    pnl_c = pnl_class(total_pnl)
    pct_c = pnl_class(avg_pct)
    wr_c  = "pos" if avg_wr >= 50 else "neg"

    pnl_str = fmt_pnl(total_pnl) or "—"
    pct_str = fmt_pct(avg_pct)   or "—"

    st.markdown(f"""
    <div class="bt-card">
        <div>
            <div class="bt-label-main">Backtest</div>
            <div style='font-size:11px;color:var(--t3);font-family:JetBrains Mono,monospace;'>({period})</div>
        </div>
        <div class="bt-item">
            <div class="bt-label">Trades</div>
            <div class="bt-val neu">{total_trades}</div>
        </div>
        <div class="bt-item">
            <div class="bt-label">Total PnL</div>
            <div class="bt-val {pnl_c}">{pnl_str}</div>
        </div>
        <div class="bt-item">
            <div class="bt-label">Avg PnL %</div>
            <div class="bt-val {pct_c}">{pct_str}</div>
        </div>
        <div class="bt-item">
            <div class="bt-label">Avg Win Rate</div>
            <div class="bt-val {wr_c}">{avg_wr}%</div>
        </div>
    </div>
    """, unsafe_allow_html=True)


# ============================================================
# RENDER SECTION
# ============================================================

def render_section(rows, title, dot_color="#4a90e2"):
    if not rows: return

    action    = [r for r in rows if r["signal"] in ("BUY","SELL")]
    holds     = len([r for r in rows if r["signal"] == "HOLD"])
    act_color = "var(--green)" if action else "var(--t3)"

    st.markdown(f"""
    <div class="sec-hdr">
        <div style='width:7px;height:7px;border-radius:50%;
                    background:{dot_color};flex-shrink:0;'></div>
        <span class="sec-title">{title}</span>
        <span class="sec-meta">
            {len(rows)} instruments &nbsp;·&nbsp;
            <span style='color:{act_color};font-weight:700;'>{len(action)} active</span>
            &nbsp;·&nbsp; {holds} hold
        </span>
    </div>
    """, unsafe_allow_html=True)

    backtest_summary_bar(rows, fetch_period)

    if not action:
        st.markdown('<div class="no-sig">No active signals — all instruments HOLD</div>', unsafe_allow_html=True)
        return

    # Sort most recent signal first
    action = sorted(action, key=lambda r: str(r.get("ts","—")), reverse=True)

    # Column headers
    h = st.columns([2.2, 0.9, 0.8, 1.1, 0.8, 1.2, 1.0, 1.0, 1.6, 0.7])
    for col, lbl in zip(h, ["Instrument","Signal","Sig RSI","Sig Price",
                             "Cur RSI","Cur Price","PnL","Win Rate","Signal Time","Chart"]):
        col.markdown(f'<div class="col-hdr">{lbl}</div>', unsafe_allow_html=True)

    for row in action:
        c  = st.columns([2.2, 0.9, 0.8, 1.1, 0.8, 1.2, 1.0, 1.0, 1.6, 0.7])
        bt = row.get("bt", {})

        # Live RSI/Price fetch — fallback to signal values
        cur_rsi   = row["sig_rsi"]
        cur_price = row["sig_price"]
        cur_live  = False
        try:
            _df = provider.fetch_data(
                symbol=row["sym"],
                interval=TIMEFRAMES[selected_tf],
                period=PERIOD_MAP[selected_tf],
            )
            if _df is not None and not _df.empty and len(_df) >= 15:
                _close = _df["Close"].squeeze()
                if hasattr(_close, "iloc"):
                    _df["RSI"] = rsi_indicator.calculate(_close)
                    _df.dropna(subset=["RSI"], inplace=True)
                    if not _df.empty:
                        _lt       = _df.iloc[-1]
                        cur_rsi   = round(float(_lt["RSI"]), 2)
                        cur_price = round(float(_lt["Close"]), 2)
                        cur_live  = True
        except Exception:
            pass

        # Instrument
        with c[0]:
            st.markdown(f"""
            <div style='padding:12px 0 10px;'>
                <div class="stock-name">{row['name']}</div>
                <div class="stock-sym">{row['sym']}</div>
            </div>""", unsafe_allow_html=True)

        # Signal badge
        with c[1]:
            badge = '<span class="badge-buy">BUY</span>' if row["signal"] == "BUY" \
                    else '<span class="badge-sell">SELL</span>'
            st.markdown(f"<div style='padding:14px 0;'>{badge}</div>", unsafe_allow_html=True)

        # Sig RSI
        with c[2]:
            _sv = fmt_rsi(row["sig_rsi"])
            _ss = rsi_style(row["sig_rsi"])
            st.markdown(
                f"<div style='padding:14px 0;font-family:JetBrains Mono,monospace;"
                f"font-size:13px;{_ss}'>{_sv}</div>",
                unsafe_allow_html=True)

        # Sig Price
        with c[3]:
            st.markdown(
                f"<div style='padding:14px 0;font-family:JetBrains Mono,monospace;"
                f"font-size:12px;color:var(--t3);'>{fmt_price(row['sig_price'])}</div>",
                unsafe_allow_html=True)

        # Cur RSI
        with c[4]:
            _cs = rsi_style(cur_rsi)
            live_dot = "<span style='width:5px;height:5px;border-radius:50%;background:var(--green);display:inline-block;margin-left:4px;vertical-align:middle;'></span>" if cur_live else ""
            st.markdown(
                f"<div style='padding:14px 0;font-family:JetBrains Mono,monospace;"
                f"font-size:13px;{_cs}'>{fmt_rsi(cur_rsi)}{live_dot}</div>",
                unsafe_allow_html=True)

        # Cur Price
        with c[5]:
            cp = fmt_price(cur_price)
            try:
                diff = float(cur_price) - float(row["sig_price"])
                if not cur_live:
                    cp_c   = "var(--t2)"
                    cp_tag = "<span style='font-size:10px;color:var(--t3);margin-left:4px;'>last</span>"
                else:
                    cp_c   = "var(--green)" if diff >= 0 else "var(--red)"
                    arrow  = "▲" if diff >= 0 else "▼"
                    diff_s = f"{'+' if diff>=0 else ''}{diff:,.2f}"
                    cp_tag = f"<span style='font-size:10px;color:{cp_c};margin-left:4px;'>{arrow}{diff_s}</span>"
            except:
                cp_c = "var(--t1)"; cp_tag = ""
            st.markdown(
                f"<div style='padding:14px 0;font-family:JetBrains Mono,monospace;"
                f"font-size:13px;color:{cp_c};'>{cp}{cp_tag}</div>",
                unsafe_allow_html=True)

        # PnL
        with c[6]:
            pnl_v = bt.get("pnl", None)
            if pnl_v is not None:
                pnl_s = fmt_pnl(pnl_v) or "—"
                color = "var(--green)" if pnl_class(pnl_v)=="pos" else ("var(--red)" if pnl_class(pnl_v)=="neg" else "var(--t3)")
                content = f"<span style='color:{color};'>{pnl_s}</span>"
            else:
                content = '<span class="badge-pending">pending</span>'
            st.markdown(
                f"<div style='padding:14px 0;font-family:JetBrains Mono,monospace;font-size:12px;'>{content}</div>",
                unsafe_allow_html=True)

        # Win Rate
        with c[7]:
            wr_v = bt.get("win_rate", None)
            if wr_v is not None:
                wr_s = f"{wr_v:.1f}%"
                wr_c = "var(--green)" if wr_v >= 50 else "var(--red)"
                content = f"<span style='color:{wr_c};'>{wr_s}</span>"
            else:
                content = '<span class="badge-pending">pending</span>'
            st.markdown(
                f"<div style='padding:14px 0;font-family:JetBrains Mono,monospace;font-size:12px;'>{content}</div>",
                unsafe_allow_html=True)

        # Signal Time
        with c[8]:
            st.markdown(
                f"<div style='padding:14px 0;font-family:JetBrains Mono,monospace;"
                f"font-size:12px;color:var(--t3);'>{row['ts']}</div>",
                unsafe_allow_html=True)

        # Chart — themed link button
        with c[9]:
            st.link_button("↗ Chart", row["tv"], use_container_width=True)

        st.markdown("<div class='row-div'></div>", unsafe_allow_html=True)

    st.markdown("<div style='margin-bottom:28px;'></div>", unsafe_allow_html=True)


# ============================================================
# RENDER ALL SECTIONS
# ============================================================

if show_idx:  render_section(idx_rows, "INDEXES",                    "#9b6dff")
if show_stk:  render_section(stk_rows, "NSE STOCKS — F&O WATCHLIST", "#4a90e2")
if show_com:  render_section(com_rows, "COMMODITIES — MCX",          "#f7a800")


# ============================================================
# SIGNAL HISTORY
# ============================================================

st.markdown("""
<div class="sec-hdr" style='margin-top:10px;'>
    <div style='width:7px;height:7px;border-radius:50%;background:var(--green);flex-shrink:0;'></div>
    <span class="sec-title">Signal History — Last 7 Days</span>
</div>
""", unsafe_allow_html=True)

try:
    if all_logs.empty:
        st.markdown('<div class="no-sig">No signals yet. Start <code>run_scheduler.py</code> or wait for GitHub Actions to run.</div>', unsafe_allow_html=True)
    else:
        logs_tf = all_logs[all_logs["Timeframe"] == selected_tf].copy()
        if logs_tf.empty:
            st.markdown(f'<div class="no-sig">No signals for <strong>{selected_tf}</strong> timeframe yet.</div>', unsafe_allow_html=True)
        else:
            # Format columns cleanly — no raw floats
            display = logs_tf[["Timestamp","Stock","Signal","RSI","Price"]].copy()
            # Convert UTC timestamps to IST
            try:
                display["Timestamp"] = pd.to_datetime(display["Timestamp"], utc=True).dt.tz_convert(IST).dt.strftime("%Y-%m-%d %H:%M IST")
            except Exception:
                pass
            # Replace commodity/stock codes with friendly display names
            _name_map = {
                **COMMODITIES_DISPLAY,
                **{s: stock_display(s) for s in STOCKS},
                **INDEXES_DISPLAY,
            }
            display["Stock"] = display["Stock"].apply(lambda x: _name_map.get(x, x))
            display["RSI"]   = display["RSI"].apply(lambda x: f"{float(x):.2f}" if str(x).replace('.','').replace('-','').isdigit() else x)
            display["Price"] = display["Price"].apply(lambda x: f"₹{float(x):,.2f}" if str(x).replace('.','').replace('-','').isdigit() else x)

            is_dark = st.session_state.dark_mode
            buy_bg  = "#0d2e1c" if is_dark else "#d4f7ec"
            sell_bg = "#2e0d0d" if is_dark else "#fde8e8"
            buy_fg  = "#1ec9a0" if is_dark else "#065f46"
            sell_fg = "#f05555" if is_dark else "#991b1b"

            def _col(v):
                if v=="BUY":  return f"background:{buy_bg};color:{buy_fg};font-weight:700;font-family:JetBrains Mono,monospace;font-size:12px;"
                if v=="SELL": return f"background:{sell_bg};color:{sell_fg};font-weight:700;font-family:JetBrains Mono,monospace;font-size:12px;"
                return f"font-family:JetBrains Mono,monospace;font-size:12px;"

            # Height based on number of rows — no unnecessary empty space
            row_count  = len(display)
            tbl_height = min(max(row_count * 42 + 48, 100), 400)

            st.dataframe(
                display.style.map(_col, subset=["Signal"]),
                use_container_width=True,
                hide_index=True,
                height=tbl_height,
            )
except Exception as e:
    st.warning(f"Signal history unavailable: {e}")


# ============================================================
# FOOTER
# ============================================================

st.markdown("""
<div style='border-top:1px solid var(--border);margin-top:40px;padding-top:16px;
            font-size:11px;color:var(--t4);text-align:center;
            font-family:JetBrains Mono,monospace;letter-spacing:1px;'>
    FOR RESEARCH & INFORMATIONAL PURPOSES ONLY &nbsp;·&nbsp;
    NOT FINANCIAL ADVICE &nbsp;·&nbsp; TRADE AT YOUR OWN RISK
</div>
""", unsafe_allow_html=True)