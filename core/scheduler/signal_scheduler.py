# ============================================================
# core/scheduler/signal_scheduler.py
#
# Market hours: 9:15 AM — 3:30 PM IST  Mon—Fri
# All instruments scanned together: Indexes + Stocks + Commodities
#
# Strategy selection:
#   Set SIGNAL_STRATEGY env variable to choose strategy.
#   Default: "RSI Reversal"
#
# FIXES (2026-06-19):
#   - Arbitrage now runs HOURLY only (not every 5 mins)
#   - Reduces Upstox API calls from 2000+/day to ~1000/day
#   - Prevents 429 rate limiting on futures search API
#   - Jobs complete cleanly without hanging
# ============================================================

import os
import sys
import time
import logging
import calendar
from datetime import datetime, date

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

load_dotenv()

sys.path.append(
    os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
)

from data.providers.upstox_provider import UpstoxProvider
from core.engine.strategy_engine import StrategyEngine
from core.strategies.strategies import STRATEGY_NAMES
from configs.universe import get_all_instruments_extended
from configs.timeframes import TIMEFRAMES, PERIOD_MAP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("scheduler")

IST = pytz.timezone("Asia/Kolkata")

# ============================================================
# STRATEGY SELECTION
# ============================================================

_DEFAULT_STRATEGY = "RSI Reversal"
_ACTIVE_STRATEGY  = os.getenv("SIGNAL_STRATEGY", _DEFAULT_STRATEGY)

ALL_STRATEGY_NAMES = STRATEGY_NAMES + ["Cash-Futures Arbitrage"]

if _ACTIVE_STRATEGY not in ALL_STRATEGY_NAMES:
    log.warning(
        f"Unknown strategy '{_ACTIVE_STRATEGY}'. "
        f"Falling back to '{_DEFAULT_STRATEGY}'."
    )
    _ACTIVE_STRATEGY = _DEFAULT_STRATEGY

# ============================================================
# NSE HOLIDAYS 2025—2027
# ============================================================

# 2026 dates verified against the OFFICIAL NSE circular
# (NSE/CMTR/71775, dated 12-Dec-2025, https://nsearchives.nseindia.com/
# content/circulars/CMTR71775.pdf) on 17-Jul-2026 — Jul 17 itself was
# the trigger: it was wrongly in this set (not an actual holiday),
# which caused the scheduler to skip a real trading day. Checking
# every other 2026 date against the same circular found the previous
# list was wrong on 9 of its 17 entries (wrong dates, a missing
# holiday — Bakri Id — and one date, Oct 21, that isn't a holiday at
# all). Rebuilt from the source rather than patching the one date.
#
# 2025 (already past) and 2027 (no official NSE circular published
# yet as of this fix — NSE typically issues it in December of the
# prior year) are UNVERIFIED — left as they were. Re-check 2027
# against NSE's circular once it's published, before relying on it.
NSE_HOLIDAYS = {
    date(2025, 1, 26), date(2025, 2, 26), date(2025, 3, 14),
    date(2025, 3, 31), date(2025, 4, 14), date(2025, 4, 18),
    date(2025, 5, 1),  date(2025, 8, 15), date(2025, 8, 27),
    date(2025, 10, 2), date(2025, 10, 20),date(2025, 10, 21),
    date(2025, 11, 5), date(2025, 12, 25),

    date(2026, 1, 26),  # Republic Day
    date(2026, 3, 3),   # Holi
    date(2026, 3, 26),  # Shri Ram Navami
    date(2026, 3, 31),  # Shri Mahavir Jayanti
    date(2026, 4, 3),   # Good Friday
    date(2026, 4, 14),  # Dr. Baba Saheb Ambedkar Jayanti
    date(2026, 5, 1),   # Maharashtra Day
    date(2026, 5, 28),  # Bakri Id
    date(2026, 6, 26),  # Muharram
    date(2026, 9, 14),  # Ganesh Chaturthi
    date(2026, 10, 2),  # Mahatma Gandhi Jayanti
    date(2026, 10, 20), # Dussehra
    date(2026, 11, 10), # Diwali-Balipratipada
    date(2026, 11, 24), # Prakash Gurpurb Sri Guru Nanak Dev
    date(2026, 12, 25), # Christmas
    # Aug 15, 2026 (Independence Day) falls on a Saturday per the
    # circular's own weekend list — already skipped by the weekday
    # check, no separate entry needed.

    date(2027, 1, 26), date(2027, 2, 17), date(2027, 3, 10),
    date(2027, 3, 19), date(2027, 3, 26), date(2027, 4, 2),
    date(2027, 4, 14), date(2027, 4, 30), date(2027, 8, 15),
    date(2027, 8, 16), date(2027, 10, 2), date(2027, 10, 8),
    date(2027, 10, 29),date(2027, 11, 16),date(2027, 12, 25),
}

