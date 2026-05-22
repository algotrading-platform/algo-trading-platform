# ============================================================
# app/dashboard/dashboard.py — Professional Trading Dashboard
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

from core.logger.signal_logger import SignalLogger
from core.backtesting.backtest_store import get_results
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

if "dark_mode" not in st.session_state:
    st.session_state.dark_mode = True


# ============================================================
# CSS
# ============================================================

DARK = """
:root {
    --bg:        #0a0e1a;
    --bg2:       #0f1629;
    --card:      #131c30;
    --border:    #1e2d4a;
    --border2:   #2a3f6b;
    --t1:        #e2e8f4;
    --t2:        #8896b3;
    --t3:        #4a5878;
    --blue:      #3b82f6;
    --green:     #10b981;
    --red:       #ef4444;
    --amber:     #f59e0b;
    --purple:    #8b5cf6;
    --buy-bg:    rgba(16,185,129,0.10);
    --buy-br:    rgba(16,185,129,0.35);
    --sell-bg:   rgba(239,68,68,0.10);
    --sell-br:   rgba(239,68,68,0.35);
    --hbuy-bg:   #0d2a1a;
    --hbuy-fg:   #10b981;
    --hsell-bg:  #2a0d0d;
    --hsell-fg:  #ef4444;
}
"""

LIGHT = """
:root {
    --bg:        #f0f4f8;
    --bg2:       #ffffff;
    --card:      #ffffff;
    --border:    #e2e8f0;
    --border2:   #cbd5e1;
    --t1:        #0f172a;
    --t2:        #475569;
    --t3:        #94a3b8;
    --blue:      #2563eb;
    --green:     #059669;
    --red:       #dc2626;
    --amber:     #d97706;
    --purple:    #7c3aed;
    --buy-bg:    rgba(5,150,105,0.07);
    --buy-br:    rgba(5,150,105,0.30);
    --sell-bg:   rgba(220,38,38,0.07);
    --sell-br:   rgba(220,38,38,0.30);
    --hbuy-bg:   #d1fae5;
    --hbuy-fg:   #065f46;
    --hsell-bg:  #fee2e2;
    --hsell-fg:  #991b1b;
}
"""

