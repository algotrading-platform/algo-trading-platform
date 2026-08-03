# ============================================================
# core/marketdata/ws_listener.py
#
# Upstox WebSocket (Market Data Feed V3, "full" mode) listener.
# Long-running process — needs an always-on Azure Container App,
# NOT the existing scheduled Job (run_single_scan.py exits after
# one pass; a WebSocket needs a persistent connection).
#
# What it does:
#   - Subscribes to the Nifty 500 + F&O universe (configs.universe)
#     resolved to Upstox instrument keys (upstox_provider's cache).
#   - Uses the official SDK's MarketDataStreamerV3 — it already
#     handles the auth handshake and protobuf decoding, so this
#     module works with plain dicts, not raw protobuf.
#   - Keeps the running (still-forming) 1-minute candle per
#     instrument in memory, flushing a batched upsert to Postgres
#     (live_candles_1min) every FLUSH_INTERVAL_SEC seconds.
#
# What reads this data (both with a REST/yfinance fallback if the
# listener is down or a symbol has no fresh row):
#   - data/providers/upstox_ws_provider.py (candle series for scans)
#   - core/execution/paper_trader.py._current_price() (latest price)
#
# NOTE: the exact shape of the decoded "full" mode message (nested
# key names under feeds[instrument_key]) should be confirmed against
# a live message the first time this runs — _extract_candle() below
# is written from Upstox's documented field names but hasn't been
# exercised against a real feed yet. Log a raw sample if candles
# aren't landing in the DB as expected.
# ============================================================

import logging
import threading
import time
from datetime import datetime

import pytz

from configs.universe import get_all_instruments_extended
from core.database import db
from data.providers.upstox_provider import _load_instruments, get_instrument_key, get_token

log = logging.getLogger("ws_listener")
IST = pytz.timezone("Asia/Kolkata")

FLUSH_INTERVAL_SEC = 5
SUPERVISOR_TICK_SEC = 30
RECONNECT_RETRY_COUNT = 100
RECONNECT_INTERVAL_SEC = 5


def build_subscription_universe() -> list[dict]:
    """
    Resolve configs.universe's Nifty500+F&O+Index list to Upstox
    instrument keys. Commodities (GC=F etc.) have no Upstox equity
    key and are skipped here — they already run on yfinance only
    (see upstox_provider.py's dead MCX auto-detection).
    """
    _load_instruments()
    instruments = get_all_instruments_extended()
    resolved: dict[str, str] = {}
    for inst in instruments:
        symbol = inst.get("symbol")
        if not symbol or symbol in resolved:
            continue
        key = get_instrument_key(symbol)
        if key:
            resolved[symbol] = key

    log.info(f"WS universe resolved: {len(resolved)} of "
             f"{len(instruments)} instruments have an Upstox key")
    return [{"symbol": s, "instrument_key": k} for s, k in resolved.items()]