# ============================================================
# MARKET HOURS CHECKS
# ============================================================

def is_market_day() -> bool:
    today = datetime.now(IST).date()
    return today.weekday() < 5 and today not in NSE_HOLIDAYS


def is_market_hours() -> bool:
    """
    Algo trading window: 9:45 AM - 3:15 PM IST (Jwala, Jul 17 —
    was 9:15-15:30, the full exchange session). Narrower than the
    real exchange hours: skips the first 30 min ("these signals
    should work after market has stabilised a little... probably
    after half an hour") and ends 15 min before exchange close,
    matching the new square-off time so nothing opened in the last
    minute of the window is left without a same-day exit chance.

    This governs SCANNING and NEW ENTRIES only. It does not change
    the real exchange session — dashboard.py's own market_open()
    (the "MARKET OPEN/CLOSED" badge) intentionally still reflects the
    true 9:15-15:30 exchange hours, since that's general market
    awareness, not "is the algo currently trading."
    """
    if not is_market_day():
        return False
    from datetime import time as dtime
    t = datetime.now(IST).time()
    return dtime(9, 45) <= t <= dtime(15, 15)


def is_equity_hours() -> bool:
    return is_market_hours()


def is_commodity_hours() -> bool:
    return is_market_hours()


def is_last_trading_day_of_month() -> bool:
    today    = datetime.now(IST).date()
    last_day = calendar.monthrange(today.year, today.month)[1]
    for day in range(today.day + 1, last_day + 1):
        candidate = date(today.year, today.month, day)
        if candidate.weekday() < 5 and candidate not in NSE_HOLIDAYS:
            return False
    return True


# ============================================================
# ENGINES & INSTRUMENTS
# ============================================================

provider         = UpstoxProvider()
instruments      = get_all_instruments_extended()
fno_instruments  = [i for i in instruments if i["category"] == "STOCK"]

# Arbitrage engine — always fixed
_arb_engine = StrategyEngine("Cash-Futures Arbitrage")

# ── Parallel strategies that run on EVERY scan ───────────────
# RSI Reversal + Volume Spike both run on the same single fetch
# per instrument (Volume Spike is stock-only, enforced in engine).
# Add more names here to run them in parallel too.
PARALLEL_STRATEGIES = ["RSI Reversal", "Volume Spike"]

# A single engine drives the multi-strategy scan. The label passed
# here is cosmetic — run_multi_scan() takes the real strategy list.
_multi_engine = StrategyEngine("RSI Reversal")

# ── Paper trading: one shared monitor instance ───────────────
# Opening positions is handled inside the engine (run_multi_scan);
# here we only MONITOR open positions once per scan cycle to close
# them on stop-loss / target. Lazy + guarded so it can never break
# a scan. Shares the same provider the scheduler already built.
_paper_monitor = None

def _get_paper_monitor():
    global _paper_monitor
    if _paper_monitor is None:
        try:
            from core.execution.paper_trader import PaperTrader
            _paper_monitor = PaperTrader(provider=provider)
        except Exception as e:
            log.warning(f"PaperTrader monitor unavailable — disabled: {e}")
            _paper_monitor = False
    return _paper_monitor or None