SHARED = """
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap');

.stApp { background: var(--bg) !important; font-family: 'IBM Plex Sans', sans-serif; }
section[data-testid="stSidebar"] { background: var(--bg2) !important; border-right: 1px solid var(--border) !important; }
#MainMenu, footer { visibility: hidden; }
.viewerBadge_container__r5tak { display: none; }
.stApp > header { background: transparent !important; }

/* Metrics */
div[data-testid="metric-container"] {
    background: var(--card) !important;
    border: 1px solid var(--border) !important;
    border-radius: 10px !important;
    padding: 14px 18px !important;
}
div[data-testid="metric-container"] label {
    color: var(--t3) !important; font-size: 10px !important;
    text-transform: uppercase !important; letter-spacing: 1.5px !important;
}
div[data-testid="metric-container"] [data-testid="stMetricValue"] {
    color: var(--t1) !important;
    font-family: 'JetBrains Mono', monospace !important;
    font-size: 20px !important; font-weight: 600 !important;
}
div[data-testid="metric-container"] [data-testid="stMetricDelta"] {
    font-family: 'JetBrains Mono', monospace !important; font-size: 11px !important;
}

/* Badges */
.badge-buy {
    display:inline-block; background:var(--buy-bg); border:1px solid var(--buy-br);
    color:var(--green); font-family:'JetBrains Mono',monospace;
    font-size:10px; font-weight:600; padding:3px 10px;
    border-radius:4px; letter-spacing:1.5px;
}
.badge-sell {
    display:inline-block; background:var(--sell-bg); border:1px solid var(--sell-br);
    color:var(--red); font-family:'JetBrains Mono',monospace;
    font-size:10px; font-weight:600; padding:3px 10px;
    border-radius:4px; letter-spacing:1.5px;
}

/* Section header */
.sec-hdr {
    display:flex; align-items:center; gap:10px;
    padding:14px 0 10px 0; border-bottom:1px solid var(--border); margin-bottom:12px;
}
.sec-title { font-size:11px; font-weight:600; color:var(--t2); text-transform:uppercase; letter-spacing:2.5px; }
.sec-meta  { font-size:10px; color:var(--t3); margin-left:4px; font-family:'JetBrains Mono',monospace; }

/* Table headers */
.col-hdr {
    font-size:10px; font-weight:600; color:var(--t3);
    text-transform:uppercase; letter-spacing:1.5px;
    padding:6px 0; border-bottom:1px solid var(--border);
}

/* Row content */
.stock-name   { font-family:'JetBrains Mono',monospace; font-size:13px; font-weight:600; color:var(--t1); }
.stock-sym    { font-size:10px; color:var(--t3); margin-top:1px; font-family:'JetBrains Mono',monospace; }
.mono         { font-family:'JetBrains Mono',monospace; font-size:12px; color:var(--t1); }
.mono-muted   { font-family:'JetBrains Mono',monospace; font-size:11px; color:var(--t3); }
.row-div      { border-top:1px solid var(--border); margin:2px 0; opacity:0.5; }
.no-sig       {
    background:var(--card); border:1px solid var(--border);
    border-radius:8px; padding:16px 20px; text-align:center;
    color:var(--t3); font-size:12px; margin-bottom:20px;
}

/* Backtest summary card */
.bt-card {
    background:var(--card); border:1px solid var(--border);
    border-radius:8px; padding:12px 16px;
    display:flex; gap:24px; align-items:center;
    margin-bottom:12px; flex-wrap:wrap;
}
.bt-item { text-align:center; min-width:70px; }
.bt-label { font-size:9px; color:var(--t3); text-transform:uppercase; letter-spacing:1.5px; margin-bottom:3px; }
.bt-val   { font-family:'JetBrains Mono',monospace; font-size:14px; font-weight:600; color:var(--t1); }
.bt-val.pos { color:var(--green); }
.bt-val.neg { color:var(--red);   }
.bt-val.neu { color:var(--t2);    }

/* Market status */
.mkt-open {
    display:inline-flex; align-items:center; gap:6px;
    background:rgba(16,185,129,0.10); border:1px solid rgba(16,185,129,0.30);
    color:#10b981; padding:6px 16px; border-radius:20px;
    font-size:11px; font-weight:600; font-family:'JetBrains Mono',monospace; letter-spacing:1px;
}
.mkt-closed {
    display:inline-flex; align-items:center; gap:6px;
    background:rgba(239,68,68,0.10); border:1px solid rgba(239,68,68,0.30);
    color:#ef4444; padding:6px 16px; border-radius:20px;
    font-size:11px; font-weight:600; font-family:'JetBrains Mono',monospace; letter-spacing:1px;
}
.pulse {
    width:7px; height:7px; border-radius:50%; background:currentColor;
    animation:pa 2s infinite;
}
@keyframes pa { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:0.4;transform:scale(0.8)} }

/* Telegram */
.tg-ok  { background:rgba(16,185,129,0.08); border:1px solid rgba(16,185,129,0.25); border-radius:6px; padding:8px 12px; font-size:10px; color:#10b981; font-family:'JetBrains Mono',monospace; letter-spacing:1px; text-align:center; }
.tg-err { background:rgba(239,68,68,0.06);  border:1px solid rgba(239,68,68,0.20);  border-radius:6px; padding:8px 12px; font-size:10px; color:#ef4444; font-family:'JetBrains Mono',monospace; letter-spacing:1px; text-align:center; }

/* Scrollbar */
::-webkit-scrollbar { width:4px; height:4px; }
::-webkit-scrollbar-track { background:var(--bg); }
::-webkit-scrollbar-thumb { background:var(--border2); border-radius:2px; }

.stDataFrame { border:1px solid var(--border) !important; border-radius:8px !important; }
"""

