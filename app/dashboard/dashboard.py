# ============================================================
# app/dashboard/dashboard.py
# ============================================================

import sys
import os
import json
import contextlib

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
sys.path.append(_project_root)

from dotenv import load_dotenv
load_dotenv(os.path.join(_project_root, ".env"))

import streamlit as st
import pandas as pd
from datetime import datetime
import pytz
from streamlit_autorefresh import st_autorefresh

from data.providers.upstox_provider import UpstoxProvider
from core.indicators.indicators import add_rsi, add_bollinger_bands, add_ema, add_pivot_points
from core.logger.signal_logger import SignalLogger
from core.backtesting.backtest_store import get_results
from core.database import get_last_scan_time as _db_last_scan
from core.strategies.strategies import STRATEGY_NAMES
from configs.instruments import (
    INDEXES, INDEXES_DISPLAY, INDEXES_TV,
    COMMODITIES, COMMODITIES_DISPLAY, COMMODITIES_TV,
)
from configs.universe import get_fno_universe, FALLBACK_FNO_SYMBOLS
from configs.timeframes import TIMEFRAMES, TV_INTERVALS, PERIOD_MAP

ALL_STRATEGY_NAMES = ["All Strategies"] + STRATEGY_NAMES + ["Cash-Futures Arbitrage"]

# ============================================================
# PAGE CONFIG
# ============================================================

st.set_page_config(
    page_title="Algo Trading | Signal Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ============================================================
# LOGIN GATE (Jwala, Jul 11: "I think we need to put some login
# credentials also")
#
# Interim app-level auth via streamlit-authenticator — not Azure Easy
# Auth (confirmed blocked: 401 on Entra ID App registration, Jul 13).
# Cookie-based session so the 5-min autorefresh below doesn't force
# re-login on every rerun — only on real browser refresh/cookie
# expiry (7 days, configs/auth_config.yaml).
#
# Must run BEFORE st_autorefresh and everything else, so an
# unauthenticated visitor never triggers any dashboard logic at all —
# not even the refresh timer.
#
# To add/remove people or set real passwords: see the instructions at
# the top of configs/auth_config.yaml and scripts/hash_password.py.
# ============================================================

import yaml
import streamlit_authenticator as stauth
from streamlit_authenticator.utilities.exceptions import LoginError

_AUTH_CONFIG_PATH = os.path.join(_project_root, "configs", "auth_config.yaml")

# NOT wrapped in @st.cache_resource — deliberately. Authenticate()
# constructs a CookieManager, which is a per-browser-session component;
# caching it as a shared resource would hand every visitor the SAME
# authenticator/cookie-manager instance, which is wrong for per-user
# cookies and also triggers Streamlit's "widget created inside a
# cached function" warning (caught by AppTest before this shipped).
# Rebuilding it each run is cheap — a small YAML read + dict wrap.
try:
    with open(_AUTH_CONFIG_PATH, "r", encoding="utf-8") as f:
        _auth_cfg = yaml.safe_load(f)
    authenticator = stauth.Authenticate(
        credentials=_auth_cfg["credentials"],
        cookie_name=_auth_cfg["cookie"]["name"],
        cookie_key=_auth_cfg["cookie"]["key"],
        cookie_expiry_days=_auth_cfg["cookie"]["expiry_days"],
    )
except Exception as e:
    st.error(
        f"Login config could not be loaded ({e}). "
        f"Check configs/auth_config.yaml exists and real password hashes "
        f"have replaced the REPLACE_ME_* placeholders — see "
        f"scripts/hash_password.py."
    )
    st.stop()

# LoginError fires specifically when someone has a still-valid browser
# cookie for a username that's since been REMOVED from auth_config.yaml
# (access revoked) — without this catch it shows an uncaught traceback
# instead of a clean message. Confirmed by reading the library source:
# this is the only way LoginError fires given we don't use
# single_session/max_concurrent_users/max_login_attempts.
try:
    authenticator.login(location="main")
except LoginError:
    st.error("Your access has been removed. Contact the admin if this is unexpected.")
    st.stop()

if st.session_state.get("authentication_status") is False:
    st.error("Username or password is incorrect.")
    st.stop()
elif st.session_state.get("authentication_status") is not True:
    st.stop()  # not yet submitted — login form is already showing, nothing else to render

st_autorefresh(interval=300000, key="dashboard_refresh")  # 5 min — matches scheduler
IST = pytz.timezone("Asia/Kolkata")

# ============================================================
# SESSION STATE
# ============================================================

if "dark_mode"        not in st.session_state: st.session_state.dark_mode        = True
if "selected_tf"      not in st.session_state: st.session_state.selected_tf      = "1 Hour"
if "selected_strategy"not in st.session_state: st.session_state.selected_strategy= "RSI Reversal"
if "chart_symbol"     not in st.session_state: st.session_state.chart_symbol      = None
if "chart_name"       not in st.session_state: st.session_state.chart_name        = None

if "provider" not in st.session_state:
    st.session_state.provider      = UpstoxProvider()
    st.session_state.logger        = SignalLogger()

provider = st.session_state.provider
logger   = st.session_state.logger

# ============================================================
# CSS
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
    --chart-bg:   #0d1526;
    --chart-grid: #1a2840;
    --chart-text: #6b7fa0;
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
    --chart-bg:   #ffffff;
    --chart-grid: #e8edf5;
    --chart-text: #7a8fad;
}
"""

SHARED = """
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap');

.stApp { background: var(--bg) !important; font-family: 'IBM Plex Sans', sans-serif; }
section[data-testid="stSidebar"] { background: var(--bg2) !important; border-right: 1px solid var(--border) !important; }
#MainMenu, footer { visibility: hidden; }
.viewerBadge_container__r5tak { display: none; }
.stApp > header { background: transparent !important; }
.stApp p, .stApp div:not([class]), .stApp span:not([class]) { color: var(--t1); }

/* Selectbox */
.stSelectbox > div > div { background: var(--card) !important; border-color: var(--border2) !important; color: var(--t1) !important; font-size: 13px !important; }
.stSelectbox label { color: var(--t2) !important; font-size: 11px !important; }
div[data-baseweb="select"] span { color: var(--t1) !important; }

/* Checkbox */
.stCheckbox label p, .stCheckbox label span { color: var(--t2) !important; font-size: 13px !important; }

/* Button */
.stButton > button {
    background: var(--card) !important; border: 1px solid var(--border2) !important;
    color: var(--t2) !important; font-size: 12px !important;
    font-family: 'IBM Plex Sans', sans-serif !important;
    border-radius: 6px !important; transition: all 0.2s; width: 100%;
}
.stButton > button:hover { border-color: var(--blue) !important; color: var(--blue) !important; background: var(--card2) !important; }

/* KPI Metrics */
div[data-testid="metric-container"] { background: var(--card) !important; border: 1px solid var(--border) !important; border-radius: 10px !important; padding: 16px 20px !important; }
div[data-testid="metric-container"] label { color: var(--t3) !important; font-size: 11px !important; text-transform: uppercase !important; letter-spacing: 1.5px !important; font-weight: 600 !important; }
div[data-testid="metric-container"] [data-testid="stMetricValue"] { color: var(--t1) !important; font-family: 'JetBrains Mono', monospace !important; font-size: 26px !important; font-weight: 600 !important; }

/* Signal badges */
.badge-buy { display:inline-block; background:var(--buy-bg); border:1px solid var(--buy-br); color:var(--green); font-family:'JetBrains Mono',monospace; font-size:11px; font-weight:700; padding:4px 14px; border-radius:4px; letter-spacing:2px; }
.badge-sell { display:inline-block; background:var(--sell-bg); border:1px solid var(--sell-br); color:var(--red); font-family:'JetBrains Mono',monospace; font-size:11px; font-weight:700; padding:4px 12px; border-radius:4px; letter-spacing:2px; }
.badge-strong { display:inline-block; background:rgba(155,109,255,0.12); border:1px solid rgba(155,109,255,0.35); color:var(--purple); font-family:'JetBrains Mono',monospace; font-size:10px; font-weight:600; padding:2px 8px; border-radius:4px; letter-spacing:1px; }
.badge-moderate { display:inline-block; background:rgba(247,168,0,0.12); border:1px solid rgba(247,168,0,0.35); color:var(--amber); font-family:'JetBrains Mono',monospace; font-size:10px; font-weight:600; padding:2px 8px; border-radius:4px; letter-spacing:1px; }
.badge-pending { display:inline-block; background:rgba(107,127,160,0.12); border:1px dashed var(--t4); color:var(--t3); font-family:'JetBrains Mono',monospace; font-size:10px; font-weight:500; padding:3px 8px; border-radius:4px; letter-spacing:1px; }

/* Section headers */
.sec-hdr { display:flex; align-items:center; gap:10px; padding:16px 0 12px 0; border-bottom:1px solid var(--border); margin-bottom:14px; }
.sec-title { font-size:12px; font-weight:700; color:var(--t2); text-transform:uppercase; letter-spacing:2.5px; }
.sec-meta { font-size:11px; color:var(--t3); margin-left:6px; font-family:'JetBrains Mono',monospace; }

/* Table */
.col-hdr { font-size:11px; font-weight:600; color:var(--t3); text-transform:uppercase; letter-spacing:1.5px; padding:8px 0; border-bottom:1px solid var(--border); }
.stock-name { font-family:'JetBrains Mono',monospace; font-size:13px; font-weight:700; color:var(--t1); line-height:1.3; }
.stock-sym { font-size:10px; color:var(--t3); margin-top:2px; font-family:'JetBrains Mono',monospace; }
.row-div { border-top:1px solid var(--border); margin:4px 0; opacity:0.35; }
.no-sig { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:18px 24px; text-align:center; color:var(--t3); font-size:13px; margin-bottom:20px; }

