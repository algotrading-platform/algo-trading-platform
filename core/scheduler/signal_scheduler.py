# ============================================================
# core/scheduler/signal_scheduler.py
#
# Runs TWO strategies simultaneously on every scan:
#   1. RSI Reversal    — all 182 instruments
#   2. Cash-Futures Arbitrage — F&O stocks only (needs Upstox)
#
# Both strategies always run. Dashboard filters by strategy.
# No Railway restart needed to switch strategy view.
# ============================================================

import os
import sys
import time
import logging
import calendar
from datetime import datetime, date
from concurrent.futures import ThreadPoolExecutor

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

load_dotenv()

sys.path.append(
    os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
)

from data.providers.upstox_provider import UpstoxProvider, get_token
from core.engine.strategy_engine import StrategyEngine
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
# NSE HOLIDAYS 2025–2027
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
# MARKET HOURS
# ============================================================

def is_market_day() -> bool:
    today = datetime.now(IST).date()
    return today.weekday() < 5 and today not in NSE_HOLIDAYS


def is_market_hours() -> bool:
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

provider    = UpstoxProvider()
instruments = get_all_instruments_extended()

# F&O stocks only for arbitrage (has futures contracts)
fno_instruments = [i for i in instruments if i["category"] == "STOCK"]

# Strategy engines — both always active
_rsi_engine = StrategyEngine("RSI Reversal")
_arb_engine = StrategyEngine("Cash-Futures Arbitrage")


# ============================================================
# SCAN FUNCTIONS
# ============================================================

def run_rsi_scan(tf_name: str) -> None:
    """RSI Reversal scan — all 182 instruments."""
    interval = TIMEFRAMES[tf_name]
    period   = PERIOD_MAP[tf_name]

    if tf_name == "1 Week" and datetime.now(IST).weekday() != 4:
        return
    if tf_name == "1 Month" and not is_last_trading_day_of_month():
        return
    if not is_market_hours():
        return

    _rsi_engine.run_scan(
        provider=provider,
        tf_name=tf_name,
        interval=interval,
        period=period,
        instruments=instruments,
    )


def run_arbitrage_scan(tf_name: str) -> None:
    """
    Arbitrage scan — F&O stocks only.
    Only runs on 5min and 15min (needs live prices).
    Skips if no valid Upstox token.
    """
    if tf_name not in ("5 Minutes", "15 Minutes"):
        return
    if not is_market_hours():
        return

    # Skip if no Upstox token
    token = get_token()
    if not token:
        log.info("Arbitrage scan skipped — no valid Upstox token")
        return

    interval = TIMEFRAMES[tf_name]
    period   = PERIOD_MAP[tf_name]

    _arb_engine.run_scan(
        provider=provider,
        tf_name=tf_name,
        interval=interval,
        period=period,
        instruments=fno_instruments,
    )


def run_scan(tf_name: str, mode: str = "all") -> None:
    """
    Runs RSI then Arbitrage sequentially.
    Sequential to stay within Railway 1GB RAM limit.
    Both always active — dashboard filters by strategy.
    Total scan time ~60-70s, well within 5-min cycle.
    """
    run_rsi_scan(tf_name)        # RSI first — frees memory before arbitrage
    run_arbitrage_scan(tf_name)  # Arbitrage after RSI completes


# ============================================================
# SCHEDULER
# ============================================================

def build_scheduler() -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone=IST)

    scheduler.add_job(
        lambda: run_scan("5 Minutes"),
        CronTrigger(
            minute="1,6,11,16,21,26,31,36,41,46,51,56",
            hour="9,10,11,12,13,14",
            day_of_week="mon-fri", timezone=IST,
        ),
        id="scan_5min", name="5 Minute Scan",
        max_instances=1, coalesce=True,
    )

    scheduler.add_job(
        lambda: run_scan("15 Minutes"),
        CronTrigger(
            minute="1,16,31,46",
            hour="9,10,11,12,13,14,15",
            day_of_week="mon-fri", timezone=IST,
        ),
        id="scan_15min", name="15 Minute Scan",
        max_instances=1, coalesce=True,
    )

    scheduler.add_job(
        lambda: run_scan("1 Hour"),
        CronTrigger(
            minute="16", hour="10,11,12,13,14,15",
            day_of_week="mon-fri", timezone=IST,
        ),
        id="scan_1hour", name="1 Hour Scan",
        max_instances=1, coalesce=True,
    )

    scheduler.add_job(
        lambda: run_scan("1 Day"),
        CronTrigger(
            hour="15", minute="31",
            day_of_week="mon-fri", timezone=IST,
        ),
        id="scan_1day", name="1 Day Scan",
        max_instances=1, coalesce=True,
    )

    scheduler.add_job(
        lambda: run_scan("1 Week"),
        CronTrigger(
            hour="15", minute="32",
            day_of_week="fri", timezone=IST,
        ),
        id="scan_1week", name="1 Week Scan",
        max_instances=1, coalesce=True,
    )

    scheduler.add_job(
        lambda: run_scan("1 Month"),
        CronTrigger(
            hour="15", minute="33",
            day_of_week="mon-fri", timezone=IST,
        ),
        id="scan_1month", name="1 Month Scan",
        max_instances=1, coalesce=True,
    )

    return scheduler


def start() -> None:
    log.info("=" * 60)
    log.info("Algo Trading Signal Scheduler")
    log.info(f"Instruments : {len(instruments)} total")
    log.info(f"F&O stocks  : {len(fno_instruments)} (for arbitrage)")
    log.info(f"Timeframes  : {list(TIMEFRAMES.keys())}")
    log.info("Strategies  : RSI Reversal + Cash-Futures Arbitrage (parallel)")
    log.info("Data source : Upstox API (primary) + yfinance (fallback)")
    log.info("Market hours: 9:15 AM – 3:30 PM IST")
    log.info("=" * 60)
    scheduler = build_scheduler()
    try:
        log.info("Scheduler started. Press Ctrl+C to stop.")
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")