# ============================================================
# core/scheduler/signal_scheduler.py
#
# Market hours:
#   Single window: 9:15 AM – 3:30 PM IST  Mon–Fri
#   ALL instruments scanned together:
#   Indexes + Stocks + Commodities
#
# No pre-market scan. No evening scan.
# Everything stops at 3:30 PM IST.
# ============================================================

import os
import sys
import time
import logging
import calendar
from datetime import datetime, date
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

load_dotenv()

sys.path.append(
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../..")
    )
)

from data.providers.yfinance_provider import YFinanceProvider
from core.indicators.rsi_indicator import RSIIndicator
from core.signals.reversal_rsi_signal import ReversalRSISignal
from core.backtesting.rsi_backtest import RSIBacktest
from core.backtesting.backtest_store import write_result
from core.logger.signal_logger import SignalLogger
from core.alerts.alert_manager import AlertManager
from configs.instruments import (
    get_all_instruments,
    COMMODITIES,
    COMMODITIES_SKIP_TIMEFRAMES,
)
from configs.timeframes import TIMEFRAMES, PERIOD_MAP


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("scheduler")

IST = pytz.timezone("Asia/Kolkata")

NSE_HOLIDAYS = {
    date(2025, 1, 26), date(2025, 3, 14), date(2025, 4, 14),
    date(2025, 4, 18), date(2025, 5, 1),  date(2025, 8, 15),
    date(2025, 10, 2), date(2025, 10, 24),date(2025, 11, 5),
    date(2025, 12, 25),
    date(2026, 1, 26), date(2026, 3, 3),  date(2026, 4, 3),
    date(2026, 4, 14), date(2026, 5, 1),  date(2026, 8, 15),
    date(2026, 10, 2), date(2026, 11, 13),date(2026, 12, 25),
}


# ============================================================
# MARKET HOURS CHECKS
# ============================================================

def is_market_day() -> bool:
    today = datetime.now(IST).date()
    return today.weekday() < 5 and today not in NSE_HOLIDAYS


def is_market_hours() -> bool:
    """Single window: 9:15 AM – 3:30 PM IST"""
    if not is_market_day():
        return False
    from datetime import time as dtime
    t = datetime.now(IST).time()
    return dtime(9, 15) <= t <= dtime(15, 30)


# Keep for backward compatibility with run_single_scan.py
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
# ENGINES
# ============================================================

provider      = YFinanceProvider()
rsi_indicator = RSIIndicator()
signal_engine = ReversalRSISignal()
backtest_eng  = RSIBacktest()
logger        = SignalLogger()
alerts        = AlertManager()
instruments   = get_all_instruments()


# ============================================================
# SINGLE INSTRUMENT SCAN
# ============================================================

def scan_instrument(
    symbol:   str,
    name:     str,
    category: str,
    tf_name:  str,
    interval: str,
    period:   str,
    strategy: str = "RSI Reversal",
    retries:  int = 3,
) -> dict | None:

    # Skip commodities on intraday timeframes
    if symbol in COMMODITIES and tf_name in COMMODITIES_SKIP_TIMEFRAMES:
        return None

    for attempt in range(1, retries + 1):
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

            latest    = df.iloc[-1]
            signal    = signal_engine.generate_signal(df["RSI"])
            rsi_val   = round(float(latest["RSI"]), 2)
            price_val = round(float(latest["Close"]), 2)

            # Log signal
            logger.log_signal(
                stock=symbol,
                timeframe=tf_name,
                signal=signal,
                rsi=rsi_val,
                price=price_val,
                strategy=strategy,
            )

            # Alert on state change
            alert = alerts.check_alert(
                timeframe=tf_name,
                stock=symbol,
                current_signal=signal,
                rsi=rsi_val,
                price=price_val,
                strategy=strategy,
            )

            if alert:
                log.info(
                    f"ALERT  {symbol:20s}  {tf_name:12s}  "
                    f"{alert['previous']:5s} -> {alert['signal']:5s}  "
                    f"RSI={rsi_val}  Price={price_val}"
                )

            # Backtest
            trades  = backtest_eng.run(df)
            summary = backtest_eng.summarise(trades)

            write_result(
                symbol=symbol,
                name=name,
                timeframe=tf_name,
                category=category,
                summary=summary,
                period=period,
                strategy=strategy,
            )

            return {
                "symbol":   symbol,
                "signal":   signal,
                "rsi":      rsi_val,
                "price":    price_val,
                "trades":   summary["trades"],
                "pnl":      summary["pnl"],
                "win_rate": summary["win_rate"],
                "alerted":  alert is not None,
            }

        except Exception as e:
            if attempt < retries:
                log.warning(f"Retry {attempt}/{retries}  {symbol}  {tf_name}  {e}")
                time.sleep(2 * attempt)
            else:
                log.error(f"FAILED {symbol}  {tf_name}  after {retries} attempts: {e}")
                return None