theme = DARK if st.session_state.dark_mode else LIGHT
st.markdown(f"<style>{theme}{SHARED}</style>", unsafe_allow_html=True)


# ============================================================
# HELPERS
# ============================================================

def market_open() -> bool:
    from datetime import time as dtime
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    return dtime(9, 15) <= now.time() <= dtime(15, 30)


def tv_url(tv_sym: str, tf: str) -> str:
    return (
        f"https://www.tradingview.com/chart/"
        f"?symbol={tv_sym}&interval={TV_INTERVALS.get(tf,'D')}"
    )

def _stock_tv(sym: str) -> str:
    return f"NSE:{sym.replace('.NS','')}"

def rsi_style(rsi) -> str:
    try:
        v = float(rsi)
        if v < 30:   return "color:var(--red);font-weight:600;"
        elif v > 70: return "color:var(--amber);font-weight:600;"
        return "color:var(--t2);"
    except: return "color:var(--t3);"

def pnl_class(val) -> str:
    try:
        return "pos" if float(val) >= 0 else "neg"
    except: return "neu"

def fmt_pnl(val) -> str:
    try:
        v = float(val)
        return f"{'+'if v>=0 else ''}₹{v:,.2f}"
    except: return "—"

def fmt_pct(val) -> str:
    try:
        v = float(val)
        return f"{'+'if v>=0 else ''}{v:.1f}%"
    except: return "—"


# ============================================================
# DATA
# ============================================================

if "logger" not in st.session_state:
    st.session_state.logger = SignalLogger()
logger = st.session_state.logger


def get_latest_signals(tf: str) -> pd.DataFrame:
    logs = logger.get_logs()
    if logs.empty: return pd.DataFrame()
    tf_logs = logs[logs["Timeframe"] == tf].copy()
    if tf_logs.empty: return pd.DataFrame()
    tf_logs["Timestamp"] = pd.to_datetime(tf_logs["Timestamp"])
    return (
        tf_logs.sort_values("Timestamp", ascending=False)
               .groupby("Stock").first().reset_index()
    )


# ============================================================
# SIDEBAR
# ============================================================

