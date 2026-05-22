# ============================================================
# core/scheduler/signal_scheduler.py
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


NSE_HOLIDAYS = {
    date(2025, 1, 26), date(2025, 3, 14), date(2025, 4, 14),
    date(2025, 4, 18), date(2025, 5, 1),  date(2025, 8, 15),
    date(2025, 10, 2), date(2025, 10, 24),date(2025, 11, 5),
    date(2025, 12, 25),
    date(2026, 1, 26), date(2026, 3, 3),  date(2026, 4, 3),
    date(2026, 4, 14), date(2026, 5, 1),  date(2026, 8, 15),
    date(2026, 10, 2), date(2026, 11, 13),date(2026, 12, 25),
}

IST = pytz.timezone("Asia/Kolkata")


def is_market_day() -> bool:
    today = datetime.now(IST).date()
    if today.weekday() >= 5:
        return False
    return today not in NSE_HOLIDAYS


def is_market_hours() -> bool:
    if not is_market_day():
        return False
    from datetime import time as dtime
    return dtime(9, 15) <= datetime.now(IST).time() <= dtime(15, 30)


def is_last_trading_day_of_month() -> bool:
    today    = datetime.now(IST).date()
    last_day = calendar.monthrange(today.year, today.month)[1]
    for day in range(today.day + 1, last_day + 1):
        candidate = date(today.year, today.month, day)
        if candidate.weekday() < 5 and candidate not in NSE_HOLIDAYS:
            return False
    return True


# ============================================================
# ENGINES — initialised once, reused across all scans
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
    retries:  int = 3,
) -> dict | None:
    """
    Fetch data, calculate RSI, generate signal,
    run backtest, log and alert.
    Returns result dict or None on failure.
    """

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

            # ---- Signal logging ----
            logger.log_signal(
                stock=symbol,
                timeframe=tf_name,
                signal=signal,
                rsi=rsi_val,
                price=price_val,
            )

            # ---- Alert on state change ----
            alert = alerts.check_alert(
                timeframe=tf_name,
                stock=symbol,
                current_signal=signal,
                rsi=rsi_val,
                price=price_val,
            )

            if alert:
                log.info(
                    f"ALERT  {symbol:20s}  {tf_name:12s}  "
                    f"{alert['previous']:5s} -> {alert['signal']:5s}  "
                    f"RSI={rsi_val}  Price={price_val}"
                )

            # ---- Backtest ----
            trades  = backtest_eng.run(df)
            summary = backtest_eng.summarise(trades)

            write_result(
                symbol=symbol,
                name=name,
                timeframe=tf_name,
                category=category,
                summary=summary,
                period=period,
            )

            return {
                "symbol":   symbol,
                "signal":   signal,
                "rsi":      rsi_val,
                "price":    price_val,
                "trades":   summary["trades"],
                "pnl":      summary["pnl"],
                "pnl_pct":  summary["pnl_pct"],
                "win_rate": summary["win_rate"],
                "alerted":  alert is not None,
            }

        except Exception as e:
            if attempt < retries:
                log.warning(
                    f"Retry {attempt}/{retries}  {symbol}  {tf_name}  {e}"
                )
                time.sleep(2 * attempt)
            else:
                log.error(
                    f"FAILED {symbol}  {tf_name}  "
                    f"after {retries} attempts: {e}"
                )
                return None


# ============================================================
# FULL SCAN — one timeframe, all instruments
# ============================================================

def run_scan(tf_name: str) -> None:
    if not is_market_hours():
        log.info(f"SKIP  {tf_name}  market closed")
        return

    if tf_name == "1 Week" and datetime.now(IST).weekday() != 4:
        log.info(f"SKIP  {tf_name}  not Friday")
        return

    if tf_name == "1 Month" and not is_last_trading_day_of_month():
        log.info(f"SKIP  {tf_name}  not last trading day")
        return

    interval = TIMEFRAMES[tf_name]
    period   = PERIOD_MAP[tf_name]

    log.info(
        f"SCAN START  {tf_name}  "
        f"{len(instruments)} instruments  "
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
            for inst in instruments
        }

        for future in as_completed(futures):
            result = future.result()
            if result:
                results.append(result)
                if result["signal"] != "HOLD":
                    signals += 1

    elapsed = round(time.time() - start_time, 1)
    log.info(
        f"SCAN DONE   {tf_name}  "
        f"{len(results)}/{len(instruments)} processed  "
        f"{signals} signals  "
        f"{elapsed}s"
    )


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
    log.info(f"Instruments : {len(instruments)}")
    log.info(f"Timeframes  : {list(TIMEFRAMES.keys())}")
    log.info("=" * 60)
    scheduler = build_scheduler()
    try:
        log.info("Scheduler started. Press Ctrl+C to stop.")
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")