/* Chart button */
.chart-btn { cursor:pointer; background:var(--card2); border:1px solid var(--border2); color:var(--blue); font-size:11px; font-family:'JetBrains Mono',monospace; border-radius:5px; padding:5px 10px; letter-spacing:0.5px; transition:all 0.2s; }
.chart-btn:hover { background:var(--blue); color:#fff; }

/* Chart container */
.chart-container { background:var(--card); border:1px solid var(--border2); border-radius:10px; margin:12px 0; padding:16px; }

/* Market status */
.mkt-open { display:inline-flex; align-items:center; gap:6px; background:rgba(30,201,160,0.12); border:1px solid rgba(30,201,160,0.35); color:#1ec9a0; padding:7px 18px; border-radius:20px; font-size:12px; font-weight:700; font-family:'JetBrains Mono',monospace; letter-spacing:1px; }
.mkt-closed { display:inline-flex; align-items:center; gap:6px; background:rgba(240,85,85,0.12); border:1px solid rgba(240,85,85,0.35); color:#f05555; padding:7px 18px; border-radius:20px; font-size:12px; font-weight:700; font-family:'JetBrains Mono',monospace; letter-spacing:1px; }

/* Dark mode tooltip / popover / dropdown fixes */
div[data-baseweb="tooltip"] { background:var(--card2) !important; color:var(--t1) !important; border:1px solid var(--border2) !important; border-radius:6px !important; }
div[data-baseweb="popover"] > div { background:var(--card2) !important; border:1px solid var(--border2) !important; }
div[data-baseweb="menu"] { background:var(--card2) !important; color:var(--t1) !important; }
div[data-baseweb="menu"] li:hover { background:var(--blue) !important; color:#fff !important; }
div[data-testid="stSelectbox"] li { color:var(--t1) !important; background:var(--card2) !important; }
div[data-testid="stTextInput"] input { background:var(--card) !important; color:var(--t1) !important; border-color:var(--border2) !important; }
div[data-testid="stTextInput"] input::placeholder { color:var(--t3) !important; }

/* ── Dark mode tooltip / popover / dropdown fixes ── */
div[data-baseweb="tooltip"] { background:var(--card2) !important; color:var(--t1) !important; border:1px solid var(--border2) !important; border-radius:6px !important; font-size:12px !important; }
div[data-baseweb="popover"] { background:var(--card2) !important; border:1px solid var(--border2) !important; }
div[data-baseweb="menu"] { background:var(--card2) !important; color:var(--t1) !important; border:1px solid var(--border2) !important; }
div[data-baseweb="menu"] li { color:var(--t1) !important; }
div[data-baseweb="menu"] li:hover { background:var(--card) !important; color:var(--blue) !important; }
div[data-baseweb="select"] div { color:var(--t1) !important; }

/* Streamlit selectbox dropdown options */
div[data-testid="stSelectbox"] ul { background:var(--card2) !important; border:1px solid var(--border2) !important; }
div[data-testid="stSelectbox"] li { color:var(--t1) !important; background:var(--card2) !important; }
div[data-testid="stSelectbox"] li:hover { background:var(--card) !important; color:var(--blue) !important; }

/* Streamlit text input */
div[data-testid="stTextInput"] input { background:var(--card) !important; color:var(--t1) !important; border-color:var(--border2) !important; }
div[data-testid="stTextInput"] input::placeholder { color:var(--t3) !important; }

/* Dataframe dark mode */
div[data-testid="stDataFrame"] { background:var(--card) !important; }
.dvn-scroller { background:var(--card) !important; }
.pulse { width:8px; height:8px; border-radius:50%; background:currentColor; animation:pa 2s infinite; }
@keyframes pa { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:0.3;transform:scale(0.7)} }

/* Scheduler */
.sched-active { display:inline-flex; align-items:center; gap:6px; font-size:11px; color:var(--green); font-family:'JetBrains Mono',monospace; }
.sched-stale { font-size:11px; color:var(--amber); font-family:'JetBrains Mono',monospace; }
.sched-never { font-size:11px; color:var(--t3); font-family:'JetBrains Mono',monospace; }

/* Telegram */
.tg-ok { background:rgba(30,201,160,0.10); border:1px solid rgba(30,201,160,0.30); border-radius:6px; padding:9px 14px; font-size:11px; color:var(--green); font-weight:600; font-family:'JetBrains Mono',monospace; letter-spacing:1px; text-align:center; }
.tg-err { background:rgba(240,85,85,0.08); border:1px solid rgba(240,85,85,0.25); border-radius:6px; padding:9px 14px; font-size:11px; color:var(--red); font-weight:600; font-family:'JetBrains Mono',monospace; letter-spacing:1px; text-align:center; }

/* Strategy badge */
.strategy-pill { display:inline-block; background:rgba(74,144,226,0.12); border:1px solid rgba(74,144,226,0.35); color:var(--blue); font-family:'JetBrains Mono',monospace; font-size:10px; font-weight:600; padding:3px 10px; border-radius:20px; letter-spacing:1px; }

/* Dataframe */
.stDataFrame, .stDataFrame > div, [data-testid="stDataFrameResizable"] { background:var(--df-bg) !important; color:var(--df-text) !important; border:1px solid var(--border) !important; border-radius:8px !important; }
.stDataFrame th { background:var(--df-hdr) !important; color:var(--t3) !important; font-size:11px !important; font-weight:600 !important; text-transform:uppercase !important; letter-spacing:1px !important; }
.stDataFrame td { background:var(--df-bg) !important; color:var(--df-text) !important; font-size:12px !important; font-family:'JetBrains Mono',monospace !important; }
.stDataFrame tr:nth-child(even) td { background:var(--df-row-alt) !important; }

/* Backtest card */
.bt-card { background:var(--card2); border:1px solid var(--border); border-left:3px solid var(--blue); border-radius:8px; padding:14px 20px; display:flex; gap:32px; align-items:center; margin-bottom:16px; flex-wrap:wrap; }
.bt-item { text-align:center; min-width:90px; }
.bt-label { font-size:10px; color:var(--t3); text-transform:uppercase; letter-spacing:1.5px; margin-bottom:4px; font-weight:600; }
.bt-val { font-family:'JetBrains Mono',monospace; font-size:16px; font-weight:700; color:var(--t1); }
.bt-val.pos { color:var(--green); }
.bt-val.neg { color:var(--red); }
.bt-pending { background:var(--card2); border:1px dashed var(--border2); border-radius:8px; padding:12px 18px; color:var(--t3); font-size:12px; font-family:'JetBrains Mono',monospace; margin-bottom:16px; }

::-webkit-scrollbar { width:4px; height:4px; }
::-webkit-scrollbar-track { background:var(--bg); }
::-webkit-scrollbar-thumb { background:var(--border2); border-radius:2px; }

/* Responsive — MacBook Air 13" and smaller screens */
@media (max-width: 1400px) {
    .stock-name { font-size:12px !important; }
    .col-hdr { font-size:10px !important; letter-spacing:0.8px !important; }
    div[data-testid="metric-container"] [data-testid="stMetricValue"] { font-size:20px !important; }
}
@media (max-width: 1280px) {
    .stock-name { font-size:11px !important; }
    .stock-sym  { font-size:9px !important; }
    .badge-buy, .badge-sell { font-size:10px !important; padding:3px 8px !important; }
    .col-hdr { font-size:9px !important; letter-spacing:0.5px !important; }
    div[data-testid="metric-container"] [data-testid="stMetricValue"] { font-size:18px !important; }
    div[data-testid="metric-container"] { padding:10px 12px !important; }
}
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

def get_latest_signals(tf: str, strategy: str) -> pd.DataFrame:
    # "All Strategies" shows the latest signal from EACH strategy per stock
    # (so an RSI signal and a Volume Spike signal on the same stock both
    #  appear — two strategies agreeing is stronger conviction, not noise).
    # A single selected strategy collapses to one row per stock as before.
    strat_filter = None if strategy == "All Strategies" else strategy
    logs = logger.get_logs(strategy=strat_filter)
    if logs.empty: return pd.DataFrame()
    tf_logs = logs[logs["Timeframe"] == tf].copy()
    if tf_logs.empty: return pd.DataFrame()
    tf_logs["_sort"] = pd.to_datetime(tf_logs["Timestamp"], errors="coerce")

    if strategy == "All Strategies" and "Strategy" in tf_logs.columns:
        group_keys = ["Stock", "Strategy"]
    else:
        group_keys = ["Stock"]

    return (
        tf_logs.sort_values("_sort", ascending=False)
               .groupby(group_keys).first()
               .reset_index()
               .drop(columns=["_sort"], errors="ignore")
               .sort_values("Stock")
               .reset_index(drop=True)
    )

def get_last_scan_time() -> str:
    try:    return _db_last_scan()
    except: return None

def stock_display(sym: str) -> str:
    return sym.replace(".NS", "")

# ── Strategy color-coding (Jwala Jul 11: "I see RSI both blue, blue...
# some kind of colour coding for the strategy itself, so that we can
# filter it out") — the strategy-pill CSS class was flat blue for
# every strategy; this makes RSI Reversal / Volume Spike / arbitrage
# visually distinct at a glance in the paper trading tables. ──
_STRATEGY_STYLE = {
    "RSI Reversal":           ("rgba(74,144,226,0.12)",  "rgba(74,144,226,0.35)",  "var(--blue)"),
    "Volume Spike":           ("rgba(247,168,0,0.12)",   "rgba(247,168,0,0.35)",   "var(--amber)"),
    "Cash-Futures Arbitrage": ("rgba(155,109,255,0.12)", "rgba(155,109,255,0.35)","var(--purple)"),
}
_DEFAULT_STRATEGY_STYLE = ("rgba(74,144,226,0.12)", "rgba(74,144,226,0.35)", "var(--blue)")

def strategy_pill_html(strategy: str, timeframe: str = "") -> str:
    bg, border, fg = _STRATEGY_STYLE.get(strategy, _DEFAULT_STRATEGY_STYLE)
    label = f"{strategy} · {timeframe}" if timeframe else strategy
    return (
        f"<span style='display:inline-block;background:{bg};border:1px solid {border};"
        f"color:{fg};font-family:JetBrains Mono,monospace;font-size:10px;font-weight:600;"
        f"padding:3px 10px;border-radius:20px;letter-spacing:1px;'>{label}</span>"
    )


# ============================================================
# TRADINGVIEW LIGHTWEIGHT CHART
# ============================================================

def build_tv_chart(
    symbol:   str,
    name:     str,
    tf_name:  str,
    is_dark:  bool,
    signals:  list[dict] = None,
    all_tf_data: dict = None,  # pre-loaded data for all timeframes
) -> str:
    """
    Fetch OHLCV data and build TradingView Lightweight Charts HTML.
    Returns HTML string to embed via st.components.v1.html()
    """
    try:
        interval = TIMEFRAMES[tf_name]
        period   = PERIOD_MAP[tf_name]

        df = provider.fetch_data(symbol=symbol, interval=interval, period=period)

        if df is None or df.empty or len(df) < 5:
            return "<div style='padding:20px;color:#6b7fa0;font-family:monospace;'>No data available for chart</div>"

        # IST display offset for Lightweight Charts.
        # The chart library renders every epoch as if it were UTC. To make
        # candles/markers show IST wall-clock time (NSE 9:15-15:30), we add
        # +5h30m (19,800s) to every epoch. Applied identically to candles,
        # RSI and markers so they stay aligned with each other.
        IST_OFFSET = 19800

        # Prepare candle data
        candles = []
        for _, row in df.iterrows():
            try:
                ts = row["Datetime"]
                if hasattr(ts, "timestamp"):
                    t = int(ts.timestamp()) + IST_OFFSET
                else:
                    t = int(pd.Timestamp(ts).timestamp()) + IST_OFFSET
                candles.append({
                    "time":  t,
                    "open":  round(float(row["Open"]),  2),
                    "high":  round(float(row["High"]),  2),
                    "low":   round(float(row["Low"]),   2),
                    "close": round(float(row["Close"]), 2),
                })
            except Exception:
                continue

        # RSI data
        try:
            df_rsi = add_rsi(df.copy())
            df_rsi.dropna(subset=["RSI"], inplace=True)
            rsi_data = []
            for _, row in df_rsi.iterrows():
                try:
                    ts = row["Datetime"]
                    t  = (int(ts.timestamp()) if hasattr(ts, "timestamp") else int(pd.Timestamp(ts).timestamp())) + IST_OFFSET
                    rsi_data.append({"time": t, "value": round(float(row["RSI"]), 2)})
                except Exception:
                    continue
        except Exception:
            rsi_data = []

        # Pivot lines
        try:
            df_piv = add_pivot_points(df.copy())
            df_piv.dropna(subset=["PP"], inplace=True)
            if not df_piv.empty:
                last_piv = df_piv.iloc[-1]
                pivots = {
                    "PP": round(float(last_piv["PP"]), 2),
                    "R1": round(float(last_piv["R1"]), 2),
                    "R2": round(float(last_piv["R2"]), 2),
                    "S1": round(float(last_piv["S1"]), 2),
                    "S2": round(float(last_piv["S2"]), 2),
                }
            else:
                pivots = {}
        except Exception:
            pivots = {}

        # Signal markers from Supabase
        markers = []
        if signals:
            for sig in signals:
                try:
                    ts_str = sig.get("Timestamp", "")
                    if not ts_str:
                        continue
                    ts = pd.to_datetime(ts_str, utc=True)
                    t  = int(ts.timestamp()) + IST_OFFSET
                    signal_type = sig.get("Signal", "")
                    markers.append({
                        "time":     t,
                        "position": "belowBar" if signal_type == "BUY" else "aboveBar",
                        "color":    "#1ec9a0" if signal_type == "BUY" else "#f05555",
                        "shape":    "arrowUp" if signal_type == "BUY" else "arrowDown",
                        "text":     f"{signal_type} ₹{sig.get('Price','')}"
                    })
                except Exception:
                    continue

        # Theme colors
        if is_dark:
            bg      = "#0d1526"
            grid    = "#1a2840"
            text    = "#6b7fa0"
            upColor = "#1ec9a0"
            dnColor = "#f05555"
            border  = "#1a2840"
        else:
            bg      = "#ffffff"
            grid    = "#e8edf5"
            text    = "#7a8fad"
            upColor = "#0a9e74"
            dnColor = "#cc2020"
            border  = "#dde3ed"

        candles_json = json.dumps(candles)
        rsi_json     = json.dumps(rsi_data)
        markers_json = json.dumps(markers)
        pivots_json  = json.dumps(pivots)

        html = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ background:{bg}; font-family:'IBM Plex Sans',sans-serif; overflow:hidden; }}
#chart-header {{ padding:10px 16px; display:flex; align-items:center; justify-content:space-between; border-bottom:1px solid {border}; }}
#chart-title {{ font-size:14px; font-weight:700; color:{'#f1f5fb' if is_dark else '#0d1526'}; letter-spacing:0.5px; }}
#chart-tf {{ font-size:11px; color:{text}; font-family:'JetBrains Mono',monospace; }}
#price-chart {{ width:100%; height:340px; }}
#rsi-chart {{ width:100%; height:120px; border-top:1px solid {border}; }}
#legend {{ padding:6px 16px; font-size:11px; color:{text}; font-family:'JetBrains Mono',monospace; display:flex; gap:16px; border-top:1px solid {border}; }}
.legend-item {{ display:flex; align-items:center; gap:5px; }}
.legend-dot {{ width:8px; height:8px; border-radius:50%; }}
</style>
</head>
<body>
<div id="chart-header">
  <div id="chart-title">{name}</div>
  <div style="display:flex;align-items:center;gap:8px;">
    <div id="tf-buttons" style="display:flex;gap:4px;">
    </div>
    <div id="chart-tf" style="font-size:10px;color:{text};font-family:JetBrains Mono,monospace;margin-left:8px;">TradingView Lightweight Charts™</div>
  </div>
</div>
<div id="price-chart"></div>
<div id="rsi-chart"></div>
<div id="legend">
  <div class="legend-item"><div class="legend-dot" style="background:#1ec9a0"></div>BUY signal</div>
  <div class="legend-item"><div class="legend-dot" style="background:#f05555"></div>SELL signal</div>
  <div class="legend-item"><div class="legend-dot" style="background:#4a90e2"></div>Pivot PP</div>
  <div class="legend-item"><div class="legend-dot" style="background:#1ec9a0;opacity:0.6"></div>S1/S2 Support</div>
  <div class="legend-item"><div class="legend-dot" style="background:#f05555;opacity:0.6"></div>R1/R2 Resistance</div>
  <div class="legend-item"><div class="legend-dot" style="background:#9b6dff;opacity:0.6"></div>RSI 25/75 levels</div>
</div>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<script>
const candles  = {candles_json};
const rsiData  = {rsi_json};
const markers  = {markers_json};
const pivots   = {pivots_json};

// ── Price Chart ──
const priceChart = LightweightCharts.createChart(document.getElementById('price-chart'), {{
  width:  document.getElementById('price-chart').clientWidth,
  height: 340,
  layout: {{
    background: {{ type:'solid', color:'{bg}' }},
    textColor:  '{text}',
    fontSize:   11,
  }},
  grid: {{
    vertLines:  {{ color:'{grid}' }},
    horzLines:  {{ color:'{grid}' }},
  }},
  crosshair: {{ mode: LightweightCharts.CrosshairMode.Normal }},
  rightPriceScale: {{ borderColor:'{border}' }},
  timeScale: {{ borderColor:'{border}', timeVisible:true, secondsVisible:false }},
  attributionLogo: true,
}});

const candleSeries = priceChart.addCandlestickSeries({{
  upColor:         '{upColor}',
  downColor:       '{dnColor}',
  borderUpColor:   '{upColor}',
  borderDownColor: '{dnColor}',
  wickUpColor:     '{upColor}',
  wickDownColor:   '{dnColor}',
}});
candleSeries.setData(candles);
if (markers.length > 0) candleSeries.setMarkers(markers);

// ── Pivot lines ──
const pivotColors = {{ PP:'#4a90e2', R1:'rgba(240,85,85,0.7)', R2:'rgba(240,85,85,0.4)', S1:'rgba(30,201,160,0.7)', S2:'rgba(30,201,160,0.4)' }};
Object.entries(pivots).forEach(([label, price]) => {{
  if (price > 0) {{
    const line = priceChart.addLineSeries({{
      color:           pivotColors[label] || '#4a90e2',
      lineWidth:       1,
      lineStyle:       LightweightCharts.LineStyle.Dashed,
      priceLineVisible:false,
      lastValueVisible:true,
      title:           label,
    }});
    if (candles.length > 0) {{
      line.setData([
        {{ time: candles[0].time,                  value: price }},
        {{ time: candles[candles.length-1].time,   value: price }},
      ]);
    }}
  }}
}});

// ── RSI Chart ──
const rsiChart = LightweightCharts.createChart(document.getElementById('rsi-chart'), {{
  width:  document.getElementById('rsi-chart').clientWidth,
  height: 120,
  layout: {{
    background: {{ type:'solid', color:'{bg}' }},
    textColor:  '{text}',
    fontSize:   10,
  }},
  grid: {{
    vertLines: {{ color:'{grid}' }},
    horzLines: {{ color:'{grid}' }},
  }},
  rightPriceScale: {{ borderColor:'{border}', scaleMargins:{{ top:0.1, bottom:0.1 }} }},
  timeScale: {{ borderColor:'{border}', timeVisible:true, secondsVisible:false }},
  crosshair: {{ mode: LightweightCharts.CrosshairMode.Normal }},
  attributionLogo: false,
}});

const rsiSeries = rsiChart.addLineSeries({{
  color: '#9b6dff',
  lineWidth: 2,
  priceLineVisible: true,
  lastValueVisible: true,
  title: 'RSI',
  priceFormat: {{ type: 'price', precision: 1, minMove: 0.1 }},
}});
if (rsiData.length > 0) {{
  rsiSeries.setData(rsiData);
  rsiChart.timeScale().fitContent();
}}

// RSI 25/75 reference lines (Jwala's levels)
[25, 75].forEach(level => {{
  const refLine = rsiChart.addLineSeries({{
    color:           level === 70 ? 'rgba(240,85,85,0.4)' : 'rgba(30,201,160,0.4)',
    lineWidth:       1,
    lineStyle:       LightweightCharts.LineStyle.Dashed,
    priceLineVisible:false,
    lastValueVisible:false,
  }});
  if (rsiData.length > 0) {{
    refLine.setData([
      {{ time: rsiData[0].time,               value: level }},
      {{ time: rsiData[rsiData.length-1].time, value: level }},
    ]);
  }}
}});

// Sync crosshair between charts
priceChart.timeScale().subscribeVisibleLogicalRangeChange(range => {{
  rsiChart.timeScale().setVisibleLogicalRange(range);
}});
rsiChart.timeScale().subscribeVisibleLogicalRangeChange(range => {{
  priceChart.timeScale().setVisibleLogicalRange(range);
}});

// Resize handler
window.addEventListener('resize', () => {{
  priceChart.resize(document.getElementById('price-chart').clientWidth, 340);
  rsiChart.resize(document.getElementById('rsi-chart').clientWidth, 120);
}});

priceChart.timeScale().fitContent();
</script>
</body>
</html>
"""
        return html

    except Exception as e:
        return f"<div style='padding:20px;color:#f05555;font-family:monospace;'>Chart error: {e}</div>"


# ============================================================
# SIDEBAR
# ============================================================

with st.sidebar:
    st.markdown(f"""
    <div style='padding:4px 0 20px;'>
        <div style='font-family:JetBrains Mono,monospace;font-size:16px;font-weight:700;color:var(--blue);letter-spacing:2px;'>ALGO SIGNALS</div>
        <div style='font-size:11px;color:var(--t3);letter-spacing:2px;text-transform:uppercase;margin-top:4px;'>NSE · BSE · MCX</div>
    </div>
    <div style='border-top:1px solid var(--border);margin-bottom:18px;'></div>
    """, unsafe_allow_html=True)

    st.markdown(
        f"<div style='font-size:12px;color:var(--t2);margin-bottom:8px;'>"
        f"Logged in as <b>{st.session_state.get('name', '')}</b></div>",
        unsafe_allow_html=True,
    )
    authenticator.logout("Logout", "sidebar")
    st.markdown("<div style='margin:10px 0;border-top:1px solid var(--border);'></div>", unsafe_allow_html=True)

    btn_label = "☀️ Light Mode" if st.session_state.dark_mode else "🌙 Dark Mode"
    if st.button(btn_label, use_container_width=True):
        st.session_state.dark_mode = not st.session_state.dark_mode
        st.rerun()

    st.markdown("<div style='margin:14px 0;border-top:1px solid var(--border);'></div>", unsafe_allow_html=True)

    # ── Strategy selector ──
    st.markdown('<div style="font-size:11px;font-weight:700;color:var(--t3);text-transform:uppercase;letter-spacing:2px;margin-bottom:8px;">Strategy</div>', unsafe_allow_html=True)
    selected_strategy = st.selectbox(
        "Strategy",
        ALL_STRATEGY_NAMES,
        index=ALL_STRATEGY_NAMES.index(st.session_state.selected_strategy)
              if st.session_state.selected_strategy in ALL_STRATEGY_NAMES else 0,
        label_visibility="collapsed",
        key="strategy_selectbox",
    )
    if selected_strategy != st.session_state.selected_strategy:
        st.session_state.selected_strategy = selected_strategy
        # Write to Supabase so scheduler picks it up immediately
        try:
            from core.database.db import set_config
            if selected_strategy != "All Strategies":
                set_config("SIGNAL_STRATEGY", selected_strategy)
        except Exception:
            pass

    st.markdown("<div style='margin:14px 0;border-top:1px solid var(--border);'></div>", unsafe_allow_html=True)

    # ── Timeframe selector — FIXED: no double-click ──
    st.markdown('<div style="font-size:11px;font-weight:700;color:var(--t3);text-transform:uppercase;letter-spacing:2px;margin-bottom:8px;">Timeframe</div>', unsafe_allow_html=True)
    _tf_keys = list(TIMEFRAMES.keys())
    selected_tf = st.selectbox(
        "Timeframe",
        _tf_keys,
        index=_tf_keys.index(st.session_state.selected_tf)
              if st.session_state.selected_tf in _tf_keys else 2,
        label_visibility="collapsed",
        key="tf_selectbox",
    )
    # FIXED: only update if changed — prevents double rerun
    if selected_tf != st.session_state.selected_tf:
        st.session_state.selected_tf = selected_tf

    st.markdown("<div style='margin:14px 0;border-top:1px solid var(--border);'></div>", unsafe_allow_html=True)

    # ── Search & Filters ──
    st.markdown('<div style="font-size:11px;font-weight:700;color:var(--t3);text-transform:uppercase;letter-spacing:2px;margin-bottom:8px;">Search</div>', unsafe_allow_html=True)
    search_query = st.text_input(
        "Search",
        placeholder="Type stock name...",
        label_visibility="collapsed",
        key="search_input",
    ).strip().upper()

    st.markdown('<div style="font-size:11px;font-weight:700;color:var(--t3);text-transform:uppercase;letter-spacing:2px;margin-bottom:8px;margin-top:12px;">Signal Filter</div>', unsafe_allow_html=True)
    signal_filter = st.selectbox(
        "Signal",
        ["All", "BUY only", "SELL only", "BUY + SELL"],
        label_visibility="collapsed",
        key="signal_filter",
    )

    st.markdown('<div style="font-size:11px;font-weight:700;color:var(--t3);text-transform:uppercase;letter-spacing:2px;margin-bottom:8px;margin-top:12px;">Strength Filter</div>', unsafe_allow_html=True)
    strength_filter = st.selectbox(
        "Strength",
        ["All", "STRONG only", "MODERATE+"],
        label_visibility="collapsed",
        key="strength_filter",
    )

    st.markdown("<div style='margin:14px 0;border-top:1px solid var(--border);'></div>", unsafe_allow_html=True)

    # ── Markets ──
    st.markdown('<div style="font-size:11px;font-weight:700;color:var(--t3);text-transform:uppercase;letter-spacing:2px;margin-bottom:8px;">Markets</div>', unsafe_allow_html=True)
    show_idx = st.checkbox("Indexes",     value=True)
    show_stk = st.checkbox("Stocks",      value=True)
    show_com = st.checkbox("Commodities", value=True)

    st.markdown("<div style='margin:14px 0;border-top:1px solid var(--border);'></div>", unsafe_allow_html=True)

    # ── Telegram status ──
    tg = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if tg:
        st.markdown('<div class="tg-ok">✓ TELEGRAM CONNECTED</div>', unsafe_allow_html=True)
    else:
        st.markdown('<div class="tg-err">✗ TELEGRAM NOT SET</div>', unsafe_allow_html=True)

    st.markdown("<div style='margin:14px 0;border-top:1px solid var(--border);'></div>", unsafe_allow_html=True)

    # ── Scheduler status ──
    last_scan    = get_last_scan_time()
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
        <div>Strategy &nbsp;<span style='color:var(--blue);font-family:JetBrains Mono,monospace;font-weight:600;'>{selected_strategy}</span></div>
        <div>Viewing &nbsp;&nbsp;<span style='color:var(--t2);font-family:JetBrains Mono,monospace;font-weight:600;'>{selected_tf}</span></div>
        <div>Period &nbsp;&nbsp;&nbsp;<span style='color:var(--t2);font-family:JetBrains Mono,monospace;font-weight:600;'>{fetch_period}</span></div>
        <div>Refresh &nbsp;&nbsp;<span style='color:var(--t2);font-family:JetBrains Mono,monospace;font-weight:600;'>5 min</span></div>
        <div style='margin-top:4px;'>{sched_html}</div>
    </div>
    """, unsafe_allow_html=True)


