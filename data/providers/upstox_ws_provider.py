# ============================================================
# data/providers/upstox_ws_provider.py
#
# Same fetch_data(symbol, interval, period) -> DataFrame contract
# as UpstoxProvider (data/providers/base_provider.py is the only
# seam strategy_engine.py talks to) — this subclass just swaps out
# where "today's" intraday candles come from.
#
# UpstoxProvider.fetch_data() already does:
#   REST historical (excludes today) + REST intraday-today merge
#   + resample
# This class replaces the REST intraday-today call with a read from
# live_candles_1min (kept warm by core/marketdata/ws_listener.py),
# which is what removes the per-cycle REST-call-per-symbol bottleneck
# and is what lets a scan cover a much wider instrument universe.
#
# Falls through to the unchanged parent implementation (full
# REST/yfinance path) at every failure point — listener down, symbol
# not subscribed, stale row, any error — so this is purely additive:
# nothing regresses if the WS listener has a bad day.
# ============================================================

import logging
from datetime import datetime

import pandas as pd
import pytz

from core.database import db
from data.providers.upstox_provider import (
    UPSTOX_FETCH_MAP,
    UpstoxProvider,
    get_token,
    resample_ohlc,
)

log = logging.getLogger("upstox_ws_provider")
IST = pytz.timezone("Asia/Kolkata")

# Intervals whose "today" portion the WS feed can serve (intraday
# only — 1d/1wk/1mo fetch full daily candles directly from REST and
# don't have a "today, minute-by-minute" gap to fill).
LIVE_INTERVALS = {"5m", "15m", "1h"}

# If the newest live row is older than this, treat it as stale
# (listener down / market closed) and fall back to REST.
MAX_LIVE_AGE_MINUTES = 10


class UpstoxWSProvider(UpstoxProvider):

    def fetch_data(
        self,
        symbol: str,
        interval: str = "1h",
        period: str = "1mo",
    ) -> pd.DataFrame:

        if interval not in LIVE_INTERVALS:
            return super().fetch_data(symbol, interval, period)

        token = get_token()
        if not token:
            return super().fetch_data(symbol, interval, period)

        fetch_config = UPSTOX_FETCH_MAP.get(interval)
        if not fetch_config:
            return super().fetch_data(symbol, interval, period)
        fetch_interval, resample_rule, fetch_period_override = fetch_config
        effective_period = fetch_period_override if fetch_period_override else period

        live_today = db.get_live_candles_today(symbol)
        if not self._is_fresh(live_today):
            return super().fetch_data(symbol, interval, period)

        upstox_key = self._resolve_symbol(symbol, token)
        if not upstox_key:
            return super().fetch_data(symbol, interval, period)

        to_date, from_date = self._period_to_dates(effective_period)
        try:
            history_df = self._fetch_candles(
                token=token,
                instrument_key=upstox_key,
                interval=fetch_interval,
                from_date=from_date,
                to_date=to_date,
            )
        except Exception as e:
            log.warning(f"history fetch failed for {symbol}, falling back to REST path: {e}")
            return super().fetch_data(symbol, interval, period)

        if history_df is None or history_df.empty:
            df = live_today
        else:
            df = (
                pd.concat([history_df, live_today], ignore_index=True)
                  .drop_duplicates(subset=["Datetime"], keep="last")
                  .sort_values("Datetime")
                  .reset_index(drop=True)
            )

        if resample_rule:
            df = resample_ohlc(df, resample_rule)

        if df is None or df.empty:
            return super().fetch_data(symbol, interval, period)

        self.last_source = "upstox_ws"
        try:
            df.attrs["data_source"] = "upstox_ws"
        except Exception:
            pass
        return df

    def _is_fresh(self, live_today: pd.DataFrame) -> bool:
        if live_today is None or live_today.empty:
            return False
        try:
            last_ts = pd.to_datetime(live_today["Datetime"].iloc[-1])
            if last_ts.tzinfo is None:
                last_ts = IST.localize(last_ts)
            age_minutes = (datetime.now(IST) - last_ts).total_seconds() / 60
            return age_minutes <= MAX_LIVE_AGE_MINUTES
        except Exception:
            return False