# Primary strategy engine — recreated when strategy changes
# (kept for backward compatibility / single-strategy callers)
_current_strategy  = None
_primary_engine    = None


def get_primary_engine() -> StrategyEngine:
    """
    Returns engine for currently selected strategy.
    Reads from PostgreSQL app_config on every scan —
    dashboard can change strategy without restart.

    NOTE: The production scan path now uses run_multi_scan (parallel
    strategies). This single-strategy engine is retained for any
    callers/tools that still request one specific strategy.
    """
    global _primary_engine, _current_strategy

    try:
        from core.database.db import get_config
        strategy = get_config("SIGNAL_STRATEGY") or os.getenv("SIGNAL_STRATEGY", "RSI Reversal")
    except Exception:
        strategy = os.getenv("SIGNAL_STRATEGY", "RSI Reversal")

    if strategy == "All Strategies":
        strategy = "RSI Reversal"

    if strategy != _current_strategy:
        log.info(f"Strategy: {_current_strategy} → {strategy}")
        _primary_engine   = StrategyEngine(strategy)
        _current_strategy = strategy

    return _primary_engine


# ============================================================
# SCAN FUNCTIONS
# ============================================================

def run_primary_scan(tf_name: str) -> None:
    """
    Parallel multi-strategy scan on all instruments.

    Runs every strategy in PARALLEL_STRATEGIES (RSI Reversal +
    Volume Spike) on a SINGLE data fetch per instrument. Each signal
    is logged and alerted tagged with its own strategy name, so the
    dashboard 'All Strategies' view and per-strategy filter both work.

    The dashboard strategy dropdown now only changes the VIEW (filter)
    — it no longer switches which strategies are scanned. All parallel
    strategies always run.
    """
    interval = TIMEFRAMES[tf_name]
    period   = PERIOD_MAP[tf_name]

    if tf_name == "1 Week" and datetime.now(IST).weekday() != 4:
        return
    if tf_name == "1 Month" and not is_last_trading_day_of_month():
        return
    if not is_market_hours():
        return

    _multi_engine.run_multi_scan(
        provider=provider,
        strategy_names=PARALLEL_STRATEGIES,
        tf_name=tf_name,
        interval=interval,
        period=period,
        instruments=instruments,
    )


def run_arbitrage_scan(tf_name: str) -> None:
    """
    Arbitrage scan on F&O stocks only.

    Cadence: every 30 minutes (per Jwala — arbitrage spread moves
    slowly, and the futures-search API is the rate-limit-sensitive one).

    Implementation: only runs on the "15 Minutes" timeframe pass, and
    only when the current IST minute is at the top/bottom of the hour
    (minute < 5 or 30–34). With the Container Job firing every 5 min,
    this yields ~2 arbitrage scans/hour instead of 4 (was 15-min).

    Futures contracts are cached in PostgreSQL (fetched once/day), so
    only the futures PRICE is fetched each run — keeps API calls low.
    """
    # Only piggyback on the 15-minute pass (avoids running on every tf)
    if tf_name != "15 Minutes":
        return

    # 30-minute cadence: fire near :00 and :30 only
    minute = datetime.now(IST).minute
    if not (minute < 5 or 30 <= minute <= 34):
        return

    if not is_market_hours():
        return

    from data.providers.upstox_provider import get_token
    if not get_token():
        log.info("Arbitrage scan skipped — no valid Upstox token")
        return

    interval = TIMEFRAMES[tf_name]
    period   = PERIOD_MAP[tf_name]

    log.info(f"Running arbitrage scan (30-min cadence) — {len(fno_instruments)} F&O stocks")

    _arb_engine.run_scan(
        provider=provider,
        tf_name=tf_name,
        interval=interval,
        period=period,
        instruments=fno_instruments,
    )