# ============================================================
# HEADER
# ============================================================

ist_now = datetime.now(IST)
is_open = market_open()

hl, hr = st.columns([5, 1])
with hl:
    st.markdown(f"""
    <div style='padding:8px 0 4px;'>
        <h1 style='font-family:IBM Plex Sans,sans-serif;font-size:28px;font-weight:700;color:var(--t1);letter-spacing:-0.5px;margin:0;'>
            Signal Dashboard
        </h1>
        <div style='font-size:12px;color:var(--t3);margin-top:6px;font-family:JetBrains Mono,monospace;display:flex;align-items:center;gap:12px;'>
            <span>{ist_now.strftime('%d %b %Y  %H:%M:%S IST')}</span>
            <span>·</span>
            <span class='strategy-pill'>{selected_strategy}</span>
            <span>·</span>
            <span>{selected_tf}</span>
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

all_logs       = logger.get_logs(strategy=None if selected_strategy == "All Strategies" else selected_strategy)
latest_signals = get_latest_signals(selected_tf, selected_strategy)
backtest_data  = get_results(selected_tf)
total_buy = total_sell = total_hold = 0

# Load instrument universe
try:
    fno_stocks = get_fno_universe()
except Exception:
    fno_stocks = FALLBACK_FNO_SYMBOLS

fno_display = {s: stock_display(s) for s in fno_stocks}
fno_tv      = {s: _stock_tv(s) for s in fno_stocks}


def build_rows(symbols, display_map, tv_map):
    global total_buy, total_sell, total_hold
    rows = []
    for sym in symbols:
        name = display_map.get(sym, sym.replace(".NS", ""))
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
                ts = str(d["Timestamp"])[:16]
        else:
            sig = "HOLD"; rsi = "—"; price = "—"; ts = "—"

        bt = {}
        if not backtest_data.empty and sym in backtest_data["Symbol"].values:
            br     = backtest_data[backtest_data["Symbol"] == sym].iloc[0]
            trades = int(br.get("Trades", 0))
            if trades > 0:
                bt = {
                    "trades":   trades,
                    "pnl":      br.get("PnL", 0.0),
                    "win_rate": br.get("Win Rate %", 0.0),
                }

        if sig == "BUY":    total_buy  += 1
        elif sig == "SELL": total_sell += 1
        else:               total_hold += 1

        rows.append({
            "sym": sym, "name": name,
            "tv": tv_url(tv, selected_tf),
            "tv_sym": tv,
            "signal": sig, "sig_rsi": rsi, "sig_price": price,
            "ts": ts, "bt": bt,
        })
    return rows


idx_rows = build_rows(INDEXES, INDEXES_DISPLAY, INDEXES_TV) if show_idx else []
stk_rows = build_rows(fno_stocks, fno_display, fno_tv)      if show_stk else []
com_rows = build_rows(COMMODITIES, COMMODITIES_DISPLAY, COMMODITIES_TV) if show_com else []

# Apply search filter
if search_query:
    idx_rows = [r for r in idx_rows if search_query in r["name"].upper() or search_query in r["sym"].upper()]
    stk_rows = [r for r in stk_rows if search_query in r["name"].upper() or search_query in r["sym"].upper()]
    com_rows = [r for r in com_rows if search_query in r["name"].upper() or search_query in r["sym"].upper()]

# Apply signal filter
def _sig_match(row, flt):
    s = row.get("signal", "HOLD")
    if flt == "BUY only":   return s == "BUY"
    if flt == "SELL only":  return s == "SELL"
    if flt == "BUY + SELL": return s in ("BUY", "SELL")
    return True  # All

def _str_match(row, flt):
    s = row.get("strength", "")
    if flt == "STRONG only":  return s == "STRONG"
    if flt == "MODERATE+":    return s in ("STRONG", "MODERATE")
    return True  # All

if signal_filter != "All":
    idx_rows = [r for r in idx_rows if _sig_match(r, signal_filter)]
    stk_rows = [r for r in stk_rows if _sig_match(r, signal_filter)]
    com_rows = [r for r in com_rows if _sig_match(r, signal_filter)]

if strength_filter != "All":
    idx_rows = [r for r in idx_rows if _str_match(r, strength_filter)]
    stk_rows = [r for r in stk_rows if _str_match(r, strength_filter)]
    com_rows = [r for r in com_rows if _str_match(r, strength_filter)]


# ============================================================
# KPI BAR
# ============================================================

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Total Scanned", total_buy + total_sell + total_hold)
k2.metric("BUY Signals",   total_buy,  delta=f"+{total_buy}"  if total_buy  > 0 else None)
k3.metric("SELL Signals",  total_sell, delta=f"-{total_sell}" if total_sell > 0 else None, delta_color="inverse")
k4.metric("HOLD",          total_hold)
k5.metric("Timeframe",     selected_tf)
st.markdown("<div style='margin:28px 0 10px;'></div>", unsafe_allow_html=True)


# ============================================================
# CHART PANEL — shown when a stock is selected
# ============================================================

def show_chart_panel():
    sym  = st.session_state.chart_symbol
    name = st.session_state.chart_name
    if not sym:
        return

    st.markdown(f"""
    <div style='background:var(--card);border:1px solid var(--border2);border-radius:10px;padding:0;margin:0 0 24px;overflow:hidden;'>
    """, unsafe_allow_html=True)

    # Get signals for this symbol to mark on chart
    sym_signals = []
    if not all_logs.empty and "Stock" in all_logs.columns:
        # Show ALL historical signals for this stock (all timeframes)
        sym_df = all_logs[all_logs["Stock"] == sym].copy()
        if not sym_df.empty:
            sym_signals = sym_df.to_dict("records")

    chart_html = build_tv_chart(
        symbol=sym,
        name=name,
        tf_name=selected_tf,
        is_dark=st.session_state.dark_mode,
        signals=sym_signals,
    )

    # Close button
    close_col, _ = st.columns([1, 5])
    with close_col:
        if st.button("✕ Close Chart", key="close_chart"):
            st.session_state.chart_symbol = None
            st.session_state.chart_name   = None
            st.rerun()

    st.components.v1.html(chart_html, height=510, scrolling=False)
    st.markdown("</div>", unsafe_allow_html=True)


show_chart_panel()


# ============================================================
# BACKTEST SUMMARY BAR
# ============================================================

def backtest_summary_bar(rows, period):
    bt_rows = [r["bt"] for r in rows if r.get("bt") and r["bt"].get("trades", 0) > 0]
    if not bt_rows:
        st.markdown(f'<div class="bt-pending">Backtest ({period}) — awaiting first scan for this timeframe + strategy</div>', unsafe_allow_html=True)
        return

    total_trades = sum(b.get("trades", 0) for b in bt_rows)
    total_pnl    = sum(b.get("pnl",    0) for b in bt_rows)
    avg_wr       = round(sum(b.get("win_rate", 0) for b in bt_rows) / len(bt_rows), 1)
    pnl_c = pnl_class(total_pnl)
    wr_c  = "pos" if avg_wr >= 50 else "neg"

    st.markdown(f"""
    <div class="bt-card">
        <div><div style='font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:1.5px;'>Backtest</div><div style='font-size:11px;color:var(--t3);font-family:JetBrains Mono,monospace;'>({period})</div></div>
        <div class="bt-item"><div class="bt-label">Trades</div><div class="bt-val">{total_trades}</div></div>
        <div class="bt-item"><div class="bt-label">Total PnL</div><div class="bt-val {pnl_c}">{fmt_pnl(total_pnl) or '—'}</div></div>
        <div class="bt-item"><div class="bt-label">Avg Win Rate</div><div class="bt-val {wr_c}">{avg_wr}%</div></div>
    </div>
    """, unsafe_allow_html=True)


# ============================================================
# RENDER SECTION
# ============================================================

def render_section(rows, title, dot_color="#4a90e2", scroll_height=None):
    """
    scroll_height: if given, the header row + all instrument rows render
    inside a native Streamlit scrollable container of that pixel height
    (same "scroll, don't paginate" treatment as Signal History). Used
    for the F&O watchlist (167 instruments) — Jwala Jul 8.
    """
    if not rows: return

    action    = [r for r in rows if r["signal"] in ("BUY", "SELL")]
    holds     = len([r for r in rows if r["signal"] == "HOLD"])
    act_color = "var(--green)" if action else "var(--t3)"

    st.markdown(f"""
    <div class="sec-hdr">
        <div style='width:7px;height:7px;border-radius:50%;background:{dot_color};flex-shrink:0;'></div>
        <span class="sec-title">{title}</span>
        <span class="sec-meta">{len(rows)} instruments &nbsp;·&nbsp; <span style='color:{act_color};font-weight:700;'>{len(action)} active</span> &nbsp;·&nbsp; {holds} hold</span>
    </div>
    """, unsafe_allow_html=True)

    backtest_summary_bar(rows, fetch_period)

    if not action:
        st.markdown('<div class="no-sig">No active signals — all instruments HOLD</div>', unsafe_allow_html=True)
        return

    action = sorted(action, key=lambda r: str(r.get("ts", "—")), reverse=True)

    _scroll_ctx = st.container(height=scroll_height) if scroll_height else contextlib.nullcontext()
    with _scroll_ctx:
        _render_action_rows(action)


def _render_action_rows(action):
    # Column headers
    h = st.columns([2.2, 0.7, 0.9, 0.8, 1.2, 1.0, 0.9, 1.2, 0.7])
    for col, lbl in zip(h, ["Instrument", "Signal", "Strength", "RSI",
                             "Price → Now", "PnL", "Win%", "Signal Time", "📈"]):
        col.markdown(f'<div class="col-hdr">{lbl}</div>', unsafe_allow_html=True)

    for row in action:
        c = st.columns([2.2, 0.7, 0.9, 0.8, 1.2, 1.0, 0.9, 1.2, 0.7])

        # Live price fetch
        cur_price = row["sig_price"]
        cur_live  = False
        try:
            _df = provider.fetch_data(
                symbol=row["sym"],
                interval=TIMEFRAMES[selected_tf],
                period=PERIOD_MAP[selected_tf],
            )
            if _df is not None and not _df.empty:
                cur_price = round(float(_df["Close"].iloc[-1]), 2)
                cur_live  = True
        except Exception:
            pass

        # Instrument
        with c[0]:
            is_selected = st.session_state.chart_symbol == row["sym"]
            highlight   = "border-left:2px solid var(--blue);padding-left:8px;" if is_selected else ""
            st.markdown(f"""
            <div style='padding:10px 0 8px;{highlight}'>
                <div class="stock-name">{row['name']}</div>
                <div class="stock-sym">{row['sym']}</div>
            </div>""", unsafe_allow_html=True)

        # Signal badge
        with c[1]:
            badge = '<span class="badge-buy">BUY</span>' if row["signal"] == "BUY" else '<span class="badge-sell">SELL</span>'
            st.markdown(f"<div style='padding:12px 0;'>{badge}</div>", unsafe_allow_html=True)

        # Strength
        with c[2]:
            _str = row.get("strength", "")
            if _str == "STRONG":
                _sc = "badge-strong"
            elif _str == "MODERATE":
                _sc = "badge-moderate"
            else:
                _sc = "badge-pending"
            _sl = _str[:3] if _str else "–"
            st.markdown(f"<div style='padding:12px 0;'><span class='{_sc}'>{_sl}</span></div>", unsafe_allow_html=True)

        # Sig RSI
        with c[3]:
            _sv = fmt_rsi(row["sig_rsi"])
            _ss = rsi_style(row["sig_rsi"])
            st.markdown(f"<div style='padding:12px 0;font-family:JetBrains Mono,monospace;font-size:13px;{_ss}'>{_sv}</div>", unsafe_allow_html=True)

        # Price → Now (merged column)
        with c[4]:
            try:
                diff  = float(cur_price) - float(row["sig_price"])
                cp_c  = "var(--green)" if diff >= 0 else "var(--red)"
                arrow = "▲" if diff >= 0 else "▼"
                sig_p = fmt_price(row["sig_price"])
                cur_p = fmt_price(cur_price)
                diff_str = f"{arrow}{'+' if diff>=0 else ''}{diff:,.1f}" if cur_live else ""
                st.markdown(f"<div style='padding:8px 0;font-family:JetBrains Mono,monospace;'>"
                            f"<div style='font-size:11px;color:var(--t3);'>{sig_p}</div>"
                            f"<div style='font-size:12px;color:{cp_c if cur_live else 'var(--t2)'};font-weight:600;'>{cur_p} <span style='font-size:10px;'>{diff_str}</span></div>"
                            f"</div>", unsafe_allow_html=True)
            except:
                st.markdown(f"<div style='padding:12px 0;font-family:JetBrains Mono,monospace;font-size:12px;color:var(--t2);'>{fmt_price(row['sig_price'])}</div>", unsafe_allow_html=True)

        # PnL
        with c[5]:
            pnl_v = row["bt"].get("pnl") if row.get("bt") else None
            content = f"<span style='color:{('var(--green)' if pnl_class(pnl_v)=='pos' else 'var(--red)') if pnl_v is not None else 'var(--t3)'};'>{fmt_pnl(pnl_v) or '—'}</span>" if pnl_v is not None else '<span class="badge-pending">pending</span>'
            st.markdown(f"<div style='padding:12px 0;font-family:JetBrains Mono,monospace;font-size:12px;'>{content}</div>", unsafe_allow_html=True)

        # Win Rate
        with c[6]:
            wr_v = row["bt"].get("win_rate") if row.get("bt") else None
            if wr_v is not None:
                wr_c = "var(--green)" if float(wr_v) >= 50 else "var(--red)"
                _wrc = f"<span style='color:{wr_c};'>{float(wr_v):.1f}%</span>"
            else:
                _wrc = '<span class="badge-pending">–</span>'
            st.markdown(f"<div style='padding:12px 0;font-family:JetBrains Mono,monospace;font-size:12px;'>{_wrc}</div>", unsafe_allow_html=True)

        # Signal Time
        with c[7]:
            st.markdown(f"<div style='padding:12px 0;font-family:JetBrains Mono,monospace;font-size:10px;color:var(--t3);'>{row['ts']}</div>", unsafe_allow_html=True)

        # Chart button — opens inline chart
        with c[8]:
            if st.button("📈", key=f"chart_{row['sym']}", help=f"View chart for {row['name']}"):
                if st.session_state.chart_symbol == row["sym"]:
                    st.session_state.chart_symbol = None
                    st.session_state.chart_name   = None
                else:
                    st.session_state.chart_symbol = row["sym"]
                    st.session_state.chart_name   = row["name"]
                st.rerun()

        st.markdown("<div class='row-div'></div>", unsafe_allow_html=True)

    st.markdown("<div style='margin-bottom:28px;'></div>", unsafe_allow_html=True)


# ============================================================
# PAPER TRADING DASHBOARD SECTION
#
# Moved above the raw signal feed per Jwala (Jul 8 call): "can we
# keep this paper trading part on the top page instead of the
# signals part... so when I open the dashboard I'd have an idea how
# paper trading is going." Also adds: capital visibility, symmetric
# LONG/SHORT display, manual Close + Edit-Stop buttons per position,
# and Opened/Closed/Duration on closed trades — all per the Jul 8 call.
# ============================================================

def _fmt_duration(opened_at, closed_at) -> str:
    """Human duration between two timestamps, e.g. '1h 12m' or '43m'."""
    try:
        o = pd.to_datetime(opened_at, utc=True)
        c = pd.to_datetime(closed_at, utc=True)
        secs = int((c - o).total_seconds())
        if secs < 0:
            return "—"
        h, rem = divmod(secs, 3600)
        m = rem // 60
        return f"{h}h {m}m" if h else f"{m}m"
    except Exception:
        return "—"


def render_paper_trading():
    from core.database.db import (
        get_open_paper_positions,
        get_closed_paper_positions,
        get_paper_pnl_summary,
        get_today_closed_paper_positions,
        get_today_pnl_summary,
        get_capital_deployed,
        close_paper_position,
        update_paper_position_stop,
    )
    from core.execution.rms import RMSConfig

    st.markdown("""
    <div class="sec-hdr" style='margin-top:6px;'>
        <div style='width:7px;height:7px;border-radius:50%;background:var(--purple);flex-shrink:0;'></div>
        <span class="sec-title">Paper Trading — Simulated Portfolio (Today)</span>
        <span class="sec-meta">Upstox Sandbox &nbsp;·&nbsp; RSI Reversal + Volume Spike &nbsp;·&nbsp; Long + Short</span>
    </div>
    """, unsafe_allow_html=True)

    # Today only, not a rolling 30-day sum (Jwala Jul 11 fix — see
    # get_today_pnl_summary's docstring for the exact bug this closes).
    summary  = get_today_pnl_summary()
    open_df  = get_open_paper_positions()

    # ── Compute UNREALIZED P&L on open positions (needs live CMP) ──
    # CMP is fetched once per open symbol here in the dashboard (the DB
    # layer has no price feed). Direction-aware: LONG profits when
    # cmp > entry, SHORT profits when cmp < entry.
    cmp_map        = {}
    total_unreal   = 0.0
    open_in_profit = 0
    if open_df is not None and not open_df.empty:
        for sym in open_df["symbol"].unique():
            try:
                _df = provider.fetch_data(
                    symbol=sym,
                    interval=TIMEFRAMES[selected_tf],
                    period=PERIOD_MAP[selected_tf],
                )
                if _df is not None and not _df.empty:
                    cmp_map[sym] = round(float(_df["Close"].iloc[-1]), 2)
            except Exception:
                cmp_map[sym] = None

        for _, r in open_df.iterrows():
            cmp = cmp_map.get(r["symbol"])
            if cmp is None:
                continue
            qty   = int(r["quantity"]); entry = float(r["entry_price"])
            u = (cmp - entry) * qty if r["side"] == "BUY" else (entry - cmp) * qty
            total_unreal += u
            if u >= 0:
                open_in_profit += 1

    # ── Gross vs Net P&L (Jwala Jul 11: "we'll not call this net,
    # we'll call this gross profit and loss. Net would be after
    # minusing the brokerage and taxes.") total_pnl (gross) is
    # unchanged in meaning from before; total_net_pnl is new. The
    # combined portfolio total below now uses NET realized (a more
    # honest "true" total than the old gross-based one it replaces —
    # renamed from "Net P&L" to "Total P&L" to free that name up for
    # its new, more specific meaning below).
    total_gross  = summary["total_pnl"]
    total_net    = summary.get("total_net_pnl", total_gross)
    total_charges= summary.get("total_charges", 0.0)
    total_pnl_combined = total_net + total_unreal

    # ── Scorecard: Unrealized, Realized (Gross + Net), Total, Win Rate ──
    p1, p2, p3, p4, p5, p6, p7 = st.columns(7)
    p1.metric("Open Positions", summary["open_count"])
    p2.metric("Open in Profit", f"{open_in_profit} / {summary['open_count']}")
    p3.metric("Unrealized P&L", f"{'+' if total_unreal>=0 else '-'}₹{abs(total_unreal):,.0f}",
              delta_color="normal" if total_unreal >= 0 else "inverse")
    p4.metric("Realized P&L (Gross)", f"{'+' if total_gross>=0 else '-'}₹{abs(total_gross):,.0f}",
              delta_color="normal" if total_gross >= 0 else "inverse")
    p5.metric("Realized P&L (Net)", f"{'+' if total_net>=0 else '-'}₹{abs(total_net):,.0f}",
              delta=f"-₹{total_charges:,.0f} charges", delta_color="off")
    p6.metric("Total P&L (Net)", f"{'+' if total_pnl_combined>=0 else '-'}₹{abs(total_pnl_combined):,.0f}",
              delta_color="normal" if total_pnl_combined >= 0 else "inverse")
    p7.metric("Win Rate",       f"{summary['win_rate']}%",
              delta=f"{summary['wins']}W / {summary['losses']}L" if summary["trades"] else None)

    # ── Capital scorecard (Jwala Jul 8: "how much capital... how much
    # has been consumed in the trades... a column for each trade") ──
    total_capital = RMSConfig.CAPITAL
    deployed      = get_capital_deployed()
    available     = total_capital - deployed
    deployed_pct  = (deployed / total_capital * 100) if total_capital else 0.0

    st.markdown("<div style='margin:14px 0 4px;'></div>", unsafe_allow_html=True)
    cc1, cc2, cc3 = st.columns(3)
    cc1.metric("Total Capital",     f"₹{total_capital:,.0f}")
    cc2.metric("Capital Deployed",  f"₹{deployed:,.0f}", delta=f"{deployed_pct:.1f}% of total")
    cc3.metric("Capital Available", f"₹{available:,.0f}")

    st.markdown("<div style='margin:20px 0 8px;'></div>", unsafe_allow_html=True)

    # ── OPEN POSITIONS ──
    # Colours (per Jwala): STOP = purple, TARGET = amber, green/red
    # reserved strictly for P&L. Rendered as real Streamlit rows (not
    # a raw HTML table) so each row can carry a live "Close" button,
    # an "Edit Stop" popover (Jwala Jul 8 — manual profit-booking and
    # manual stop-shift, e.g. to breakeven), and a chart button.
    oph_col, kill_col = st.columns([5, 1.3])
    with oph_col:
        st.markdown('<div style="font-size:12px;font-weight:700;color:var(--t2);text-transform:uppercase;letter-spacing:2px;margin:10px 0 8px;">Open Positions — Unrealized P&L</div>', unsafe_allow_html=True)
    with kill_col:
        # ── Kill Switch (Jwala Jul 11, reconfirmed 16:21/36:07):
        # close EVERY open position immediately, not one at a time.
        # Behind a popover confirm — this is destructive and portfolio-wide.
        with st.popover("🔴 Kill Switch", use_container_width=True):
            n_open = 0 if open_df is None else len(open_df)
            st.markdown(f"**Close all {n_open} open position(s) now?**")
            st.caption("Each closes at its current market price (or entry price if a live quote isn't available). This can't be undone.")
            if n_open > 0 and st.button("Yes, close everything", key="pt_kill_switch_confirm", use_container_width=True):
                _closed, _failed = 0, []
                for _, _r in open_df.iterrows():
                    _pid = int(_r["id"])
                    _sym = _r["symbol"]
                    _px  = cmp_map.get(_sym) or float(_r["entry_price"])
                    if close_paper_position(_pid, _px, exit_reason="kill_switch"):
                        _closed += 1
                    else:
                        _failed.append(stock_display(_sym))
                if _failed:
                    st.error(f"Closed {_closed}, failed: {', '.join(_failed)}")
                else:
                    st.success(f"Closed {_closed} position(s).")
                st.rerun()

    if open_df is None or open_df.empty:
        st.markdown('<div class="no-sig">No open positions</div>', unsafe_allow_html=True)
    else:
        # Actions widened (0.8 → 1.5) for the Chart button (Jwala Jul 11:
        # "can I have the link here itself of the chart? Like previous.")
        # alongside the existing Close / Edit-Stop; Strategy trimmed
        # slightly (1.35 → 1.05) to make room.
        widths = [1.5, 0.55, 0.55, 0.85, 1.0, 1.0, 0.85, 0.85, 0.95, 1.05, 0.8, 1.5]
        h = st.columns(widths)
        for col, lbl in zip(h, ["Stock", "Side", "Qty", "Entry", "CMP", "Unreal. P&L",
                                 "Stop", "Target", "Capital", "Strategy", "Opened", "Actions"]):
            col.markdown(f'<div class="col-hdr">{lbl}</div>', unsafe_allow_html=True)

        for _, r in open_df.iterrows():
            pid    = int(r["id"])
            sym    = r["symbol"]
            side   = r["side"]
            qty    = int(r["quantity"])
            entry  = float(r["entry_price"])
            stop   = float(r["stop_loss"])
            target = float(r["target"])
            side_c = "var(--green)" if side == "BUY" else "var(--red)"
            side_lbl = "LONG" if side == "BUY" else "SHORT"
            cmp    = cmp_map.get(sym)
            capital_used = entry * qty

            c = st.columns(widths)

            with c[0]:
                st.markdown(f"<div style='padding:9px 0;font-size:13px;color:var(--t1);font-weight:600;'>{stock_display(sym)}</div>", unsafe_allow_html=True)
            with c[1]:
                st.markdown(f"<div style='padding:9px 0;'><span style='color:{side_c};font-weight:700;font-family:JetBrains Mono,monospace;font-size:11px;'>{side_lbl}</span></div>", unsafe_allow_html=True)
            with c[2]:
                st.markdown(f"<div style='padding:9px 0;font-family:JetBrains Mono,monospace;font-size:12px;color:var(--t2);'>{qty}</div>", unsafe_allow_html=True)
            with c[3]:
                st.markdown(f"<div style='padding:9px 0;font-family:JetBrains Mono,monospace;font-size:12px;color:var(--t2);'>₹{entry:,.2f}</div>", unsafe_allow_html=True)

            with c[4]:
                if cmp is not None:
                    u_arrow = "▲" if cmp >= entry else "▼"
                    u_c     = "var(--green)" if cmp >= entry else "var(--red)"
                    st.markdown(f"<div style='padding:9px 0;font-family:JetBrains Mono,monospace;font-size:12px;'>"
                                f"<span style='color:var(--t1);font-weight:600;'>₹{cmp:,.2f}</span> "
                                f"<span style='font-size:10px;color:{u_c};'>{u_arrow}{abs(cmp-entry):,.2f}</span></div>", unsafe_allow_html=True)
                else:
                    st.markdown("<div style='padding:9px 0;'><span class='badge-pending'>fetching…</span></div>", unsafe_allow_html=True)

            with c[5]:
                if cmp is not None:
                    u   = (cmp - entry) * qty if side == "BUY" else (entry - cmp) * qty
                    u_c = "var(--green)" if u >= 0 else "var(--red)"
                    st.markdown(f"<div style='padding:9px 0;font-family:JetBrains Mono,monospace;font-size:12px;'>"
                                f"<span style='color:{u_c};font-weight:700;'>{'+' if u>=0 else '-'}₹{abs(u):,.0f}</span></div>", unsafe_allow_html=True)
                else:
                    st.markdown("<div style='padding:9px 0;'><span class='badge-pending'>–</span></div>", unsafe_allow_html=True)

            with c[6]:
                st.markdown(f"<div style='padding:9px 0;font-family:JetBrains Mono,monospace;font-size:12px;color:var(--purple);'>₹{stop:,.2f}</div>", unsafe_allow_html=True)
            with c[7]:
                st.markdown(f"<div style='padding:9px 0;font-family:JetBrains Mono,monospace;font-size:12px;color:var(--amber);'>₹{target:,.2f}</div>", unsafe_allow_html=True)
            with c[8]:
                st.markdown(f"<div style='padding:9px 0;font-family:JetBrains Mono,monospace;font-size:12px;color:var(--t2);'>₹{capital_used:,.0f}</div>", unsafe_allow_html=True)
            with c[9]:
                st.markdown(f"<div style='padding:9px 0;'>{strategy_pill_html(r['strategy'], r['timeframe'])}</div>", unsafe_allow_html=True)

            with c[10]:
                try:
                    opened = pd.to_datetime(r["opened_at"], utc=True).tz_convert(IST).strftime("%d-%b %H:%M")
                except Exception:
                    opened = str(r.get("opened_at", ""))[:16]
                st.markdown(f"<div style='padding:9px 0;font-size:11px;color:var(--t3);font-family:JetBrains Mono,monospace;'>{opened}</div>", unsafe_allow_html=True)

            with c[11]:
                chcol, bcol, scol = st.columns(3)
                with chcol:
                    # Same inline chart panel the signal feed uses — "like
                    # previous" (Jwala Jul 11: "can I have the link here
                    # itself of the chart? Like previous.")
                    if st.button("📈", key=f"pt_chart_{pid}", help=f"View chart for {stock_display(sym)}"):
                        if st.session_state.chart_symbol == sym:
                            st.session_state.chart_symbol = None
                            st.session_state.chart_name   = None
                        else:
                            st.session_state.chart_symbol = sym
                            st.session_state.chart_name   = stock_display(sym)
                        st.rerun()
                with bcol:
                    if st.button("Close", key=f"pt_close_{pid}", help=f"Book P&L now for {stock_display(sym)}"):
                        exit_px = cmp if cmp is not None else entry
                        if close_paper_position(pid, exit_px, exit_reason="manual"):
                            st.rerun()
                        else:
                            st.error("Close failed")
                with scol:
                    with st.popover("Stop"):
                        new_stop = st.number_input(
                            "New stop", value=stop, step=0.05, format="%.2f",
                            key=f"pt_stop_input_{pid}",
                        )
                        if st.button("Update", key=f"pt_stop_btn_{pid}"):
                            if update_paper_position_stop(pid, new_stop):
                                st.rerun()
                            else:
                                st.error("Update failed")

            st.markdown("<div class='row-div'></div>", unsafe_allow_html=True)

    st.markdown("<div style='margin-bottom:10px;'></div>", unsafe_allow_html=True)

    # ── CLOSED TRADES — Realized P&L (paginated, 15 per page) ──
    # Rebuilt as per-row Streamlit widgets (was a static HTML table) so
    # a chart button can sit on each row — Jwala Jul 11: "can I have
    # the link here itself of the chart? ... include the chart option
    # in the closed trades also." Entry price+time and Exit price+time
    # are merged into one cell each (Jwala Jul 9/11), and Qty is back
    # (was missing — Om caught this himself on the Jul 11 call too).
    st.markdown('<div style="font-size:12px;font-weight:700;color:var(--t2);text-transform:uppercase;letter-spacing:2px;margin:22px 0 8px;">Closed Trades — Realized P&L (Today)</div>', unsafe_allow_html=True)
    closed_df = get_today_closed_paper_positions()  # today only — see summary fetch above
    if closed_df is None or closed_df.empty:
        st.markdown('<div class="no-sig">No closed trades yet</div>', unsafe_allow_html=True)
    else:
        PAGE = 15
        total = len(closed_df)
        pages = max(1, (total + PAGE - 1) // PAGE)
        if "pt_closed_page" not in st.session_state:
            st.session_state.pt_closed_page = 0
        st.session_state.pt_closed_page = max(0, min(st.session_state.pt_closed_page, pages - 1))
        pg = st.session_state.pt_closed_page

        page_df = closed_df.iloc[pg*PAGE : (pg+1)*PAGE]

        # Net P&L added alongside Gross (Jwala Jul 11: "we'll call
        # this gross profit and loss. Net would be after minusing the
        # brokerage and taxes."). Strategy trimmed slightly to fit.
        ct_widths = [1.1, 0.5, 0.5, 0.95, 0.95, 0.8, 0.8, 0.75, 1.0, 0.65, 0.5]
        ct_h = st.columns(ct_widths)
        for col, lbl in zip(ct_h, ["Stock", "Side", "Qty", "Entry", "Exit", "Gross P&L", "Net P&L",
                                     "Exit Reason", "Strategy", "Duration", "Chart"]):
            col.markdown(f'<div class="col-hdr">{lbl}</div>', unsafe_allow_html=True)

        for _, r in page_df.iterrows():
            ct_sym   = r["symbol"]
            side     = r["side"]
            side_c   = "var(--green)" if side == "BUY" else "var(--red)"
            side_lbl = "LONG" if side == "BUY" else "SHORT"
            qty      = int(r["quantity"])
            pnl      = float(r["pnl"])
            pnl_c    = "var(--green)" if pnl >= 0 else "var(--red)"
            net_pnl_val = r.get("net_pnl")
            net_pnl_val = float(net_pnl_val) if net_pnl_val is not None and pd.notna(net_pnl_val) else None
            reason   = str(r.get("exit_reason", "")).upper()
            rc = {"STOP": "var(--red)", "TARGET": "var(--green)",
                  "REVERSAL": "var(--amber)", "MANUAL": "var(--blue)",
                  "SQUARE_OFF": "var(--purple)", "KILL_SWITCH": "var(--red)",
                  "STALE_CARRYOVER": "var(--purple)"}.get(reason, "var(--t3)")

            try:
                opened = pd.to_datetime(r["opened_at"], utc=True).tz_convert(IST).strftime("%d-%b %H:%M")
            except Exception:
                opened = str(r.get("opened_at", ""))[:16]
            try:
                closed = pd.to_datetime(r["closed_at"], utc=True).tz_convert(IST).strftime("%d-%b %H:%M")
            except Exception:
                closed = str(r.get("closed_at", ""))[:16]
            duration = _fmt_duration(r.get("opened_at"), r.get("closed_at"))

            cc = st.columns(ct_widths)

            with cc[0]:
                st.markdown(f"<div style='padding:9px 0;font-size:13px;color:var(--t1);font-weight:600;'>{stock_display(ct_sym)}</div>", unsafe_allow_html=True)
            with cc[1]:
                st.markdown(f"<div style='padding:9px 0;'><span style='color:{side_c};font-weight:700;font-family:JetBrains Mono,monospace;font-size:11px;'>{side_lbl}</span></div>", unsafe_allow_html=True)
            with cc[2]:
                st.markdown(f"<div style='padding:9px 0;font-family:JetBrains Mono,monospace;font-size:12px;color:var(--t2);'>{qty}</div>", unsafe_allow_html=True)
            with cc[3]:
                st.markdown(
                    f"<div style='padding:9px 0;'>"
                    f"<div style='font-family:JetBrains Mono,monospace;font-size:13px;color:var(--t1);font-weight:700;'>₹{float(r['entry_price']):,.2f}</div>"
                    f"<div style='font-size:10px;color:var(--t3);font-family:JetBrains Mono,monospace;'>{opened}</div>"
                    f"</div>", unsafe_allow_html=True)
            with cc[4]:
                st.markdown(
                    f"<div style='padding:9px 0;'>"
                    f"<div style='font-family:JetBrains Mono,monospace;font-size:13px;color:var(--t1);font-weight:700;'>₹{float(r['exit_price']):,.2f}</div>"
                    f"<div style='font-size:10px;color:var(--t3);font-family:JetBrains Mono,monospace;'>{closed}</div>"
                    f"</div>", unsafe_allow_html=True)
            with cc[5]:
                st.markdown(f"<div style='padding:9px 0;font-family:JetBrains Mono,monospace;font-size:12px;color:{pnl_c};font-weight:700;'>{'+' if pnl>=0 else '-'}₹{abs(pnl):,.0f}</div>", unsafe_allow_html=True)
            with cc[6]:
                if net_pnl_val is not None:
                    net_c = "var(--green)" if net_pnl_val >= 0 else "var(--red)"
                    st.markdown(f"<div style='padding:9px 0;font-family:JetBrains Mono,monospace;font-size:12px;color:{net_c};font-weight:700;'>{'+' if net_pnl_val>=0 else '-'}₹{abs(net_pnl_val):,.0f}</div>", unsafe_allow_html=True)
                else:
                    st.markdown("<div style='padding:9px 0;'><span class='badge-pending'>–</span></div>", unsafe_allow_html=True)
            with cc[7]:
                st.markdown(f"<div style='padding:9px 0;font-size:11px;color:{rc};font-family:JetBrains Mono,monospace;text-transform:uppercase;'>{reason}</div>", unsafe_allow_html=True)
            with cc[8]:
                st.markdown(f"<div style='padding:9px 0;'>{strategy_pill_html(r.get('strategy',''), r.get('timeframe',''))}</div>", unsafe_allow_html=True)
            with cc[9]:
                st.markdown(f"<div style='padding:9px 0;font-size:11px;color:var(--t3);font-family:JetBrains Mono,monospace;'>{duration}</div>", unsafe_allow_html=True)
            with cc[10]:
                _ct_key = f"pt_closed_chart_{int(r['id'])}"
                if st.button("📈", key=_ct_key, help=f"View chart for {stock_display(ct_sym)}"):
                    if st.session_state.chart_symbol == ct_sym:
                        st.session_state.chart_symbol = None
                        st.session_state.chart_name   = None
                    else:
                        st.session_state.chart_symbol = ct_sym
                        st.session_state.chart_name   = stock_display(ct_sym)
                    st.rerun()

            st.markdown("<div class='row-div'></div>", unsafe_allow_html=True)

        # Pagination controls (only if more than one page)
        if pages > 1:
            nav1, nav2, nav3 = st.columns([1, 2, 1])
            with nav1:
                if st.button("← Prev", key="pt_prev", disabled=(pg <= 0)):
                    st.session_state.pt_closed_page = max(0, pg - 1)
                    st.rerun()
            with nav2:
                st.markdown(
                    f"<div style='text-align:center;font-size:11px;color:var(--t3);"
                    f"font-family:JetBrains Mono,monospace;padding-top:8px;'>"
                    f"Page {pg + 1} of {pages} &nbsp;·&nbsp; {total} closed trades</div>",
                    unsafe_allow_html=True,
                )
            with nav3:
                if st.button("Next →", key="pt_next", disabled=(pg >= pages - 1)):
                    st.session_state.pt_closed_page = min(pages - 1, pg + 1)
                    st.rerun()


render_paper_trading()


# ============================================================
# RENDER ALL SECTIONS
# ============================================================

if show_idx: render_section(idx_rows, "INDEXES",                    "#9b6dff")
if show_stk: render_section(stk_rows, "NSE STOCKS — F&O WATCHLIST", "#4a90e2", scroll_height=560)
if show_com: render_section(com_rows, "COMMODITIES — MCX",          "#f7a800")

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
        st.markdown('<div class="no-sig">No signals yet. Scheduler runs at 9:15 AM IST.</div>', unsafe_allow_html=True)
    else:
        logs_tf = all_logs[all_logs["Timeframe"] == selected_tf].copy() if not all_logs.empty else pd.DataFrame()
        if logs_tf.empty:
            st.markdown(f'<div class="no-sig">No signals for <strong>{selected_tf}</strong> timeframe yet.</div>', unsafe_allow_html=True)
        else:
            display = logs_tf[["Timestamp", "Stock", "Signal", "RSI", "Price", "Strategy"]].copy()
            try:
                display["Timestamp"] = pd.to_datetime(display["Timestamp"], utc=True).dt.tz_convert(IST).dt.strftime("%Y-%m-%d %H:%M IST")
            except Exception:
                pass

            _name_map = {
                **COMMODITIES_DISPLAY,
                **{s: stock_display(s) for s in fno_stocks},
                **INDEXES_DISPLAY,
            }
            display["Stock"] = display["Stock"].apply(lambda x: _name_map.get(x, x))
            display["RSI"]   = display["RSI"].apply(lambda x: f"{float(x):.2f}" if str(x).replace('.','').replace('-','').isdigit() else x)
            display["Price"] = display["Price"].apply(lambda x: f"₹{float(x):,.2f}" if str(x).replace('.','').replace('-','').isdigit() else x)

            is_dark  = st.session_state.dark_mode
            buy_bg   = "#0d2e1c" if is_dark else "#d4f7ec"
            sell_bg  = "#2e0d0d" if is_dark else "#fde8e8"
            buy_fg   = "#1ec9a0" if is_dark else "#065f46"
            sell_fg  = "#f05555" if is_dark else "#991b1b"

            def _col(v):
                if v == "BUY":  return f"background:{buy_bg};color:{buy_fg};font-weight:700;font-family:JetBrains Mono,monospace;font-size:12px;"
                if v == "SELL": return f"background:{sell_bg};color:{sell_fg};font-weight:700;font-family:JetBrains Mono,monospace;font-size:12px;"
                return "font-family:JetBrains Mono,monospace;font-size:12px;"

            # Build custom HTML table — respects dark mode CSS variables
            rows_html = ""
            for _, row in display.iterrows():
                sig = row["Signal"]
                if sig == "BUY":
                    sig_html = f"<span style='color:{buy_fg};font-weight:700;font-family:JetBrains Mono,monospace;'>{sig}</span>"
                elif sig == "SELL":
                    sig_html = f"<span style='color:{sell_fg};font-weight:700;font-family:JetBrains Mono,monospace;'>{sig}</span>"
                else:
                    sig_html = f"<span style='color:var(--t3);font-family:JetBrains Mono,monospace;'>{sig}</span>"

                rows_html += f"""
                <tr style='border-bottom:1px solid var(--border);'>
                    <td style='padding:8px 12px;font-size:12px;color:var(--t3);font-family:JetBrains Mono,monospace;'>{row['Timestamp']}</td>
                    <td style='padding:8px 12px;font-size:13px;color:var(--t1);font-weight:500;'>{row['Stock']}</td>
                    <td style='padding:8px 12px;'>{sig_html}</td>
                    <td style='padding:8px 12px;font-size:12px;color:var(--t2);font-family:JetBrains Mono,monospace;'>{row['RSI']}</td>
                    <td style='padding:8px 12px;font-size:12px;color:var(--t2);font-family:JetBrains Mono,monospace;'>{row['Price']}</td>
                    <td style='padding:8px 12px;font-size:11px;color:var(--t3);'>{row['Strategy']}</td>
                </tr>"""

            max_h = min(len(display) * 41 + 50, 450)
            st.markdown(f"""
<div style='overflow-y:auto;max-height:{max_h}px;border:1px solid var(--border);border-radius:8px;background:var(--card);'>
<table style='width:100%;border-collapse:collapse;'>
    <thead>
        <tr style='border-bottom:2px solid var(--border2);background:var(--card2);position:sticky;top:0;'>
            <th style='padding:10px 12px;text-align:left;font-size:11px;color:var(--t3);font-weight:600;text-transform:uppercase;letter-spacing:1px;white-space:nowrap;'>Timestamp</th>
            <th style='padding:10px 12px;text-align:left;font-size:11px;color:var(--t3);font-weight:600;text-transform:uppercase;letter-spacing:1px;'>Stock</th>
            <th style='padding:10px 12px;text-align:left;font-size:11px;color:var(--t3);font-weight:600;text-transform:uppercase;letter-spacing:1px;'>Signal</th>
            <th style='padding:10px 12px;text-align:left;font-size:11px;color:var(--t3);font-weight:600;text-transform:uppercase;letter-spacing:1px;'>RSI</th>
            <th style='padding:10px 12px;text-align:left;font-size:11px;color:var(--t3);font-weight:600;text-transform:uppercase;letter-spacing:1px;'>Price</th>
            <th style='padding:10px 12px;text-align:left;font-size:11px;color:var(--t3);font-weight:600;text-transform:uppercase;letter-spacing:1px;'>Strategy</th>
        </tr>
    </thead>
    <tbody>{rows_html}</tbody>
</table>
</div>
""", unsafe_allow_html=True)
except Exception as e:
    st.warning(f"Signal history unavailable: {e}")


# ============================================================
# FOOTER
# ============================================================

st.markdown("""
<div style='border-top:1px solid var(--border);margin-top:40px;padding-top:16px;font-size:11px;color:var(--t4);text-align:center;font-family:JetBrains Mono,monospace;letter-spacing:1px;'>
    FOR RESEARCH & INFORMATIONAL PURPOSES ONLY &nbsp;·&nbsp; NOT FINANCIAL ADVICE &nbsp;·&nbsp; TRADE AT YOUR OWN RISK
    <br><span style='font-size:10px;opacity:0.5;'>Charts powered by TradingView Lightweight Charts™ (Apache 2.0)</span>
</div>
""", unsafe_allow_html=True)