class WSListener:

    def __init__(self):
        self._key_to_symbol: dict[str, str] = {}
        self._pending: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._streamer = None
        self._need_restart = False

    # --------------------------------------------------------
    # connection
    # --------------------------------------------------------
    def _build_streamer(self, instrument_keys: list[str]):
        import upstox_client

        token = get_token()
        if not token:
            raise RuntimeError(
                "no valid Upstox token — run scripts/upstox_login.py "
                "(tokens expire daily at 3:30 AM IST)"
            )

        cfg = upstox_client.Configuration()
        cfg.access_token = token
        streamer = upstox_client.MarketDataStreamerV3(
            upstox_client.ApiClient(cfg),
            instrument_keys,
            "full",
        )
        streamer.on("message", self._on_message)
        streamer.on("error", self._on_error)
        streamer.on("close", self._on_close)
        streamer.on("autoReconnectStopped", self._on_reconnect_exhausted)
        streamer.auto_reconnect(True, interval=RECONNECT_INTERVAL_SEC,
                                 retry_count=RECONNECT_RETRY_COUNT)
        return streamer

    def _connect_once(self, instrument_keys: list[str]) -> None:
        def _run():
            try:
                log.info(f"connecting WS — {len(instrument_keys)} instruments, mode=full")
                self._streamer = self._build_streamer(instrument_keys)
                self._streamer.connect()  # may block this thread indefinitely — that's fine
            except Exception as e:
                log.error(f"WS connect thread crashed: {e}")
                self._need_restart = True

        threading.Thread(target=_run, daemon=True, name="ws-listener-connect").start()

    # --------------------------------------------------------
    # event handlers (called from the SDK's own thread(s))
    # --------------------------------------------------------
    def _on_message(self, message) -> None:
        try:
            self._handle_feed(message)
        except Exception as e:
            log.warning(f"on_message handling failed: {e}")

    def _on_error(self, error) -> None:
        log.warning(f"WS error: {error}")

    def _on_close(self, *args, **kwargs) -> None:
        log.warning("WS closed")

    def _on_reconnect_exhausted(self, *args, **kwargs) -> None:
        # Most likely cause: the daily token expired mid-session.
        # A fresh MarketDataStreamerV3 (new token) is needed — the
        # SDK's own auto_reconnect can't fix an expired token.
        log.error("WS auto-reconnect exhausted — will rebuild with a fresh token")
        self._need_restart = True

    def _handle_feed(self, message) -> None:
        if not isinstance(message, dict):
            return
        feeds = message.get("feeds") or {}
        for instrument_key, feed in feeds.items():
            symbol = self._key_to_symbol.get(instrument_key)
            if not symbol:
                continue
            row = self._extract_candle(instrument_key, symbol, feed)
            if row:
                with self._lock:
                    self._pending[instrument_key] = row

    def _extract_candle(self, instrument_key: str, symbol: str, feed: dict) -> dict | None:
        # Confirmed against a live message (03-Aug-2026): the nested
        # key is "fullFeed" (not "ff"), and OHLC volume is "vol"
        # (a numeric string), not "volume".
        full_feed = feed.get("fullFeed", {}) if isinstance(feed, dict) else {}
        market_ff = full_feed.get("marketFF") or full_feed.get("indexFF") or {}
        ltpc = market_ff.get("ltpc") or {}
        ltp = ltpc.get("ltp")
        if ltp is None:
            return None

        ohlc_list = (market_ff.get("marketOHLC") or {}).get("ohlc", [])
        i1 = next((o for o in ohlc_list if o.get("interval") == "I1"), None)
        if i1:
            # The candle's own ts (epoch ms) is more correct than
            # "now" -- avoids clock-skew/boundary edge cases.
            try:
                ts = datetime.fromtimestamp(int(i1["ts"]) / 1000, tz=IST).replace(
                    second=0, microsecond=0)
            except (KeyError, ValueError, TypeError):
                ts = datetime.now(IST).replace(second=0, microsecond=0)
            return {
                "instrument_key": instrument_key, "symbol": symbol, "ts": ts,
                "open": i1.get("open", ltp), "high": i1.get("high", ltp),
                "low": i1.get("low", ltp), "close": i1.get("close", ltp),
                "volume": int(float(i1.get("vol", 0) or 0)),
            }

        ts = datetime.now(IST).replace(second=0, microsecond=0)

        # No OHLC field on this particular tick — still track LTP so
        # the latest-price read path stays fresh even before the
        # first I1 candle arrives for this instrument this minute.
        with self._lock:
            prev = self._pending.get(instrument_key)
        if prev and prev["ts"] == ts:
            prev["close"] = ltp
            prev["high"] = max(prev["high"], ltp)
            prev["low"] = min(prev["low"], ltp)
            return prev
        return {
            "instrument_key": instrument_key, "symbol": symbol, "ts": ts,
            "open": ltp, "high": ltp, "low": ltp, "close": ltp, "volume": 0,
        }

    # --------------------------------------------------------
    # flush + supervisor loops (main-process threads)
    # --------------------------------------------------------
    def _flush_loop(self) -> None:
        while True:
            time.sleep(FLUSH_INTERVAL_SEC)
            with self._lock:
                rows = list(self._pending.values())
            if not rows:
                continue
            if db.upsert_live_candles(rows):
                log.debug(f"flushed {len(rows)} candle rows")
            else:
                log.warning(f"flush failed for {len(rows)} rows — will retry next cycle")

    def run_forever(self) -> None:
        universe = build_subscription_universe()
        if not universe:
            log.error("empty subscription universe — aborting")
            return

        self._key_to_symbol = {u["instrument_key"]: u["symbol"] for u in universe}
        instrument_keys = list(self._key_to_symbol.keys())

        threading.Thread(target=self._flush_loop, daemon=True, name="ws-listener-flush").start()
        self._connect_once(instrument_keys)

        while True:
            time.sleep(SUPERVISOR_TICK_SEC)
            if self._need_restart:
                log.info("rebuilding WS connection with a fresh token")
                self._need_restart = False
                self._connect_once(instrument_keys)
