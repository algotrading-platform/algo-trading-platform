# ============================================================
# app/dashboard/dashboard.py
# ============================================================

import sys
import os
import json
import time
import bisect
import contextlib
import urllib.parse

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
sys.path.append(_project_root)

from dotenv import load_dotenv
load_dotenv(os.path.join(_project_root, ".env"))

import streamlit as st
import pandas as pd
from datetime import datetime
import pytz
import msal
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
# LOGIN GATE — Microsoft Entra ID (MSAL) sign-in
#
# Auth-code flow via msal.ConfidentialClientApplication. The pending
# flow (state/nonce/code_verifier) is persisted in the app_config DB
# table (see _store_pending_flow/_pop_pending_flow below), NOT
# st.session_state and NOT an in-process store — the sign-in link is
# a real top-level browser navigation away to login.microsoftonline.com
# and back, which starts a brand-new Streamlit session (and can land on
# a different container replica/process) with empty session_state, so
# anything stashed in-process before the redirect would already be gone
# by the time Entra redirects back.
#
# Both the sign-in and logout links use target="_self": Streamlit's
# markdown renderer stamps target="_blank" on any <a> left unspecified,
# which was sending the Entra round-trip to a new tab and leaving the
# original tab stuck on this screen forever.
#
# Must run BEFORE st_autorefresh and everything else, so an
# unauthenticated visitor never triggers any dashboard logic at all —
# not even the refresh timer.
# ============================================================

IST = pytz.timezone("Asia/Kolkata")  # moved up from below set_page_config —
                                      # needed here for the login screen's
                                      # own live market-status readout

ENTRA_CLIENT_ID     = os.environ.get("ENTRA_CLIENT_ID", "")
ENTRA_TENANT_ID     = os.environ.get("ENTRA_TENANT_ID", "")
ENTRA_CLIENT_SECRET = os.environ.get("ENTRA_CLIENT_SECRET", "")
ENTRA_REDIRECT_URI  = os.environ.get("ENTRA_REDIRECT_URI", "")

# App-level allow-list, checked in addition to (not instead of) the Entra app
# registration's "assignment required" toggle -- that toggle is otherwise the
# ONLY thing restricting sign-in to these 3 accounts, with no code-level
# fallback if it's ever flipped or the app is made multi-tenant. Defaults to
# the current deliberately-final 3-person list so this is safe even where
# ENTRA_ALLOWED_UPNS isn't set.
ENTRA_ALLOWED_UPNS = {
    u.strip().lower()
    for u in os.environ.get(
        "ENTRA_ALLOWED_UPNS",
        "cgummunur@ariqt.com,rkumar@ariqt.com,algotrading@ariqt.com",
    ).split(",")
    if u.strip()
}


# Pending OAuth flows (state -> MSAL flow dict) are persisted in the
# app_config table, NOT an in-process st.cache_resource dict as before.
# That in-memory store was lost every time the container restarted or
# redeployed (which happens routinely on this project) -- anyone
# mid-login at that moment landed on a fresh process with an empty
# store and got a false "session expired" on an otherwise-valid code
# exchange. The DB survives restarts, so this removes that failure
# mode entirely, not just widens the window before it.
FLOW_TTL_SECONDS = 900  # was implicitly 600s via the old dict's own prune
                        # loop -- widened since the tenant's Conditional
                        # Access "sign in to browser" prompt (a policy this
                        # app doesn't control) can add real minutes to the
                        # round-trip before Entra redirects back.


def _store_pending_flow(state: str, flow: dict) -> bool:
    from core.database import set_config, delete_config_prefix_older_than
    delete_config_prefix_older_than("oauth_flow:", FLOW_TTL_SECONDS)
    return set_config(f"oauth_flow:{state}", json.dumps({"flow": flow, "created_at": time.time()}))


def _pop_pending_flow(state: str):
    """
    One-time read of a pending flow. Returns (flow_dict, None) on success.
    Returns (None, "expired") vs (None, "not_found") as DISTINCT outcomes
    (was one generic message for both) so a real timeout is now
    distinguishable from a replayed/duplicate callback URL -- the two
    have different, actionable causes for the person hitting them.
    """
    from core.database import get_config, delete_config
    raw = get_config(f"oauth_flow:{state}") if state else None
    if raw is None:
        return None, "not_found"
    delete_config(f"oauth_flow:{state}")  # one-time use regardless of outcome below
    try:
        entry = json.loads(raw)
    except (TypeError, ValueError):
        return None, "not_found"
    if time.time() - entry["created_at"] > FLOW_TTL_SECONDS:
        return None, "expired"
    return entry["flow"], None


def _msal_app():
    return msal.ConfidentialClientApplication(
        ENTRA_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{ENTRA_TENANT_ID}",
        client_credential=ENTRA_CLIENT_SECRET,
    )


