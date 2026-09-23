#!/usr/bin/env python3
# ============================================================
# run_single_scan.py
#
# Entry point for the scheduled Container Apps Job(s).
#
# Enterprise Phase 2 (2026-09-21): split from one job driving all 6
# timeframes sequentially (each cron tick paying for the 5-Minute
# scan's full cost before 15-Minute/1-Hour/EOD even got a chance to
# run, behind a single GLOBAL run-lock) into 4 independent jobs --
# algo-scanner-5min, algo-scanner-15min, algo-scanner-1hour,
# algo-scanner-eod -- each with its own cron trigger matching its
# real cadence and its own run-lock, so a slow 5-Minute cycle can
# never again delay or skip another timeframe. See infra/main.bicep's
# scanJob loop for the per-group cron expressions.
#
# --group selects which TIMEFRAMES this execution covers; "all" (the
# original behavior) is kept only for local/manual runs, not used by
# any deployed Job.
#
# Usage:
#   python run_single_scan.py --group 5min
#   python run_single_scan.py --group all     # local dev / manual only
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
from core.telemetry import init_telemetry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("single_scan")
IST = pytz.timezone("Asia/Kolkata")

# Which TIMEFRAMES keys each --group covers. "5min" also owns the
# once-per-execution housekeeping step (position monitoring, EOD
# square-off check) -- it's the tightest cadence, so open positions
# are never checked less often than they were under the single-job
# design, regardless of which other groups exist.
GROUPS = {
    "5min":  ["5 Minutes"],
    "15min": ["15 Minutes"],
    "1hour": ["1 Hour"],
    "eod":   ["1 Day", "1 Week", "1 Month"],
    "all":   list(TIMEFRAMES.keys()),  # local/manual only -- no deployed Job uses this
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--group",
        choices=list(GROUPS.keys()),
        default="all",
        help="Which timeframe group this execution covers (see GROUPS above).",
    )
    args = parser.parse_args()
    group_timeframes = GROUPS[args.group]

    init_telemetry("scanner")

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
    # Keyed by group (not one global "single_scan" name any more) --
    # each group's Job runs fully independently now, so a slow 1-Hour
    # cycle holding its own lock must never block 5-Minute's.
    lock_name = f"single_scan_{args.group}"
    if not db.try_acquire_scan_lock(lock_name, stale_after_seconds=900):
        log.warning(f"Previous '{args.group}' scan still running (or lock check failed) - skipping this cycle.")
        sys.exit(0)

    try:
        # Snapshotted ONCE and passed to every timeframe in this group.
        # Each timeframe's due-check (_is_scan_due) looks for an exact
        # IST minute -- re-reading the clock per timeframe meant that by
        # the time the loop reached a later timeframe, an earlier one's
        # scan (which can itself take a while) had already pushed the
        # clock past its window. One shared snapshot makes every
        # timeframe's due-check reflect the minute this Job actually
        # fired at, not the minute it happened to be reached. Now mostly
        # moot for cross-timeframe drift (each group is its own Job),
        # but "eod" still covers 3 timeframes in one execution.
        run_started_at = datetime.now(IST)
        now_str = run_started_at.strftime("%H:%M IST")
        log.info(f"Single scan - group={args.group} - time={now_str}")

        for tf in group_timeframes:
            run_scan(tf, "all", now=run_started_at)

        # Only the 5min group's execution runs housekeeping -- see
        # GROUPS' comment above and run_post_scan_housekeeping()'s own
        # docstring for why this must run exactly once per cycle, not
        # once per timeframe.
        if "5 Minutes" in group_timeframes:
            run_post_scan_housekeeping()

        log.info("Single scan complete.")
    finally:
        db.release_scan_lock(lock_name)


if __name__ == "__main__":
    main()