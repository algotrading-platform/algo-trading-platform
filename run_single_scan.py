#!/usr/bin/env python3
# ============================================================
# run_single_scan.py
#
# Entry point for GitHub Actions.
# Runs one scan cycle for the appropriate mode based on time.
#
# Usage:
#   python run_single_scan.py                 # auto-detect mode
#   python run_single_scan.py --mode equity
#   python run_single_scan.py --mode commodity
#   python run_single_scan.py --mode all
# ============================================================

import sys
import os
import argparse
import logging
from datetime import datetime
import pytz

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from core.scheduler.signal_scheduler import (
    run_scan,
    is_equity_hours,
    is_commodity_hours,
    is_market_day,
    TIMEFRAMES,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("single_scan")

IST = pytz.timezone("Asia/Kolkata")


def detect_mode() -> str:
    """
    Auto-detect which mode to run based on current IST time.
    equity    → 9:15 AM – 3:30 PM
    commodity → 3:31 PM – 11:55 PM
    both      → overlap (shouldn't happen but safe)
    """
    eq  = is_equity_hours()
    com = is_commodity_hours()

    if eq and com:   return "all"
    if eq:           return "equity"
    if com:          return "commodity"
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["equity", "commodity", "all"],
        default=None,
        help="Scan mode. Auto-detected from time if not specified."
    )
    args = parser.parse_args()

    if not is_market_day():
        log.info("Market closed today (weekend or holiday). Skipping scan.")
        sys.exit(0)

    mode = args.mode or detect_mode()

    if mode is None:
        log.info("Outside all market hours. Skipping scan.")
        sys.exit(0)

    now = datetime.now(IST).strftime("%H:%M IST")
    log.info(f"Single scan — mode={mode} — time={now}")

    # Run all timeframes for the detected mode
    # GitHub Actions calls this every 5 min for equity
    # and every 30 min for commodity (separate workflow jobs)
    for tf in TIMEFRAMES.keys():
        run_scan(tf, mode)

    log.info("Single scan complete.")


if __name__ == "__main__":
    main()