with st.sidebar:
    st.markdown(f"""
    <div style='padding:4px 0 18px;'>
        <div style='font-family:JetBrains Mono,monospace;font-size:15px;
                    font-weight:600;color:var(--blue);letter-spacing:2px;'>ALGO SIGNALS</div>
        <div style='font-size:10px;color:var(--t3);letter-spacing:2px;
                    text-transform:uppercase;margin-top:3px;'>NSE · BSE · MCX</div>
    </div>
    <div style='border-top:1px solid var(--border);margin-bottom:16px;'></div>
    """, unsafe_allow_html=True)

    # Theme toggle — label shows what you'll switch TO
    btn_label = "☀️ Switch to Light" if st.session_state.dark_mode else "🌙 Switch to Dark"
    if st.button(btn_label, use_container_width=True):
        st.session_state.dark_mode = not st.session_state.dark_mode
        st.rerun()

    st.markdown("<div style='margin:12px 0;border-top:1px solid var(--border);'></div>", unsafe_allow_html=True)

    st.markdown('<div style="font-size:10px;font-weight:600;color:var(--t3);text-transform:uppercase;letter-spacing:2px;margin-bottom:6px;">Timeframe</div>', unsafe_allow_html=True)
    selected_tf = st.selectbox("Timeframe", list(TIMEFRAMES.keys()), index=2, label_visibility="collapsed")

    st.markdown("<div style='margin:12px 0;border-top:1px solid var(--border);'></div>", unsafe_allow_html=True)

    st.markdown('<div style="font-size:10px;font-weight:600;color:var(--t3);text-transform:uppercase;letter-spacing:2px;margin-bottom:6px;">Markets</div>', unsafe_allow_html=True)
    show_idx  = st.checkbox("Indexes",     value=True)
    show_stk  = st.checkbox("Stocks",      value=True)
    show_com  = st.checkbox("Commodities", value=True)

    st.markdown("<div style='margin:12px 0;border-top:1px solid var(--border);'></div>", unsafe_allow_html=True)

    tg = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if tg:
        st.markdown('<div class="tg-ok">✓ TELEGRAM CONNECTED</div>', unsafe_allow_html=True)
    else:
        st.markdown('<div class="tg-err">✗ TELEGRAM NOT SET</div>', unsafe_allow_html=True)

    st.markdown("<div style='margin:12px 0;border-top:1px solid var(--border);'></div>", unsafe_allow_html=True)

    fetch_period = PERIOD_MAP.get(selected_tf, "3mo")
    st.markdown(f"""
    <div style='font-size:10px;color:var(--t3);line-height:2;'>
        <div>Viewing &nbsp;<span style='color:var(--t2);font-family:JetBrains Mono,monospace;'>{selected_tf}</span></div>
        <div>Period &nbsp;&nbsp;<span style='color:var(--t2);font-family:JetBrains Mono,monospace;'>{fetch_period}</span></div>
        <div>Refresh &nbsp;<span style='color:var(--t2);font-family:JetBrains Mono,monospace;'>60s</span></div>
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
    <div style='padding:6px 0 2px;'>
        <h1 style='font-family:IBM Plex Sans,sans-serif;font-size:28px;
                   font-weight:600;color:var(--t1);letter-spacing:-0.5px;margin:0;'>
            Signal Dashboard
        </h1>
        <div style='font-size:11px;color:var(--t3);margin-top:5px;font-family:JetBrains Mono,monospace;'>
            {ist_now.strftime('%d %b %Y &nbsp;·&nbsp; %H:%M:%S IST')}
            &nbsp;·&nbsp; {selected_tf} &nbsp;·&nbsp; Auto-refresh 60s
        </div>
    </div>
    """, unsafe_allow_html=True)
with hr:
    st.markdown("<div style='height:14px;'></div>", unsafe_allow_html=True)
    if is_open:
        st.markdown('<div style="text-align:right;"><span class="mkt-open"><span class="pulse"></span>MARKET OPEN</span></div>', unsafe_allow_html=True)
    else:
        st.markdown('<div style="text-align:right;"><span class="mkt-closed"><span class="pulse"></span>MARKET CLOSED</span></div>', unsafe_allow_html=True)

st.markdown("<div style='border-top:1px solid var(--border);margin:14px 0 20px;'></div>", unsafe_allow_html=True)


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

        # Signal data
        if not latest_signals.empty and sym in latest_signals["Stock"].values:
            d     = latest_signals[latest_signals["Stock"] == sym].iloc[0]
            sig   = str(d["Signal"])
            rsi   = d["RSI"]
            price = d["Price"]
            ts    = str(d["Timestamp"])[:16]
        else:
            sig = "HOLD"; rsi = "—"; price = "—"; ts = "—"

        # Backtest data
        bt = {}
        if not backtest_data.empty and sym in backtest_data["Symbol"].values:
            br       = backtest_data[backtest_data["Symbol"] == sym].iloc[0]
            bt = {
                "trades":   int(br.get("Trades",   0)),
                "pnl":      br.get("PnL",      0.0),
                "pnl_pct":  br.get("PnL %",    0.0),
                "win_rate": br.get("Win Rate %",0.0),
                "period":   br.get("Period",   fetch_period),
            }

        if sig == "BUY":    total_buy  += 1
        elif sig == "SELL": total_sell += 1
        else:               total_hold += 1

        rows.append({
            "sym": sym, "name": name,
            "tv":  tv_url(tv, selected_tf),
            "signal": sig,
            "sig_rsi": rsi,       # RSI at signal time
            "sig_price": price,   # Price at signal time
            "rsi": rsi,           # kept for compatibility
            "price": price,       # kept for compatibility
            "ts": ts,
            "bt": bt,
        })
    return rows


