# core/database/__init__.py
from core.database.db import (
    get_client,
    insert_signal,
    get_signals,
    get_last_signal,
    get_last_scan_time,
    get_alert_state,
    upsert_alert_state,
    upsert_backtest,
    get_backtest_results,
)