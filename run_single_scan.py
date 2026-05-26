#!/usr/bin/env python3
# ============================================================
# run_single_scan.py
#
# Entry point for GitHub Actions.
# Runs one scan cycle — all instruments 9:15–3:30 IST only.
#
# Usage:
#   python run_single_scan.py        # auto-detect
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
    is_market_hours,
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["all"],
        default="all",
        help="Scan mode. Always 'all' — all instruments together."
    )
    args = parser.parse_args()

    if not is_market_day():
        log.info("Market closed today (weekend or holiday). Skipping scan.")
        sys.exit(0)

    if not is_market_hours():
        log.info("Outside market hours (9:15–3:30 IST). Skipping scan.")
        sys.exit(0)

    now = datetime.now(IST).strftime("%H:%M IST")
    log.info(f"Single scan — mode=all — time={now}")

    for tf in TIMEFRAMES.keys():
        run_scan(tf, "all")

    log.info("Single scan complete.")


if __name__ == "__main__":
    main()