idx_rows = build_rows(INDEXES, INDEXES_DISPLAY, INDEXES_TV) if show_idx else []
stk_rows = build_rows(STOCKS, {s:stock_display(s) for s in STOCKS}, {s:_stock_tv(s) for s in STOCKS}) if show_stk else []
com_rows = build_rows(COMMODITIES, COMMODITIES_DISPLAY, COMMODITIES_TV, skip_tf=COMMODITIES_SKIP_TIMEFRAMES) if show_com else []


# ============================================================
# GLOBAL KPI BAR
# ============================================================

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Total Scanned",  total_buy + total_sell + total_hold)
k2.metric("BUY Signals",    total_buy,
          delta=f"+{total_buy}" if total_buy > 0 else None)
k3.metric("SELL Signals",   total_sell,
          delta=f"-{total_sell}" if total_sell > 0 else None,
          delta_color="inverse")
k4.metric("HOLD",           total_hold)
k5.metric("Timeframe",      selected_tf)

st.markdown("<div style='margin:24px 0 8px;'></div>", unsafe_allow_html=True)


# ============================================================
# BACKTEST CATEGORY SUMMARY
# ============================================================

def backtest_summary_bar(rows: list[dict], period: str) -> None:
    """Show aggregated backtest KPIs for a category."""
    bt_rows = [r["bt"] for r in rows if r.get("bt")]
    if not bt_rows:
        return

    total_trades = sum(b.get("trades", 0) for b in bt_rows)
    total_pnl    = sum(b.get("pnl",    0) for b in bt_rows)
    avg_wr       = round(
        sum(b.get("win_rate",0) for b in bt_rows) / len(bt_rows), 1
    ) if bt_rows else 0.0
    avg_pct      = round(
        sum(b.get("pnl_pct",0) for b in bt_rows) / len(bt_rows), 1
    ) if bt_rows else 0.0

    pnl_c  = pnl_class(total_pnl)
    pct_c  = pnl_class(avg_pct)
    wr_c   = "pos" if avg_wr >= 50 else "neg"

    st.markdown(f"""
    <div class="bt-card">
        <div style='font-size:9px;color:var(--t3);text-transform:uppercase;
                    letter-spacing:1.5px;margin-right:8px;'>
            Backtest ({period})
        </div>
        <div class="bt-item">
            <div class="bt-label">Trades</div>
            <div class="bt-val neu">{total_trades}</div>
        </div>
        <div class="bt-item">
            <div class="bt-label">Total PnL</div>
            <div class="bt-val {pnl_c}">{fmt_pnl(total_pnl)}</div>
        </div>
        <div class="bt-item">
            <div class="bt-label">Avg PnL %</div>
            <div class="bt-val {pct_c}">{fmt_pct(avg_pct)}</div>
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

def render_section(rows, title, dot_color="#3b82f6"):
    if not rows: return

    action = [r for r in rows if r["signal"] in ("BUY","SELL")]
    holds  = len([r for r in rows if r["signal"] == "HOLD"])
    act_color = "var(--green)" if action else "var(--t3)"

    # Section header
    st.markdown(f"""
    <div class="sec-hdr">
        <div style='width:6px;height:6px;border-radius:50%;
                    background:{dot_color};flex-shrink:0;'></div>
        <span class="sec-title">{title}</span>
        <span class="sec-meta">
            {len(rows)} instruments &nbsp;·&nbsp;
            <span style='color:{act_color};'>{len(action)} active</span>
            &nbsp;·&nbsp; {holds} hold
        </span>
    </div>
    """, unsafe_allow_html=True)

    # Backtest summary bar
    backtest_summary_bar(rows, fetch_period)

    if not action:
        st.markdown('<div class="no-sig">No active signals — all instruments HOLD</div>', unsafe_allow_html=True)
        return

    # Sort by signal time — most recent first
    def _ts_sort(r):
        try:
            return str(r.get("ts", "—"))
        except:
            return "—"
    action = sorted(action, key=_ts_sort, reverse=True)

    # Column headers — now includes Current RSI and Current Price
    h = st.columns([2.0, 0.9, 0.8, 1.1, 0.8, 1.1, 1.0, 1.0, 1.7, 0.7])
    for col, lbl in zip(h, ["Instrument","Signal","Sig RSI","Sig Price","Cur RSI","Cur Price","PnL","Win Rate","Signal Time","Chart"]):
        col.markdown(f'<div class="col-hdr">{lbl}</div>', unsafe_allow_html=True)

    for row in action:
        c  = st.columns([2.0, 0.9, 0.8, 1.1, 0.8, 1.1, 1.0, 1.0, 1.7, 0.7])
        bt = row.get("bt", {})

        # Fetch current RSI and Price live
        # Fallback to signal values if live fetch fails
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
                _df["RSI"] = rsi_indicator.calculate(_close)
                _df.dropna(subset=["RSI"], inplace=True)
                if not _df.empty:
                    _lt       = _df.iloc[-1]
                    cur_rsi   = round(float(_lt["RSI"]), 2)
                    cur_price = round(float(_lt["Close"]), 2)
                    cur_live  = True
        except Exception:
            pass

        with c[0]:
            st.markdown(f"""
            <div style='padding:10px 0 8px;'>
                <div class="stock-name">{row['name']}</div>
                <div class="stock-sym">{row['sym']}</div>
            </div>""", unsafe_allow_html=True)

        with c[1]:
            badge = '<span class="badge-buy">BUY</span>' if row["signal"] == "BUY" \
                    else '<span class="badge-sell">SELL</span>'
            st.markdown(f"<div style='padding:12px 0;'>{badge}</div>", unsafe_allow_html=True)

        with c[2]:
            _sv  = row["sig_rsi"]
            _ss  = rsi_style(_sv)
            st.markdown(
                f"<div style='padding:12px 0;font-family:JetBrains Mono,monospace;"
                f"font-size:12px;{_ss}'>{_sv}</div>",
                unsafe_allow_html=True)

        with c[3]:
            try:    sp = f"₹{float(row['sig_price']):,.2f}"
            except: sp = str(row["sig_price"])
            st.markdown(f"<div class='mono-muted' style='padding:12px 0;font-size:11px;'>{sp}</div>", unsafe_allow_html=True)

        with c[4]:
            _cs = rsi_style(cur_rsi)
            st.markdown(
                f"<div style='padding:12px 0;font-family:JetBrains Mono,monospace;"
                f"font-size:12px;{_cs}'>{cur_rsi}</div>",
                unsafe_allow_html=True)

        with c[5]:
            try:    cp = f"₹{float(cur_price):,.2f}"
            except: cp = str(cur_price)
            try:
                diff  = float(cur_price) - float(row["sig_price"])
                if not cur_live:
                    cp_c = "var(--t2)"   # grey — same as signal, not live
                else:
                    cp_c = "var(--green)" if diff >= 0 else "var(--red)"
            except:
                cp_c  = "var(--t1)"
            # Show (live) or (last) indicator
            live_tag = "" if cur_live else "<span style='font-size:9px;color:var(--t3);margin-left:4px;'>last</span>"
            st.markdown(f"<div style='padding:12px 0;font-family:JetBrains Mono,monospace;font-size:12px;color:{cp_c};'>{cp}{live_tag}</div>", unsafe_allow_html=True)

        with c[6]:
            pnl_v = bt.get("pnl", None)
            pnl_s = fmt_pnl(pnl_v) if pnl_v is not None else "—"
            pc    = pnl_class(pnl_v) if pnl_v is not None else "neu"
            color = "var(--green)" if pc=="pos" else ("var(--red)" if pc=="neg" else "var(--t3)")
            st.markdown(f"<div style='padding:12px 0;font-family:JetBrains Mono,monospace;font-size:11px;color:{color};'>{pnl_s}</div>", unsafe_allow_html=True)

        with c[7]:
            wr_v  = bt.get("win_rate", None)
            wr_s  = f"{wr_v:.1f}%" if wr_v is not None else "—"
            wr_c  = "var(--green)" if wr_v and wr_v >= 50 else "var(--t3)"
            st.markdown(f"<div style='padding:12px 0;font-family:JetBrains Mono,monospace;font-size:11px;color:{wr_c};'>{wr_s}</div>", unsafe_allow_html=True)

        with c[8]:
            st.markdown(f"<div class='mono-muted' style='padding:12px 0;'>{row['ts']}</div>", unsafe_allow_html=True)

        with c[9]:
            st.link_button("Chart", row["tv"], use_container_width=True)

        st.markdown("<div class='row-div'></div>", unsafe_allow_html=True)
    st.markdown("<div style='margin-bottom:24px;'></div>", unsafe_allow_html=True)


# ============================================================
# RENDER ALL
# ============================================================

if show_idx:  render_section(idx_rows, "INDEXES",                    "#8b5cf6")
if show_stk:  render_section(stk_rows, "NSE STOCKS — F&O WATCHLIST", "#3b82f6")
if show_com:  render_section(com_rows, "COMMODITIES — MCX",          "#f59e0b")


# ============================================================
# SIGNAL HISTORY
# ============================================================

st.markdown("""
<div class="sec-hdr" style='margin-top:8px;'>
    <div style='width:6px;height:6px;border-radius:50%;background:#10b981;flex-shrink:0;'></div>
    <span class="sec-title">Signal History — Last 7 Days</span>
</div>
""", unsafe_allow_html=True)

try:
    if all_logs.empty:
        st.markdown('<div class="no-sig">No signals yet. Ensure <code>run_scheduler.py</code> is running.</div>', unsafe_allow_html=True)
    else:
        logs_tf = all_logs[all_logs["Timeframe"] == selected_tf].copy()
        if logs_tf.empty:
            st.markdown(f'<div class="no-sig">No signals for <strong>{selected_tf}</strong> yet.</div>', unsafe_allow_html=True)
        else:
            is_dark  = st.session_state.dark_mode
            buy_bg   = "#0d2a1a" if is_dark else "#d1fae5"
            sell_bg  = "#2a0d0d" if is_dark else "#fee2e2"
            buy_fg   = "#10b981" if is_dark else "#065f46"
            sell_fg  = "#ef4444" if is_dark else "#991b1b"

            def _col(v):
                if v=="BUY":  return f"background:{buy_bg};color:{buy_fg};font-weight:600;font-family:JetBrains Mono,monospace;"
                if v=="SELL": return f"background:{sell_bg};color:{sell_fg};font-weight:600;font-family:JetBrains Mono,monospace;"
                return ""

            cols = [c for c in ["Timestamp","Stock","Signal","RSI","Price"] if c in logs_tf.columns]
            st.dataframe(
                logs_tf[cols].style.map(_col, subset=["Signal"]),
                use_container_width=True, hide_index=True, height=280,
            )
except Exception as e:
    st.warning(f"Signal history unavailable: {e}")


# ============================================================
# FOOTER
# ============================================================

st.markdown("""
<div style='border-top:1px solid var(--border);margin-top:32px;padding-top:14px;
            font-size:10px;color:var(--t3);text-align:center;
            font-family:JetBrains Mono,monospace;letter-spacing:1px;'>
    FOR RESEARCH & INFORMATIONAL PURPOSES ONLY &nbsp;·&nbsp;
    NOT FINANCIAL ADVICE &nbsp;·&nbsp; TRADE AT YOUR OWN RISK
</div>
""", unsafe_allow_html=True)