# ── LOGIN SCREEN — styled unconditionally, before auth succeeds, so it
# never renders as bare unstyled Streamlit. Dark terminal aesthetic
# matching the rest of the app (same palette/type as the main theme
# below), not a separate visual identity. Signature element: a live
# IST clock + real MARKET OPEN/CLOSED badge at the top of the card —
# reuses the same badge classes and market-hours logic as the live
# dashboard, so the login screen reads as part of the terminal, not a
# bolted-on auth wall, before a single credential is even typed.
if not st.session_state.get("user"):
    qp = st.query_params

    if "error" in qp:
        _msg = qp.get("error_description", qp.get("error", "Sign-in failed."))
        if qp.get("state"):
            # Otherwise this pending flow (e.g. user clicked "Cancel" on the
            # Microsoft form) just sits in app_config until the next
            # visitor's TTL sweep instead of being cleaned up immediately.
            from core.database import delete_config
            delete_config(f"oauth_flow:{qp.get('state')}")
        qp.clear()
        st.error(_msg.split("\r\n")[0])
        st.stop()

    if "code" in qp:
        # Exchange the auth code exactly once: _pop_pending_flow() deletes
        # the DB row so a stray reload of this same callback URL fails
        # cleanly instead of re-exchanging an already-redeemed code.
        _flow, _flow_err = _pop_pending_flow(qp.get("state", ""))
        if _flow is None:
            qp.clear()  # otherwise F5 replays the same dead ?code&state forever
            if _flow_err == "expired":
                st.error(f"Sign-in took longer than {FLOW_TTL_SECONDS // 60} minutes and expired — please try again.")
            else:
                st.error("Sign-in link was already used or could not be found — please try again.")
            st.stop()

        try:
            _result = _msal_app().acquire_token_by_auth_code_flow(_flow, qp.to_dict())
        except Exception:
            qp.clear()
            st.error("Sign-in could not be verified — please try again.")
            st.stop()

        if "error" in _result or "id_token_claims" not in _result:
            qp.clear()
            st.error(_result.get("error_description", _result.get("error", "Sign-in failed — please try again.")))
            st.stop()

        _claims = _result["id_token_claims"]
        _upn = (_claims.get("preferred_username") or _claims.get("upn") or "").strip().lower()
        if _upn not in ENTRA_ALLOWED_UPNS:
            # Defense in depth: the Entra app registration's "assignment
            # required" toggle is otherwise the only thing keeping non-
            # assigned accounts out. This never triggers for an assigned
            # account today; it only matters if that toggle is ever changed.
            qp.clear()
            st.error("This account is not authorized to access this dashboard.")
            st.stop()

        st.session_state["user"] = _claims
        qp.clear()
        st.rerun()  # strip ?code&state from the URL before anything else runs

    from datetime import time as _dtime
    _login_now = datetime.now(IST)
    _login_open = _login_now.weekday() < 5 and _dtime(9, 15) <= _login_now.time() <= _dtime(15, 30)
    _badge_cls  = "mkt-open" if _login_open else "mkt-closed"
    _badge_text = "MARKET OPEN" if _login_open else "MARKET CLOSED"

    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@300;400;500;600;700&display=swap');

    .stApp { background: #080c18 !important; }
    #MainMenu, footer, header { visibility: hidden; }
    section[data-testid="stSidebar"] { display: none !important; }
    .block-container { padding-top: 6vh !important; max-width: 460px !important; }

    .login-wrap { text-align: center; margin-bottom: 28px; }
    .login-word {
        font-family: 'IBM Plex Sans', sans-serif; font-size: 30px; font-weight: 700;
        color: #f1f5fb; letter-spacing: 3px; margin-bottom: 6px;
    }
    .login-word span { color: #4a90e2; }
    .login-tag {
        font-family: 'JetBrains Mono', monospace; font-size: 11px; color: #6b7fa0;
        letter-spacing: 3px; text-transform: uppercase; margin-bottom: 18px;
    }
    .login-status {
        display: inline-flex; align-items: center; gap: 10px;
        font-family: 'JetBrains Mono', monospace; font-size: 11px; color: #7a8cae;
        border: 1px solid #1a2840; border-radius: 20px; padding: 6px 16px;
        background: rgba(255,255,255,0.02);
    }
    .mkt-open { display:inline-flex; align-items:center; gap:6px; color:#1ec9a0; font-weight:700; letter-spacing:1px; }
    .mkt-closed { display:inline-flex; align-items:center; gap:6px; color:#f05555; font-weight:700; letter-spacing:1px; }
    .pulse-dot { width:6px; height:6px; border-radius:50%; background:currentColor; animation:loginpulse 2s infinite; }
    @keyframes loginpulse { 0%,100%{opacity:1} 50%{opacity:0.35} }

    .login-card {
        background: linear-gradient(180deg, #101828 0%, #0d1526 100%);
        border: 1px solid #1a2840; border-radius: 14px;
        padding: 32px 32px 26px;
        box-shadow: 0 0 0 1px rgba(74,144,226,0.06), 0 20px 50px rgba(0,0,0,0.45);
        position: relative; overflow: hidden;
    }
    .login-card::before {
        content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
        background: linear-gradient(90deg, #4a90e2, #9b6dff);
    }
    .login-card h3 {
        font-family: 'IBM Plex Sans', sans-serif; font-size: 17px;
        font-weight: 600; color: #f1f5fb; margin: 0 0 18px;
    }
    .entra-signin-btn {
        display: flex; align-items: center; justify-content: center; gap: 10px;
        width: 100%; margin-top: 4px; border-radius: 8px; box-sizing: border-box;
        background: #4a90e2; border: 1px solid #4a90e2;
        color: #fff !important; font-weight: 600; font-size: 14px; padding: 11px;
        font-family: 'IBM Plex Sans', sans-serif; text-decoration: none !important;
        transition: all 0.15s ease;
    }
    .entra-signin-btn:hover {
        background: #3d7dc9; border-color: #3d7dc9;
        box-shadow: 0 4px 14px rgba(74,144,226,0.35);
    }
    .ms-logo { display: inline-grid; grid-template-columns: 1fr 1fr; gap: 2px; width: 16px; height: 16px; }
    .ms-logo span { display: block; }
    .login-footer {
        text-align: center; margin-top: 22px; font-family: 'JetBrains Mono', monospace;
        font-size: 10px; color: #3d5070; letter-spacing: 1px;
    }
    </style>
    """, unsafe_allow_html=True)

    st.markdown(f"""
    <div class="login-wrap">
        <div class="login-word">ALGO <span>SIGNALS</span></div>
        <div class="login-tag">NSE &nbsp;·&nbsp; BSE &nbsp;·&nbsp; MCX</div>
        <div class="login-status">
            <span>{_login_now.strftime('%d %b %Y &nbsp; %H:%M:%S IST')}</span>
            <span class="{_badge_cls}"><span class="pulse-dot"></span>{_badge_text}</span>
        </div>
    </div>
    """, unsafe_allow_html=True)

    if not (ENTRA_CLIENT_ID and ENTRA_TENANT_ID and ENTRA_CLIENT_SECRET and ENTRA_REDIRECT_URI):
        st.error("Sign-in is misconfigured — one or more ENTRA_* environment variables are missing. Contact an admin.")
        st.stop()

    # prompt="login" skips Entra's silent-SSO / device-broker check, which
    # is what hands off to Edge's own native account/device flow (the
    # "Sign in with your work account" nudge + Intune "secure this device"
    # wall seen on 2026-08-24 for algotrading@ariqt.com and for Jwala's
    # account) before our own MSAL login screen ever gets a chance to run.
    # This forces a plain interactive Entra sign-in instead, decoupled from
    # whichever Windows/Edge identity is already cached on the device.
    # (Tried once before in c30f403, removed in fec7023 -- but only because
    # it wasn't the fix for that commit's actual bug, the new-tab redirect,
    # which target="_self" below fixed. Never disproven for THIS issue.)
    _flow = _msal_app().initiate_auth_code_flow(
        scopes=["User.Read"], redirect_uri=ENTRA_REDIRECT_URI, prompt="login",
    )
    if not _store_pending_flow(_flow["state"], _flow):
        # set_config() swallows DB errors and returns False -- without this
        # check a DB outage (e.g. this host's IP not allow-listed on the
        # Azure SQL firewall) rendered a normal-looking sign-in link that
        # would always fail at the callback with "already used or could
        # not be found", with no indication the real cause was the DB.
        st.error("Sign-in is temporarily unavailable — could not reach the database. Please try again shortly.")
        st.stop()

    # The sign-in link is a real top-level navigation to
    # login.microsoftonline.com and back. Streamlit's markdown renderer
    # stamps target="_blank" on any <a> with no explicit target, which was
    # sending this navigation to a NEW tab -- the Entra callback then
    # completed in that new tab while the original tab sat on this login
    # screen forever, looking like sign-in silently did nothing. Explicit
    # target="_self" keeps it a same-tab navigation. (prompt="login" and
    # an inline onclick=window.location.assign were tried earlier and
    # didn't help: the former doesn't affect tab targeting at all, and the
    # latter is inert -- React never attaches string on* attributes from
    # markdown-rendered HTML.)
    st.markdown(f"""
    <div class="login-card">
        <h3>Sign in</h3>
        <a href="{_flow['auth_uri']}" class="entra-signin-btn" target="_self">
            <span class="ms-logo">
                <span style="background:#f25022;"></span><span style="background:#7fba00;"></span>
                <span style="background:#00a4ef;"></span><span style="background:#ffb900;"></span>
            </span>
            Sign in with Microsoft
        </a>
    </div>
    """, unsafe_allow_html=True)

    st.markdown('<div class="login-footer">FOR RESEARCH & INFORMATIONAL PURPOSES ONLY &nbsp;·&nbsp; NOT FINANCIAL ADVICE</div>', unsafe_allow_html=True)
    st.stop()

st_autorefresh(interval=300000, key="dashboard_refresh")  # 5 min — matches scheduler

# ============================================================
# SESSION STATE
# ============================================================

if "dark_mode"        not in st.session_state: st.session_state.dark_mode        = True
if "selected_tf"      not in st.session_state: st.session_state.selected_tf      = "1 Hour"
if "selected_strategy"not in st.session_state: st.session_state.selected_strategy= "RSI + MA"  # was "RSI Reversal"
if "chart_symbol"     not in st.session_state: st.session_state.chart_symbol      = None
if "chart_name"       not in st.session_state: st.session_state.chart_name        = None
if "chart_strategy"   not in st.session_state: st.session_state.chart_strategy    = None
if "chart_timeframe"  not in st.session_state: st.session_state.chart_timeframe   = None

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
    --teal:       #14b8a6;  /* 3 Bar Play strategy color — distinct from
                               --green, which already means profit/BUY
                               elsewhere; reusing it for a strategy pill
                               would be confusing */
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
    --teal:       #0d9488;
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

/* Button — refined for a more polished, consistent look across the
   Close/Stop/Chart action buttons (Jwala Jul 17: "make the buttons
   more professional") — subtle shadow, smoother hover/press states,
   slightly larger radius, consistent height so a row of 3 aligns
   cleanly. This is my interpretation of "professional" — say the
   word if you want a different direction (flatter, more colorful, etc). */
.stButton > button {
    background: var(--card) !important; border: 1px solid var(--border2) !important;
    color: var(--t2) !important; font-size: 12px !important; font-weight: 500 !important;
    font-family: 'IBM Plex Sans', sans-serif !important;
    border-radius: 8px !important; min-height: 34px;
    box-shadow: 0 1px 2px rgba(0,0,0,0.06); transition: all 0.15s ease; width: 100%;
}
.stButton > button:hover {
    border-color: var(--blue) !important; color: var(--blue) !important;
    background: var(--card2) !important; box-shadow: 0 2px 6px rgba(74,144,226,0.18);
}
.stButton > button:active { transform: translateY(1px); box-shadow: none; }

/* Primary buttons (Streamlit's type="primary") — used for the
   destructive "Close" action so it reads as intentional/distinct
   from Chart and Stop. */
.stButton > button[kind="primary"] {
    background: var(--red) !important; border: 1px solid var(--red) !important;
    color: #fff !important; font-weight: 600 !important;
}
.stButton > button[kind="primary"]:hover {
    background: #d84343 !important; border-color: #d84343 !important;
    color: #fff !important; box-shadow: 0 2px 6px rgba(240,85,85,0.35);
}

/* Popover trigger ("Kill Switch" and per-row "Stop") — this rule was
   missing background/border/text-color entirely, so it fell back to
   Streamlit's own default (light) button styling regardless of theme.
   Harmless-looking in Light Mode (blends in with the light background
   by coincidence) but a jarring white box in Dark Mode — exactly the
   "buttons not aligned with dark mode" bug. Now explicitly themed with
   the same CSS variables as .stButton > button, so it follows
   whichever mode is active instead of Streamlit's hardcoded default.
   FIXED 2026-08-16: the original selector (`div[data-testid="stPopover"]
   > button`) matched NOTHING — confirmed against the installed
   Streamlit 1.45.1 frontend bundle, the actual trigger element carries
   its own `data-testid="stPopoverButton"` directly (a BaseButton), not
   a bare <button> as a direct child of the stPopover wrapper div. That
   selector mismatch is why this "fix" never actually applied despite
   looking correct in the source. */
[data-testid="stPopoverButton"] {
    background: var(--card) !important; border: 1px solid var(--border2) !important;
    color: var(--t2) !important; font-size: 12px !important; font-weight: 500 !important;
    font-family: 'IBM Plex Sans', sans-serif !important;
    border-radius: 8px !important; min-height: 34px;
    box-shadow: 0 1px 2px rgba(0,0,0,0.06); transition: all 0.15s ease;
}
[data-testid="stPopoverButton"]:hover {
    border-color: var(--blue) !important; color: var(--blue) !important;
    background: var(--card2) !important; box-shadow: 0 2px 6px rgba(74,144,226,0.18);
}
[data-testid="stPopoverButton"] p { color: inherit !important; }

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

/* Dense table layout fix (Om, Jul 22: "if it gets into the small
   screen, the buttons are being disrupted and not being on the
   layout"). Root cause: st.columns() is Streamlit's own layout
   primitive — below Streamlit's internal PER-COLUMN width threshold
   it stacks columns VERTICALLY instead of horizontally, which for an
   11-column Open Positions row (with a further nested 3-column
   Chart/Close/Stop group) turns each table row into a jumbled
   vertical stack / letter-wrapped buttons rather than reflowing
   gracefully like the KPI cards above do.
   FIXED 2026-08-16: that threshold is driven by how many pixels each
   individual column gets once the row is split N ways, NOT by the
   browser's overall viewport width — so gating this behind
   `@media (max-width: 900px)` was wrong. An 11-column row (with a
   nested 3-column group inside its narrowest column) can hit
   Streamlit's per-column stacking threshold on a perfectly wide
   desktop window; confirmed broken live on a normal-width window
   despite the previous <=900px scoping, because the media query
   simply never activated. Applied unconditionally instead: forces
   every column row to stay in a single line and lets the PAGE scroll
   horizontally whenever content is denser than the available width —
   standard practice for dense trading tables (most real trading
   platforms do the same rather than reflowing a data-dense table). */
div[data-testid="stHorizontalBlock"] {
    flex-wrap: nowrap !important;
    min-width: max-content;
}
div[data-testid="stHorizontalBlock"] > div[data-testid="column"] {
    min-width: fit-content;
    flex-shrink: 0;
}
.stButton > button, [data-testid="stPopoverButton"] {
    min-width: 54px !important; white-space: nowrap !important;
    /* white-space:nowrap alone only stops breaks AT spaces — a single
       word like "Close" can still split mid-word ("Clos"/"e") if
       overflow-wrap/word-break elsewhere allows an emergency break
       when the button is narrower than the label. Force the button to
       overflow instead of breaking the word. Needs !important — this
       stylesheet's own convention throughout, and without it Streamlit's
       own button styles keep winning (confirmed: the first attempt at
       this fix silently did nothing because it omitted !important). */
    overflow-wrap: normal !important; word-break: normal !important;
}
.stButton > button p, [data-testid="stPopoverButton"] p,
.stButton > button div, [data-testid="stPopoverButton"] div {
    white-space: nowrap !important; overflow-wrap: normal !important; word-break: normal !important;
}
.main .block-container {
    overflow-x: auto;
    padding-left: 1rem !important;
    padding-right: 1rem !important;
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
    # "RSI Reversal" kept for old trades from before the Jul 24 rename
    # (forward-only — historical rows still say "RSI Reversal", see
    # _CANONICAL_STRATEGY_ORDER below for the same reasoning). Same
    # blue as its successor, since it's literally the same strategy
    # under a new name, not a different one.
    "RSI Reversal":           ("rgba(74,144,226,0.12)",  "rgba(74,144,226,0.35)",  "var(--blue)"),
    "RSI + MA":               ("rgba(74,144,226,0.12)",  "rgba(74,144,226,0.35)",  "var(--blue)"),
    "Volume Spike":           ("rgba(247,168,0,0.12)",   "rgba(247,168,0,0.35)",   "var(--amber)"),
    "3 Bar Play":             ("rgba(20,184,166,0.12)",  "rgba(20,184,166,0.35)", "var(--teal)"),
    # Amber/caution color deliberately distinct from "3 Bar Play"'s teal —
    # visually marks this one as the experimental/unproven pattern, kept
    # running under its old logic but relabeled per client feedback.
    "Experiment 3 Bar Play":  ("rgba(247,168,0,0.10)",   "rgba(247,168,0,0.30)",  "var(--amber)"),
    "Cash-Futures Arbitrage": ("rgba(155,109,255,0.12)", "rgba(155,109,255,0.35)","var(--purple)"),
}
_DEFAULT_STRATEGY_STYLE = ("rgba(74,144,226,0.12)", "rgba(74,144,226,0.35)", "var(--blue)")

# Literal hex fallbacks for the CSS custom properties _STRATEGY_STYLE's fg
# color uses -- needed anywhere a strategy pill has to render standalone
# (e.g. inside build_tv_chart's iframe HTML, which has no access to the
# main page's :root variables).
_STRATEGY_FG_HEX = {
    "var(--blue)":   ("#4a90e2", "#1a6fd4"),
    "var(--amber)":  ("#f7a800", "#c47e00"),
    "var(--teal)":   ("#14b8a6", "#0d9488"),
    "var(--purple)": ("#9b6dff", "#6040cc"),
}

def _strategy_chip_colors(strategy: str, is_dark: bool) -> tuple[str, str, str]:
    bg, border, fg_var = _STRATEGY_STYLE.get(strategy, _DEFAULT_STRATEGY_STYLE)
    dark_hex, light_hex = _STRATEGY_FG_HEX.get(fg_var, _STRATEGY_FG_HEX["var(--blue)"])
    return bg, border, (dark_hex if is_dark else light_hex)


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

# Lightweight Charts always renders an epoch as if it were UTC.
IST_OFFSET = 19800  # +5h30m, in seconds


def _to_chart_epoch(ts) -> int:
    """
    Convert a Datetime value to the epoch Lightweight Charts should be
    given so it displays the correct IST wall-clock time.

    - tz-aware ts: `.timestamp()` gives the true UTC instant. Add the
      IST offset so that once the chart renders it "as UTC", the
      numbers shown are the IST wall-clock time.
    - tz-naive ts: these already hold IST wall-clock numbers (e.g.
      upstox_provider.resample_ohlc() strips tz after converting to
      IST). Pandas' Timestamp.timestamp() treats a naive value as if
      it were already UTC, so the wall-clock numbers survive
      unchanged with NO extra offset — adding one here would double
      the shift (this was the bug: 5m/15m/1h charts rendered ~5.5h
      later than the real candle time).
    """
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        return int(ts.timestamp())
    return int(ts.timestamp()) + IST_OFFSET


def build_tv_chart(
    symbol:   str,
    name:     str,
    tf_name:  str,
    is_dark:  bool,
    signals:  list[dict] = None,
    strategy: str = None,
    strategy_timeframe: str = None,
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
            _bg, _fg = ("#0d1526", "#6b7fa0") if is_dark else ("#ffffff", "#7a8fad")
            return f"<div style='padding:20px;background:{_bg};color:{_fg};font-family:monospace;height:510px;box-sizing:border-box;'>No data available for chart</div>"

        # Prepare candle data
        candles = []
        for _, row in df.iterrows():
            try:
                t = _to_chart_epoch(row["Datetime"])
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
                    t = _to_chart_epoch(row["Datetime"])
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

        # Signal markers (strategy BUY/SELL alerts, and paper-trading
        # entry/exit events passed in via `signals` from show_chart_panel)
        #
        # Lightweight Charts only reliably renders a marker whose "time"
        # exactly equals an existing candle's time — a signal's real
        # timestamp (e.g. 09:52:05) never lands exactly on a bucket
        # boundary (e.g. 09:15:00), so markers appeared to work at wide
        # zoom (where the mismatch happens to fall within rendering
        # tolerance) and silently vanished once zoomed to a range where
        # it doesn't. Snapping each marker to the last candle at-or-before
        # its real time removes that dependency entirely.
        candle_times = [c["time"] for c in candles]

        def _snap_to_candle(epoch: int) -> int:
            if not candle_times:
                return epoch
            idx = bisect.bisect_right(candle_times, epoch) - 1
            return candle_times[max(idx, 0)]

        markers = []
        if signals:
            for sig in signals:
                try:
                    ts_str = sig.get("Timestamp", "")
                    if not ts_str:
                        continue
                    signal_type = sig.get("Signal", "")
                    # Skip HOLD / anything that isn't an actual BUY or SELL —
                    # log_signal() writes a row on every scan (including
                    # HOLD), so without this filter the chart got flooded
                    # with HOLD rows mislabeled as red SELL arrows.
                    if signal_type not in ("BUY", "SELL"):
                        continue
                    t = _snap_to_candle(_to_chart_epoch(pd.to_datetime(ts_str, utc=True)))
                    label = sig.get("Label")
                    text  = f"{label} {signal_type} ₹{sig.get('Price','')}" if label else f"{signal_type} ₹{sig.get('Price','')}"
                    markers.append({
                        "time":     t,
                        "position": "belowBar" if signal_type == "BUY" else "aboveBar",
                        "color":    "#1ec9a0" if signal_type == "BUY" else "#f05555",
                        "shape":    "arrowUp" if signal_type == "BUY" else "arrowDown",
                        "text":     text,
                    })
                except Exception:
                    continue

        # setMarkers() requires the array sorted ascending by time -- our
        # input list is Stock-filtered signals (newest-first, per
        # get_signals' ORDER BY DESC) concatenated with open/closed paper
        # positions (also newest-first), so it's never actually sorted.
        # Lightweight Charts doesn't just misplace out-of-order markers,
        # it silently drops the ENTIRE set once the view is zoomed/panned
        # (confirmed library behavior: github.com/tradingview/
        # lightweight-charts issues #956 and #1766) -- this, not the
        # candle-time mismatch, was the real cause of markers vanishing
        # on zoom even after they were snapped to an exact candle time.
        markers.sort(key=lambda m: m["time"])

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

        strategy_chip_html = ""
        if strategy:
            chip_bg, chip_border, chip_fg = _strategy_chip_colors(strategy, is_dark)
            chip_label = f"{strategy} · {strategy_timeframe}" if strategy_timeframe else strategy
            strategy_chip_html = (
                f"<span style='display:inline-block;background:{chip_bg};border:1px solid {chip_border};"
                f"color:{chip_fg};font-family:JetBrains Mono,monospace;font-size:10px;font-weight:600;"
                f"padding:3px 10px;border-radius:20px;letter-spacing:0.5px;'>{chip_label}</span>"
            )

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
  <div style="display:flex;align-items:center;gap:10px;">
    <div id="chart-title">{name}</div>
    {strategy_chip_html}
  </div>
  <div style="display:flex;align-items:center;gap:10px;">
    <div id="chart-tf" style="font-size:11px;color:{text};font-family:'JetBrains Mono',monospace;font-weight:600;">{tf_name}</div>
    <div style="font-size:10px;color:{text};font-family:'JetBrains Mono',monospace;opacity:0.6;">TradingView Lightweight Charts™</div>
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
    color:           level === 75 ? 'rgba(240,85,85,0.4)' : 'rgba(30,201,160,0.4)',
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
        _bg = "#0d1526" if is_dark else "#ffffff"
        return f"<div style='padding:20px;background:{_bg};color:#f05555;font-family:monospace;height:510px;box-sizing:border-box;'>Chart error: {e}</div>"


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
        f"Logged in as <b>{st.session_state.get('user', {}).get('name', '')}</b></div>",
        unsafe_allow_html=True,
    )
    # Full sign-out: also ends the browser's Entra SSO session (via
    # /oauth2/v2.0/logout) so the next Sign-in-with-Microsoft shows the
    # account picker instead of silently re-authenticating the same user.
    _logout_url = (
        f"https://login.microsoftonline.com/{ENTRA_TENANT_ID}/oauth2/v2.0/logout"
        f"?post_logout_redirect_uri={urllib.parse.quote(ENTRA_REDIRECT_URI, safe='')}"
    )
    st.markdown(f"""
    <style>
    .entra-logout-btn {{
        display: block; width: 100%; text-align: center; text-decoration: none !important;
        background: var(--card); border: 1px solid var(--border2); color: var(--t2) !important;
        font-size: 12px; font-weight: 500; font-family: 'IBM Plex Sans', sans-serif;
        border-radius: 8px; padding: 8px; box-shadow: 0 1px 2px rgba(0,0,0,0.06);
        box-sizing: border-box; transition: all 0.15s ease;
    }}
    .entra-logout-btn:hover {{ border-color: var(--blue); color: var(--blue) !important; }}
    </style>
    <a href="{_logout_url}" class="entra-logout-btn" target="_self">Logout</a>
    """, unsafe_allow_html=True)
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

    # Paper-trading entry/exit markers for this symbol. These are NOT
    # in the `signals` table above — a stop-loss/target/manual/kill-switch
    # exit is a paper_positions event, not a strategy-generated signal,
    # so it never gets written by log_signal(). Fetched directly (not via
    # `all_logs`) so they also aren't hidden by the sidebar's
    # strategy/timeframe filter or the signals log's 7-day window.
    try:
        from core.database.db import get_open_paper_positions, get_closed_paper_positions
        open_pos = get_open_paper_positions(symbol=sym)
        if open_pos is not None and not open_pos.empty:
            for _, p in open_pos.iterrows():
                sym_signals.append({
                    "Timestamp": p["opened_at"],
                    "Signal":    p["side"],
                    "Price":     p["entry_price"],
                    "Label":     "ENTRY",
                })
        closed_pos = get_closed_paper_positions(days=365)
        if closed_pos is not None and not closed_pos.empty and "symbol" in closed_pos.columns:
            closed_pos = closed_pos[closed_pos["symbol"] == sym]
            for _, p in closed_pos.iterrows():
                sym_signals.append({
                    "Timestamp": p["opened_at"],
                    "Signal":    p["side"],
                    "Price":     p["entry_price"],
                    "Label":     "ENTRY",
                })
                sym_signals.append({
                    "Timestamp": p["closed_at"],
                    "Signal":    "SELL" if p["side"] == "BUY" else "BUY",
                    "Price":     p["exit_price"],
                    "Label":     f"EXIT {str(p.get('exit_reason', '')).upper()}".strip(),
                })
    except Exception:
        pass

    chart_html = build_tv_chart(
        symbol=sym,
        name=name,
        tf_name=selected_tf,
        is_dark=st.session_state.dark_mode,
        signals=sym_signals,
        strategy=st.session_state.chart_strategy,
        strategy_timeframe=st.session_state.chart_timeframe,
    )

    # Close button
    close_col, _ = st.columns([1, 5])
    with close_col:
        if st.button("✕ Close Chart", key="close_chart"):
            st.session_state.chart_symbol    = None
            st.session_state.chart_name      = None
            st.session_state.chart_strategy  = None
            st.session_state.chart_timeframe = None
            st.rerun()

    st.components.v1.html(chart_html, height=510, scrolling=False)
    st.markdown("</div>", unsafe_allow_html=True)


show_chart_panel()


# ============================================================
# TABS — Live Trading / Paper Trading / Signals (Jwala, Jul 14:
# "consider 3 pages... one goes for the live trading, one goes for
# the paper trading, one goes for the live signals being generated" —
# built as st.tabs() inside this one file, not separate Streamlit
# pages/URLs, so the login gate and session state stay exactly as
# they are — no risk of auth breaking across pages).
#
# KPI bar and chart panel stay page-level chrome (always visible,
# above the tabs) — not moved into a specific tab, to keep this
# restructure minimal and lower-risk. Say the word if you'd rather
# have the KPI bar live inside the Signals tab specifically instead.
# ============================================================

tab_live, tab_paper, tab_signals, tab_reports = st.tabs(
    ["🔴 Live Trading", "📊 Paper Trading", "📡 Signals", "📑 Reports"]
)

with tab_live:
    st.info(
        "Live trading isn't implemented yet — this tab is reserved for "
        "when real broker execution goes live (Phase 4). Until then, "
        "use the Paper Trading tab to track simulated performance."
    )


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

def render_section(rows, title, dot_color="var(--blue)", scroll_height=None):
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
                    st.session_state.chart_symbol   = None
                    st.session_state.chart_name     = None
                    st.session_state.chart_strategy = None
                    st.session_state.chart_timeframe = None
                else:
                    st.session_state.chart_symbol   = row["sym"]
                    st.session_state.chart_name     = row["name"]
                    # This table is already scoped to the sidebar's own
                    # strategy/timeframe filter, so that's what this row
                    # represents -- "All Strategies" has no single pill.
                    st.session_state.chart_strategy = selected_strategy if selected_strategy != "All Strategies" else None
                    st.session_state.chart_timeframe = selected_tf
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


# ── Per-strategy table split (Jwala Jul 14: "Can we have segregation
# in terms of strategy? So RSI reversal, one strategy or block of
# RSI, then volumes like another block... strategy wise, I have a
# segregation just by looking at it") — separate tables, not a
# dropdown filter. Canonical order first, then anything else found in
# the data (e.g. if Arbitrage joins paper trading later) so nothing
# silently disappears if a new strategy name shows up. ──
# "RSI Reversal" kept alongside "RSI + MA" (Jul 24 rename was forward-
# only — old closed trades still say "RSI Reversal" and need their own
# ordered table section, not to fall into the dynamic catch-all).
_CANONICAL_STRATEGY_ORDER = ["RSI Reversal", "RSI + MA", "Volume Spike", "3 Bar Play", "Experiment 3 Bar Play"]

def _ordered_strategies_present(df) -> list:
    if df is None or df.empty or "strategy" not in df.columns:
        return []
    present = set(df["strategy"].unique())
    ordered = [s for s in _CANONICAL_STRATEGY_ORDER if s in present]
    ordered += sorted(present - set(_CANONICAL_STRATEGY_ORDER))
    return ordered


def _render_open_positions_table(df, cmp_map, key_prefix: str):
    """One Open Positions table for a (pre-filtered, single-strategy)
    slice of open_df. key_prefix keeps widget keys unique across the
    RSI/Volume Spike tables (position id alone is already globally
    unique, so this is just cheap insurance)."""
    if df is None or df.empty:
        st.markdown('<div class="no-sig">No open positions</div>', unsafe_allow_html=True)
        return

    widths = [1.5, 0.55, 0.55, 0.85, 1.0, 1.0, 0.85, 0.85, 0.95, 0.8, 1.5]
    h = st.columns(widths)
    for col, lbl in zip(h, ["Stock", "Side", "Qty", "Entry", "CMP", "Unreal. P&L",
                             "Stop", "Target", "Capital", "Opened", "Actions"]):
        col.markdown(f'<div class="col-hdr">{lbl}</div>', unsafe_allow_html=True)

    for _, r in df.iterrows():
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
            try:
                opened = pd.to_datetime(r["opened_at"], utc=True).tz_convert(IST).strftime("%d-%b %H:%M")
            except Exception:
                opened = str(r.get("opened_at", ""))[:16]
            st.markdown(f"<div style='padding:9px 0;font-size:11px;color:var(--t3);font-family:JetBrains Mono,monospace;'>{opened}</div>", unsafe_allow_html=True)

        with c[10]:
            chcol, bcol, scol = st.columns(3)
            with chcol:
                if st.button("📈", key=f"{key_prefix}_chart_{pid}", help=f"View chart for {stock_display(sym)}"):
                    if st.session_state.chart_symbol == sym:
                        st.session_state.chart_symbol    = None
                        st.session_state.chart_name      = None
                        st.session_state.chart_strategy  = None
                        st.session_state.chart_timeframe = None
                    else:
                        st.session_state.chart_symbol    = sym
                        st.session_state.chart_name      = stock_display(sym)
                        st.session_state.chart_strategy  = r.get("strategy")
                        st.session_state.chart_timeframe = r.get("timeframe")
                    st.rerun()
            with bcol:
                if st.button("Close", key=f"{key_prefix}_close_{pid}", help=f"Book P&L now for {stock_display(sym)}", type="primary"):
                    exit_px = cmp if cmp is not None else entry
                    if close_paper_position(pid, exit_px, exit_reason="manual"):
                        st.rerun()
                    else:
                        st.error("Close failed")
            with scol:
                with st.popover("Stop"):
                    new_stop = st.number_input(
                        "New stop", value=stop, step=0.05, format="%.2f",
                        key=f"{key_prefix}_stop_input_{pid}",
                    )
                    if st.button("Update", key=f"{key_prefix}_stop_btn_{pid}"):
                        if update_paper_position_stop(pid, new_stop):
                            st.rerun()
                        else:
                            st.error("Update failed")

        st.markdown("<div class='row-div'></div>", unsafe_allow_html=True)


def _render_closed_trades_table(df, key_prefix: str):
    """One paginated Closed Trades table for a (pre-filtered,
    single-strategy) slice of closed_df. Each strategy's table gets
    its OWN pagination state (key_prefix-scoped session_state key),
    so paging through RSI trades doesn't affect Volume Spike's page."""
    if df is None or df.empty:
        st.markdown('<div class="no-sig">No closed trades yet</div>', unsafe_allow_html=True)
        return

    PAGE = 15
    total = len(df)
    pages = max(1, (total + PAGE - 1) // PAGE)
    page_key = f"{key_prefix}_closed_page"
    if page_key not in st.session_state:
        st.session_state[page_key] = 0
    st.session_state[page_key] = max(0, min(st.session_state[page_key], pages - 1))
    pg = st.session_state[page_key]

    page_df = df.iloc[pg*PAGE : (pg+1)*PAGE]

    ct_widths = [1.2, 0.5, 0.5, 1.0, 1.0, 0.85, 0.85, 0.85, 0.7, 0.55]
    ct_h = st.columns(ct_widths)
    for col, lbl in zip(ct_h, ["Stock", "Side", "Qty", "Entry", "Exit", "Gross P&L", "Net P&L",
                                 "Exit Reason", "Duration", "Chart"]):
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
            st.markdown(f"<div style='padding:9px 0;font-size:11px;color:var(--t3);font-family:JetBrains Mono,monospace;'>{duration}</div>", unsafe_allow_html=True)
        with cc[9]:
            _ct_key = f"{key_prefix}_closed_chart_{int(r['id'])}"
            if st.button("📈", key=_ct_key, help=f"View chart for {stock_display(ct_sym)}"):
                if st.session_state.chart_symbol == ct_sym:
                    st.session_state.chart_symbol    = None
                    st.session_state.chart_name      = None
                    st.session_state.chart_strategy  = None
                    st.session_state.chart_timeframe = None
                else:
                    st.session_state.chart_symbol    = ct_sym
                    st.session_state.chart_name      = stock_display(ct_sym)
                    st.session_state.chart_strategy  = r.get("strategy")
                    st.session_state.chart_timeframe = r.get("timeframe")
                st.rerun()

        st.markdown("<div class='row-div'></div>", unsafe_allow_html=True)

    if pages > 1:
        nav1, nav2, nav3 = st.columns([1, 2, 1])
        with nav1:
            if st.button("← Prev", key=f"{key_prefix}_prev", disabled=(pg <= 0)):
                st.session_state[page_key] = max(0, pg - 1)
                st.rerun()
        with nav2:
            st.markdown(
                f"<div style='text-align:center;font-size:11px;color:var(--t3);"
                f"font-family:JetBrains Mono,monospace;padding-top:8px;'>"
                f"Page {pg + 1} of {pages} &nbsp;·&nbsp; {total} closed trades</div>",
                unsafe_allow_html=True,
            )
        with nav3:
            if st.button("Next →", key=f"{key_prefix}_next", disabled=(pg >= pages - 1)):
                st.session_state[page_key] = min(pages - 1, pg + 1)
                st.rerun()


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
        <span class="sec-meta">Upstox Sandbox &nbsp;·&nbsp; RSI + MA + Volume Spike + 3 Bar Play + Experiment 3 Bar Play &nbsp;·&nbsp; Long + Short</span>
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

    # ── OPEN POSITIONS — split into one table per strategy (Jwala
    # Jul 14: "Can we have segregation in terms of strategy?... RSI
    # reversal, one strategy or block... volumes like another block")
    # — separate tables, not a filter dropdown.
    oph_col, kill_col = st.columns([5, 1.3])
    with oph_col:
        st.markdown('<div style="font-size:12px;font-weight:700;color:var(--t2);text-transform:uppercase;letter-spacing:2px;margin:10px 0 8px;">Open Positions — Unrealized P&L</div>', unsafe_allow_html=True)
    with kill_col:
        # ── Kill Switch (Jwala Jul 11, reconfirmed 16:21/36:07):
        # close EVERY open position immediately, across ALL
        # strategies, not one at a time. Behind a popover confirm —
        # this is destructive and portfolio-wide.
        with st.popover("🔴 Kill Switch", use_container_width=True):
            n_open = 0 if open_df is None else len(open_df)
            st.markdown(f"**Close all {n_open} open position(s) now?**")
            st.caption("Each closes at its current market price (or entry price if a live quote isn't available). This can't be undone.")
            if n_open > 0 and st.button("Yes, close everything", key="pt_kill_switch_confirm", use_container_width=True, type="primary"):
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

    _open_strategies = _ordered_strategies_present(open_df)
    if not _open_strategies:
        st.markdown('<div class="no-sig">No open positions</div>', unsafe_allow_html=True)
    else:
        for _strat in _open_strategies:
            _strat_df       = open_df[open_df["strategy"] == _strat]
            _strat_deployed = get_capital_deployed(strategy=_strat)
            st.markdown(
                f'<div style="font-size:11px;font-weight:700;color:var(--t3);text-transform:uppercase;letter-spacing:1.5px;margin:14px 0 6px;">'
                f'{_strat} &nbsp;·&nbsp; {len(_strat_df)}/{RMSConfig.MAX_OPEN_POSITIONS_PER_STRATEGY} open'
                f' &nbsp;·&nbsp; ₹{_strat_deployed:,.0f} / ₹{RMSConfig.CAPITAL_PER_STRATEGY:,.0f} deployed</div>',
                unsafe_allow_html=True,
            )
            _render_open_positions_table(_strat_df, cmp_map, key_prefix=f"pt_open_{_strat.replace(' ', '_').lower()}")

    st.markdown("<div style='margin-bottom:10px;'></div>", unsafe_allow_html=True)

    # ── CLOSED TRADES — split into one table per strategy, same
    # reasoning as Open Positions above (Jwala Jul 14). Each gets its
    # OWN pagination (see _render_closed_trades_table's key_prefix).
    st.markdown('<div style="font-size:12px;font-weight:700;color:var(--t2);text-transform:uppercase;letter-spacing:2px;margin:22px 0 8px;">Closed Trades — Realized P&L (Today)</div>', unsafe_allow_html=True)
    closed_df = get_today_closed_paper_positions()  # today only — see summary fetch above

    _closed_strategies = _ordered_strategies_present(closed_df)
    if not _closed_strategies:
        st.markdown('<div class="no-sig">No closed trades yet</div>', unsafe_allow_html=True)
    else:
        for _strat in _closed_strategies:
            _strat_cdf = closed_df[closed_df["strategy"] == _strat]
            st.markdown(
                f'<div style="font-size:11px;font-weight:700;color:var(--t3);text-transform:uppercase;letter-spacing:1.5px;margin:14px 0 6px;">{_strat} &nbsp;·&nbsp; {len(_strat_cdf)} closed today</div>',
                unsafe_allow_html=True,
            )
            _render_closed_trades_table(_strat_cdf, key_prefix=f"pt_closed_{_strat.replace(' ', '_').lower()}")


with tab_paper:
    render_paper_trading()


with tab_signals:
    # ============================================================
    # RENDER ALL SECTIONS
    # ============================================================

    if show_idx: render_section(idx_rows, "INDEXES",                    "var(--purple)")
    if show_stk: render_section(stk_rows, "NSE STOCKS — F&O WATCHLIST", "var(--blue)", scroll_height=560)
    if show_com: render_section(com_rows, "COMMODITIES — MCX",          "var(--amber)")

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
# REPORTS TAB — daily/weekly/monthly/yearly Excel reports for Paper
# Trading and Signal History, generated on demand (no job/scheduler —
# a single filtered SELECT + in-memory openpyxl build is sub-second
# at this app's trade volume, see core/reporting/'s own design notes).
# Chosen as its own tab (rather than embedding controls inside the
# already-dense Paper Trading/Signals tabs, or a sidebar section that
# would compete with the existing strategy/timeframe/search filters)
# so both report types share one consolidated set of controls.
# ============================================================

def render_reports():
    from core.reporting.paper_trading_report import build_paper_trading_report
    from core.reporting.signal_history_report import build_signal_history_report

    st.markdown("""
    <div class="sec-hdr" style='margin-top:6px;'>
        <div style='width:7px;height:7px;border-radius:50%;background:var(--teal);flex-shrink:0;'></div>
        <span class="sec-title">Reports</span>
        <span class="sec-meta">Daily / Weekly / Monthly / Yearly &nbsp;·&nbsp; Downloadable Excel</span>
    </div>
    """, unsafe_allow_html=True)

    r1, r2, r3, r4 = st.columns([1.3, 1, 1, 1])
    with r1:
        report_type = st.radio("Report Type", ["Paper Trading", "Signal History"], horizontal=True, key="rpt_type")
    with r2:
        period_label = st.selectbox("Period", ["Daily", "Weekly", "Monthly", "Yearly", "Custom"], key="rpt_period")
    with r3:
        # "Cash-Futures Arbitrage" never has paper_positions rows -- it's a
        # paired spot+futures trade, scanned via a separate path
        # (run_arbitrage_scan) that never calls _run_paper_trading(). Only
        # offer it for Signal History, where it genuinely has data; showing
        # it as a Paper Trading filter option is a guaranteed 0-trade
        # dead end that reads as "report generation failed" (2026-08-25 audit).
        strategy_options = ALL_STRATEGY_NAMES if report_type == "Signal History" \
            else [s for s in ALL_STRATEGY_NAMES if s != "Cash-Futures Arbitrage"]
        strategy_choice = st.selectbox("Strategy", strategy_options, key="rpt_strategy")
    with r4:
        timeframe_choice = st.selectbox("Timeframe", ["All Timeframes"] + list(TIMEFRAMES.keys()), key="rpt_timeframe")

    custom_start = custom_end = None
    if period_label == "Custom":
        d1, d2 = st.columns(2)
        with d1:
            custom_start = st.date_input("Start date", key="rpt_custom_start")
        with d2:
            custom_end = st.date_input("End date", key="rpt_custom_end")

    strategy_filter  = None if strategy_choice == "All Strategies" else strategy_choice
    timeframe_filter = None if timeframe_choice == "All Timeframes" else timeframe_choice
    period_key = period_label.lower()

    # Signature of everything that would change the generated file --
    # used below to detect "filters changed since last Generate click"
    # (found 2026-08-25 audit: changing a filter without re-clicking
    # Generate silently kept serving the PREVIOUS selection's file via
    # Download, with no indication it was stale).
    current_filters = (report_type, period_key, strategy_filter, timeframe_filter,
                       str(custom_start), str(custom_end))

    if st.button("Generate Report", type="primary", key="rpt_generate"):
        try:
            builder = build_paper_trading_report if report_type == "Paper Trading" else build_signal_history_report
            file_bytes, filename, stats_caption = builder(
                period_key, custom_start=custom_start, custom_end=custom_end,
                strategy=strategy_filter, timeframe=timeframe_filter,
            )
            st.session_state["rpt_file_bytes"] = file_bytes
            st.session_state["rpt_filename"]   = filename
            st.session_state["rpt_caption"]    = stats_caption
            st.session_state["rpt_filters"]    = current_filters
        except ValueError as e:
            st.warning(str(e))
            st.session_state.pop("rpt_file_bytes", None)
        except Exception as e:
            st.error(f"Report generation failed: {e}")
            st.session_state.pop("rpt_file_bytes", None)

    if st.session_state.get("rpt_file_bytes"):
        if st.session_state.get("rpt_filters") != current_filters:
            st.info("Filters changed since this report was generated — click **Generate Report** to refresh it.")
        else:
            st.caption(st.session_state["rpt_caption"])
            st.download_button(
                "⬇ Download Excel Report",
                data=st.session_state["rpt_file_bytes"],
                file_name=st.session_state["rpt_filename"],
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="rpt_download",
            )


with tab_reports:
    render_reports()


# ============================================================
# FOOTER
# ============================================================

st.markdown("""
<div style='border-top:1px solid var(--border);margin-top:40px;padding-top:16px;font-size:11px;color:var(--t4);text-align:center;font-family:JetBrains Mono,monospace;letter-spacing:1px;'>
    FOR RESEARCH & INFORMATIONAL PURPOSES ONLY &nbsp;·&nbsp; NOT FINANCIAL ADVICE &nbsp;·&nbsp; TRADE AT YOUR OWN RISK
    <br><span style='font-size:10px;opacity:0.5;'>Charts powered by TradingView Lightweight Charts™ (Apache 2.0)</span>
</div>
""", unsafe_allow_html=True)