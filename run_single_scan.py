#!/usr/bin/env python3
# ============================================================
# run_single_scan.py
#
# Entry point for the scheduled Container Apps Job.
# Runs one scan cycle - all instruments 9:15-3:30 IST only.
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
    run_post_scan_housekeeping,
    is_market_day,
    TIMEFRAMES,
)
from core.database import db

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
        help="Scan mode. Always 'all' - all instruments together."
    )
    args = parser.parse_args()

    if not is_market_day():
        log.info("Market closed today (weekend or holiday). Skipping scan.")
        sys.exit(0)

    # NOTE: deliberately NOT gating on is_market_hours() (9:45-15:15) here.
    # EOD timeframes (1 Day/Week/Month) are scheduled for 15:30/15:35/15:40
    # — after that window closes — and run_primary_scan() already applies
    # the correct, narrower is_market_hours() check per-timeframe for the
    # non-EOD ones (see _EOD_TIMEFRAMES branch there). A top-level exit here
    # meant the whole process quit before the per-timeframe loop below ever
    # got a chance to reach the EOD timeframes, so they could never run no
    # matter what their own due-check said. Cheap either way: every other
    # timeframe still no-ops correctly outside its own window via that same
    # per-timeframe check.

    # Run-lock: prevents a new cron-triggered execution from racing a
    # still-running previous one. Fails closed -- if the lock can't be
    # acquired (already running, or the check itself errored), skip
    # this cycle; the next scheduled trigger retries in a few minutes.
    if not db.try_acquire_scan_lock("single_scan", stale_after_seconds=900):
        log.warning("Previous scan still running (or lock check failed) - skipping this cycle.")
        sys.exit(0)

    try:
        # Snapshotted ONCE and passed to every timeframe below. Each
        # timeframe's due-check (_is_scan_due) looks for an exact IST
        # minute (e.g. "1 Hour" only at minute==6) — re-reading the
        # clock per timeframe meant that by the time the loop reached
        # "15 Minutes"/"1 Hour", the 5-Minute scan ahead of it (which
        # alone regularly takes several minutes) had already pushed
        # the clock past their window, so they were silently skipped
        # almost every cycle. One shared snapshot makes every
        # timeframe's due-check reflect the minute this Job actually
        # fired at, not the minute it happened to be reached.
        run_started_at = datetime.now(IST)
        now_str = run_started_at.strftime("%H:%M IST")
        log.info(f"Single scan - mode=all - time={now_str}")

        for tf in TIMEFRAMES.keys():
            run_scan(tf, "all", now=run_started_at)

        # Once per execution, not once per timeframe — see
        # run_post_scan_housekeeping()'s docstring for why this used to
        # live inside the loop above and what that repetition caused.
        run_post_scan_housekeeping()

        log.info("Single scan complete.")
    finally:
        db.release_scan_lock("single_scan")


if __name__ == "__main__":
    main()