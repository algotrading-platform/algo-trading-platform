#!/usr/bin/env python3
# ============================================================
# run_single_scan.py
#
# Runs ONE scan cycle across all timeframes and instruments.
# Called by GitHub Actions every 5 minutes.
#
# Unlike run_scheduler.py (which runs continuously with APScheduler),
# this script runs once and exits. GitHub Actions handles
# the scheduling by calling it every 5 minutes.
#
# The script is smart:
#   - Checks market hours before scanning
#   - Runs only the timeframes whose candle just closed
#   - Exits cleanly after scan
# ============================================================

import os
import sys
import time
import logging
from datetime import datetime, time as dtime

import pytz
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.makedirs("data", exist_ok=True)
os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("single_scan")

IST = pytz.timezone("Asia/Kolkata")


def should_scan_timeframe(tf_name: str, now: datetime) -> bool:
    """
    Returns True if this timeframe's candle just closed
    (within the last 5 minutes).
    """
    minute = now.minute
    hour   = now.hour
    weekday = now.weekday()

    if tf_name == "5 Minutes":
        # 5-min candles close every 5 minutes
        # We scan at :01, :06, :11, :16, :21, :26, :31, :36, :41, :46, :51, :56
        return minute % 5 == 1

    elif tf_name == "15 Minutes":
        # 15-min candles close at :00, :15, :30, :45
        # We scan at :01, :16, :31, :46
        return minute in (1, 16, 31, 46)

    elif tf_name == "1 Hour":
        # NSE hourly candles close at :16 past the hour
        # (9:15 open → 10:15 close → we scan at 10:16)
        return minute == 16

    elif tf_name == "1 Day":
        # Daily candle closes at 15:30
        # We scan at 15:31
        return hour == 15 and minute == 31

    elif tf_name == "1 Week":
        # Weekly candle closes on Friday at 15:30
        return weekday == 4 and hour == 15 and minute == 32

    elif tf_name == "1 Month":
        # Monthly — checked inside run_scan
        return hour == 15 and minute == 33

    return False


if __name__ == "__main__":
    from core.scheduler.signal_scheduler import (
        run_scan, is_market_hours, TIMEFRAMES
    )

    now_ist = datetime.now(IST)
    log.info(f"Single scan triggered at {now_ist.strftime('%Y-%m-%d %H:%M:%S IST')}")

    if not is_market_hours():
        log.info("Market closed — nothing to scan.")
        sys.exit(0)

    scanned = 0
    for tf_name in TIMEFRAMES.keys():
        if should_scan_timeframe(tf_name, now_ist):
            log.info(f"Scanning {tf_name}...")
            run_scan(tf_name)
            scanned += 1

    if scanned == 0:
        log.info("No timeframes due for scanning this cycle.")

    log.info("Single scan complete.")