def run_scan(tf_name: str, mode: str = "all") -> None:
    """
    Runs primary strategy then Arbitrage (hourly only).
    Updates LAST_SCAN_TIME after every scan.
    """
    run_primary_scan(tf_name)
    run_arbitrage_scan(tf_name)  # Only runs on 15 Minutes — no-op for other timeframes

    # Paper trading: close any open positions that hit stop/target.
    # Once per scan cycle, single-threaded, fully guarded.
    try:
        pm = _get_paper_monitor()
        if pm is not None:
            closed = pm.monitor_open()
            for c in (closed or []):
                log.info(f"PAPER CLOSE  {c['symbol']}  {c['reason']}  "
                         f"exit={c['exit']}  pnl={c['pnl']}")
    except Exception as e:
        log.warning(f"paper monitor error (non-fatal): {e}")

    try:
        from core.database.db import set_config
        set_config("LAST_SCAN_TIME", datetime.now(IST).isoformat())
    except Exception:
        pass


# ============================================================
# SCHEDULER
# ============================================================

def build_scheduler() -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone=IST)

    scheduler.add_job(
        lambda: run_scan("5 Minutes", "all"),
        CronTrigger(
            minute="1,6,11,16,21,26,31,36,41,46,51,56",
            hour="9,10,11,12,13,14,15",
            day_of_week="mon-fri", timezone=IST,
        ),
        id="scan_5min", name="5 Minute Scan",
        max_instances=1, coalesce=True,
    )

    scheduler.add_job(
        lambda: run_scan("15 Minutes", "all"),
        CronTrigger(
            minute="1,16,31,46",
            hour="9,10,11,12,13,14,15",
            day_of_week="mon-fri", timezone=IST,
        ),
        id="scan_15min", name="15 Minute Scan",
        max_instances=1, coalesce=True,
    )

    scheduler.add_job(
        lambda: run_scan("1 Hour", "all"),
        CronTrigger(
            minute="16", hour="10,11,12,13,14,15",
            day_of_week="mon-fri", timezone=IST,
        ),
        id="scan_1hour", name="1 Hour Scan",
        max_instances=1, coalesce=True,
    )

    scheduler.add_job(
        lambda: run_scan("1 Day", "all"),
        CronTrigger(
            hour="15", minute="31",
            day_of_week="mon-fri", timezone=IST,
        ),
        id="scan_1day", name="1 Day Scan",
        max_instances=1, coalesce=True,
    )

    scheduler.add_job(
        lambda: run_scan("1 Week", "all"),
        CronTrigger(
            hour="15", minute="32",
            day_of_week="fri", timezone=IST,
        ),
        id="scan_1week", name="1 Week Scan",
        max_instances=1, coalesce=True,
    )

    scheduler.add_job(
        lambda: run_scan("1 Month", "all"),
        CronTrigger(
            hour="15", minute="33",
            day_of_week="mon-fri", timezone=IST,
        ),
        id="scan_1month", name="1 Month Scan",
        max_instances=1, coalesce=True,
    )

    return scheduler


def start() -> None:
    try:
        from core.database.db import get_config
        active_strat = get_config("SIGNAL_STRATEGY") or os.getenv("SIGNAL_STRATEGY", "RSI Reversal")
    except Exception:
        active_strat = os.getenv("SIGNAL_STRATEGY", "RSI Reversal")

    log.info("=" * 60)
    log.info("Algo Trading Signal Scheduler")
    log.info(f"Instruments : {len(instruments)} total | {len(fno_instruments)} F&O")
    log.info(f"Timeframes  : {list(TIMEFRAMES.keys())}")
    log.info(f"Strategies  : {PARALLEL_STRATEGIES} (parallel, every scan)")
    log.info(f"Arbitrage   : Every 30 mins (F&O stocks only)")
    log.info(f"Dashboard   : strategy dropdown filters the VIEW only")
    log.info("Data source : Upstox API (primary) + yfinance (fallback)")
    log.info("Market hours: 9:15 AM — 3:30 PM IST")
    log.info("=" * 60)
    scheduler = build_scheduler()
    try:
        log.info("Scheduler started. Press Ctrl+C to stop.")
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")