# ============================================================
# FULL SCAN — one timeframe, filtered by mode
# ============================================================

def run_scan(tf_name: str, mode: str = "all") -> None:
    """
    Scans ALL instruments — Indexes + Stocks + Commodities.
    Mode parameter kept for backward compatibility but ignored.
    Single window: 9:15 AM – 3:30 PM IST only.
    """
    interval = TIMEFRAMES[tf_name]
    period   = PERIOD_MAP[tf_name]

    # Weekly/Monthly special gates
    if tf_name == "1 Week" and datetime.now(IST).weekday() != 4:
        log.info(f"SKIP  {tf_name}  not Friday")
        return
    if tf_name == "1 Month" and not is_last_trading_day_of_month():
        log.info(f"SKIP  {tf_name}  not last trading day")
        return

    # Single market hours check — all instruments same window
    if not is_market_hours():
        log.info(f"SKIP  {tf_name}  outside market hours (9:15–3:30 IST)")
        return

    scan_list = instruments

    if not scan_list:
        log.info(f"SKIP  {tf_name}  no instruments")
        return

    log.info(
        f"SCAN START  {tf_name}  [all]  "
        f"{len(scan_list)} instruments  "
        f"interval={interval}  period={period}"
    )

    start_time = time.time()
    results    = []
    signals    = 0

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(
                scan_instrument,
                inst["symbol"],
                inst["name"],
                inst["category"],
                tf_name,
                interval,
                period,
            ): inst
            for inst in scan_list
        }

        for future in as_completed(futures):
            result = future.result()
            if result:
                results.append(result)
                if result["signal"] != "HOLD":
                    signals += 1

    elapsed = round(time.time() - start_time, 1)
    log.info(
        f"SCAN DONE   {tf_name}  [all]  "
        f"{len(results)}/{len(scan_list)} processed  "
        f"{signals} signals  {elapsed}s"
    )


# ============================================================
# SCHEDULER — local laptop use
# ============================================================

def build_scheduler() -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone=IST)

    # ---- EQUITY SCANS (9:15 AM – 3:30 PM) ----

    scheduler.add_job(
        lambda: run_scan("5 Minutes", "all"),
        CronTrigger(
            minute="1,6,11,16,21,26,31,36,41,46,51,56",
            hour="9,10,11,12,13,14",
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

    # ---- COMMODITY EVENING SCANS REMOVED ----
    # All scanning stops at 3:30 PM IST.
    # Commodities are included in the equity window above.

    return scheduler


def start() -> None:
    log.info("=" * 60)
    log.info("Algo Trading Signal Scheduler")
    log.info(f"Instruments : {len(instruments)}")
    log.info(f"Timeframes  : {list(TIMEFRAMES.keys())}")
    log.info("Market hours: 9:15 AM – 3:30 PM IST (all instruments)")
    log.info("Indexes + Stocks + Commodities scanned together")
    log.info("No pre-market scan. No evening scan.")
    log.info("=" * 60)
    scheduler = build_scheduler()
    try:
        log.info("Scheduler started. Press Ctrl+C to stop.")
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")