# ============================================================
# core/reporting/periods.py
#
# IST-aligned period bucketing for the reporting subsystem.
# Generalizes core.database.db._ist_today_bounds_utc() to weekly/
# monthly/yearly/custom ranges. A report period is a date-bucketing
# choice for the report ROLLUP (daily/weekly/monthly/yearly), not a
# scan timeframe (see configs/timeframes.py) -- the two concepts are
# unrelated and shouldn't be conflated.
# ============================================================

import calendar
from datetime import date, datetime, timedelta, timezone

import pytz

IST = pytz.timezone("Asia/Kolkata")

PERIOD_TYPES = ["daily", "weekly", "monthly", "yearly", "custom"]


def _ist_midnight_to_utc(d: date) -> datetime:
    naive = datetime(d.year, d.month, d.day)
    return IST.localize(naive).astimezone(timezone.utc)


def get_period_bounds(
    period: str,
    anchor: date = None,
    custom_start: date = None,
    custom_end: date = None,
) -> tuple:
    """
    Returns (start_utc, end_utc, label) for a report period.

    period: "daily" | "weekly" | "monthly" | "yearly" | "custom"
    anchor: any date inside the period (default: today, IST). Ignored
            for "custom".
    custom_start/custom_end: required (inclusive, IST calendar dates)
            when period == "custom".
    """
    anchor = anchor or datetime.now(IST).date()

    if period == "daily":
        start, end = anchor, anchor + timedelta(days=1)
        label = anchor.isoformat()

    elif period == "weekly":
        # Calendar week, Monday-Sunday. Numerically identical to a
        # Mon-Fri "trading week" for row-filtering purposes (the
        # exchange is closed Sat/Sun so no rows exist for those days
        # regardless) but simpler to compute and reads naturally
        # ("the week of Aug 10", not "trading week 33").
        week_start = anchor - timedelta(days=anchor.weekday())
        week_end   = week_start + timedelta(days=7)
        start, end = week_start, week_end
        label = f"{week_start.isoformat()}_to_{(week_end - timedelta(days=1)).isoformat()}"

    elif period == "monthly":
        start = anchor.replace(day=1)
        last_day = calendar.monthrange(anchor.year, anchor.month)[1]
        end = start + timedelta(days=last_day)
        label = f"{anchor.year:04d}-{anchor.month:02d}"

    elif period == "yearly":
        start = date(anchor.year, 1, 1)
        end   = date(anchor.year + 1, 1, 1)
        label = f"{anchor.year:04d}"

    elif period == "custom":
        if custom_start is None or custom_end is None:
            raise ValueError("custom_start and custom_end are required for period='custom'")
        if custom_end < custom_start:
            raise ValueError("custom_end must not be before custom_start")
        start = custom_start
        end   = custom_end + timedelta(days=1)  # inclusive end date -> exclusive UTC bound
        label = f"{custom_start.isoformat()}_to_{custom_end.isoformat()}"

    else:
        raise ValueError(f"Unknown period type: {period!r} (expected one of {PERIOD_TYPES})")

    return _ist_midnight_to_utc(start), _ist_midnight_to_utc(end), label


def count_trading_days(start_utc: datetime, end_utc: datetime, holidays: set = None) -> int:
    """
    Number of NSE trading days (Mon-Fri, minus holidays) in
    [start_utc, end_utc) -- used for Overview-sheet context (e.g. "18
    trading days, 142 signals" makes signal/trade density comparable
    across periods of different length). Pass
    core.scheduler.signal_scheduler.NSE_HOLIDAYS as `holidays`.
    """
    holidays  = holidays or set()
    start_ist = start_utc.astimezone(IST).date()
    end_ist   = end_utc.astimezone(IST).date()  # exclusive
    n = 0
    d = start_ist
    while d < end_ist:
        if d.weekday() < 5 and d not in holidays:
            n += 1
        d += timedelta(days=1)
    return n
