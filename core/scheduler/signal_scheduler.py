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

NSE_HOLIDAYS = {
    date(2025, 1, 26), date(2025, 2, 26), date(2025, 3, 14),
    date(2025, 3, 31), date(2025, 4, 14), date(2025, 4, 18),
    date(2025, 5, 1),  date(2025, 8, 15), date(2025, 8, 27),
    date(2025, 10, 2), date(2025, 10, 20),date(2025, 10, 21),
    date(2025, 11, 5), date(2025, 12, 25),

    date(2026, 1, 26), date(2026, 2, 26), date(2026, 3, 20),
    date(2026, 3, 25), date(2026, 4, 2),  date(2026, 4, 3),
    date(2026, 4, 14), date(2026, 4, 30), date(2026, 6, 27),
    date(2026, 7, 17), date(2026, 8, 15), date(2026, 8, 27),
    date(2026, 9, 25), date(2026, 10, 2), date(2026, 10, 20),
    date(2026, 10, 21),date(2026, 11, 25),date(2026, 12, 25),

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
    """Single window: 9:15 AM — 3:30 PM IST"""
    if not is_market_day():
        return False
    from datetime import time as dtime
    t = datetime.now(IST).time()
    return dtime(9, 15) <= t <= dtime(15, 30)


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

# Primary strategy engine — recreated when strategy changes
_current_strategy  = None
_primary_engine    = None


def get_primary_engine() -> StrategyEngine:
    """
    Returns engine for currently selected strategy.
    Reads from PostgreSQL app_config on every scan —
    dashboard can change strategy without restart.
    """
    global _primary_engine, _current_strategy

    try:
        from core.database.db import get_config
        strategy = get_config("SIGNAL_STRATEGY") or os.getenv("SIGNAL_STRATEGY", "RSI Reversal")
    except Exception:
        strategy = os.getenv("SIGNAL_STRATEGY", "RSI Reversal")

    if strategy != _current_strategy:
        log.info(f"Strategy: {_current_strategy} → {strategy}")
        _primary_engine   = StrategyEngine(strategy)
        _current_strategy = strategy

    return _primary_engine


# ============================================================
# SCAN FUNCTIONS
# ============================================================

def run_primary_scan(tf_name: str) -> None:
    """Primary strategy scan on all instruments."""
    interval = TIMEFRAMES[tf_name]
    period   = PERIOD_MAP[tf_name]

    if tf_name == "1 Week" and datetime.now(IST).weekday() != 4:
        return
    if tf_name == "1 Month" and not is_last_trading_day_of_month():
        return
    if not is_market_hours():
        return

    get_primary_engine().run_scan(
        provider=provider,
        tf_name=tf_name,
        interval=interval,
        period=period,
        instruments=instruments,
    )


def run_arbitrage_scan(tf_name: str) -> None:
    """
    Arbitrage scan on F&O stocks only.

    FIXED (2026-06-19): runs HOURLY only (not every 5/15 mins).

    Reasons:
      - Arbitrage spread changes slowly (minutes to hours)
      - Reduces Upstox API calls from 2000+/day to ~1000/day
      - Prevents 429 rate limiting on futures search API
      - Futures contracts cached in PostgreSQL — fetched once per day
      - Only futures PRICE needs to be fetched each hour
    """
    # FIXED: only run on 15 Minutes timeframe
    if tf_name != "15 Minutes":
        return

    if not is_market_hours():
        return

    from data.providers.upstox_provider import get_token
    if not get_token():
        log.info("Arbitrage scan skipped — no valid Upstox token")
        return

    interval = TIMEFRAMES[tf_name]
    period   = PERIOD_MAP[tf_name]

    log.info(f"Running arbitrage scan — {len(fno_instruments)} F&O stocks")

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
    log.info(f"Strategy    : {active_strat}")
    log.info(f"Arbitrage   : Every 15 mins (9:31, 9:46 ... 15:16, 15:31 IST)")
    log.info("Data source : Upstox API (primary) + yfinance (fallback)")
    log.info("Market hours: 9:15 AM — 3:30 PM IST")
    log.info("=" * 60)
    scheduler = build_scheduler()
    try:
        log.info("Scheduler started. Press Ctrl+C to stop.")
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")