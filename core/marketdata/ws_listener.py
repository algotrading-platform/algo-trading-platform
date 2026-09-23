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
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as dtime

import pytz
import requests

from configs.universe import get_all_instruments_extended
from core.database import db
from core.telemetry import record_signal_to_order_latency
from data.providers.upstox_provider import _load_instruments, get_instrument_key, get_token

log = logging.getLogger("ws_listener")
IST = pytz.timezone("Asia/Kolkata")

# ── Ops alert (Sep 6) — a WS connect failure used to only ever show up
# in container logs, which nobody watches proactively. That's exactly
# what let the Sep 3 token expiry run silently all day: the listener
# was stuck retrying with a dead token from ~5 AM IST, and it took a
# post-mortem hours later to notice. This sends a direct Telegram alert
# the moment reconnection genuinely fails, instead of only logging it.
# Deliberately a plain standalone notifier, not routed through
# AlertManager (that class formats trading-signal messages, not ops
# alerts). Rate-limited so a stuck retry loop can't spam the chat.
_OPS_ALERT_COOLDOWN_SEC = 1800  # at most one alert per 30 min per reason
_last_ops_alert: dict[str, float] = {}
_ops_alert_lock = threading.Lock()


def _candle_ts_is_stale(candle: dict | None) -> bool:
    ts = (candle or {}).get("ts")
    if ts is None:
        return False
    try:
        if getattr(ts, "tzinfo", None) is None:
            ts = pytz.utc.localize(ts)
        return ts.astimezone(IST).date() != datetime.now(IST).date()
    except Exception:
        return False


def _is_stale_breakout(anatomy: dict | None) -> bool:
    """
    True if the pattern's breakout OR flagpole candle isn't from today
    (IST) -- see the Sep 21 MARICO.NS incident this guards against:
    between market open and this timeframe's first candle actually
    closing, the most recent COMPLETE candle can still be the PREVIOUS
    session's last bar, and a trend-grading re-check that happens to
    flip from "suppressed" to "not suppressed" overnight can fire a
    signal on a breakout that already fully played out last session.
    Originally only checked the breakout candle; extended (Sep 22,
    second MARICO.NS report) after the same stale-session-boundary
    issue surfaced on the FLAGPOLE candle instead -- bar1_idx in
    ThreeBarFlagStrategy.generate_signal() can walk back into the
    prior session near market open just as easily as brk_idx can, so
    a flagpole from yesterday needs the same guard as a breakout from
    yesterday.
    Best-effort: missing/malformed anatomy is treated as NOT stale
    (fail open) so a genuine data-shape issue elsewhere never blocks
    a real signal -- this is a staleness check, not a validity check.
    """
    if not anatomy:
        return False
    return (_candle_ts_is_stale(anatomy.get("breakout")) or
            _candle_ts_is_stale(anatomy.get("flagpole")))


def _send_ops_alert(reason: str, message: str) -> None:
    # Locked read-then-write: two connect threads can hit the same
    # failure reason at nearly the same time, and without a lock both
    # can read the stale `last` before either writes the update,
    # letting both past the cooldown and firing a duplicate alert.
    with _ops_alert_lock:
        now = time.time()
        last = _last_ops_alert.get(reason, 0.0)
        if now - last < _OPS_ALERT_COOLDOWN_SEC:
            return
        _last_ops_alert[reason] = now

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id   = os.getenv("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        log.warning(f"ops alert suppressed (Telegram not configured): {message}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": f"⚠️ *WS Listener*: {message}", "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"ops alert send failed: {e}")

FLUSH_INTERVAL_SEC = 5
SUPERVISOR_TICK_SEC = 30

# ── Order-execution worker pool (enterprise Phase 1, 2026-09-20) ──
# _scan_universe_for_patterns() used to call PaperTrader.on_signal()
# (which blocks on a broker network call) SYNCHRONOUSLY, once per
# symbol, inline in its ~500-symbol loop -- a slow order (a 401 retry
# costs up to ~4s, sandbox_client.py) stalled evaluation of every
# other symbol for the rest of that 60s cycle. Detection now only
# writes a trade_intents row (db.enqueue_trade_intent) and moves on;
# this worker pool drains that table on its own short cadence, with
# its own concurrency, fully decoupled from the scan loop's cadence.
ORDER_WORKER_POLL_SEC = 3
ORDER_WORKER_CLAIM_BATCH = 10
ORDER_WORKER_MAX_WORKERS = 8
ORDER_INTENT_STALE_AFTER_SEC = 120  # re-queue a claim stuck in EXECUTING this long
RECONNECT_RETRY_COUNT = 100
RECONNECT_INTERVAL_SEC = 5
BREAKOUT_WATCH_INTERVAL_SEC = 60  # fast breakout watch cadence (Sep 2) --
                                  # see WSListener._breakout_watch_loop

# ── Full-universe pattern scan (Sep 7) ────────────────────────
# Why this exists: the fast breakout watch above only starts once the
# 5-min scanner has ALREADY seen "flagpole + pause, no breakout yet"
# and written a pending_breakouts row. If a whole setup (flagpole ->
# consolidation -> breakout) completes inside one 5-minute gap between
# scans, that intermediate moment is never observed by anything, and
# the trade only fires later via the slower normal-scan path, at a
# worse price (confirmed live, Sep 7: BOSCHLTD.NS -- correct pattern,
# correct stop/target, but no pending_breakouts row was ever written
# because the pause and breakout candles were 5 minutes apart -- the
# same 5-min gap the scanner checks at).
#
# The fix: run the SAME pattern check (ThreeBarFlagStrategy, including
# its own candle catch-up) once a minute against every symbol in this
# listener's universe, not just symbols the scanner already flagged.
# This costs ZERO extra Upstox API calls -- the tick data is already
# arriving via the WebSocket subscription this process already pays
# for -- the only added cost is DB read + CPU inside this container.
# Kept cheap deliberately: each symbol keeps a rolling in-memory 1-min
# candle buffer, refreshed via a genuinely incremental DB read (only
# rows newer than what's already buffered), not a full re-fetch every
# cycle -- see _get_5min_candles(). A naive "re-resample the whole
# day from scratch every minute for 500 symbols" version was
# considered and rejected: on this project's Basic-tier (5 DTU) Azure
# SQL database, that read volume risked forcing a costly tier upgrade
# just to avoid timeouts. This design keeps the per-cycle DB read down
# to whatever's genuinely new (usually 0-1 rows per symbol per pass).
PATTERN_SCAN_INTERVAL_SEC = 60

# Enterprise Phase 3 (2026-09-21) -- the universe is split into this many
# roughly-equal, disjoint symbol shards, each scanned by its own thread on
# the same PATTERN_SCAN_INTERVAL_SEC cadence (see run_forever()). Total
# per-cycle wall time then scales with (universe size / PATTERN_SCAN_SHARDS)
# instead of the full universe size, which is what actually lets the
# instrument count grow without the single-threaded loop's cycle time
# eventually exceeding its own 60s cadence. 4 is a starting point, not
# derived from a measured ceiling -- raise it if a future universe
# expansion (e.g. crypto, or a wider Nifty universe) needs it.
PATTERN_SCAN_SHARDS = 4
CANDLE_BUFFER_MAX_1MIN_ROWS = 400  # ~6.5h of 1-min data -- comfortably covers
                                    # ATR(30) + VOLUME_LOOKBACK(20) + the
                                    # pattern's own lookback + catch-up
                                    # headroom, once resampled to 5-min bars.
                                    # This buffer only ever holds TODAY's
                                    # data now -- see HISTORICAL_BOOTSTRAP_DAYS
                                    # below for the multi-day context that's
                                    # combined with it before a pattern check.

# ── Multi-day historical bootstrap (Sep 11) ───────────────────
# Confirmed live: zero 3-Bar-Play signals fired anywhere in the universe
# for two full trading days after the WS-only migration -- root cause was
# that the candle buffer above only ever held the CURRENT day's 1-min
# data (get_live_candles_today()). Resampled, a single trading day gives
# only ~6 1-Hour bars and ~25 15-Minute bars, both well under
# ThreeBarFlagStrategy's VOLUME_LOOKBACK+4=24-bar minimum for most of
# the session -- 1-Hour could never reach it at all in a single day, and
# 15-Minute only barely qualified in the last few minutes. The old REST
# scanner never had this problem: it fetched a 5d/15d period per
# timeframe every scan. This fixes it by bootstrapping a wide multi-day
# window ONCE per symbol per day (get_live_candles_before_today()),
# resampling immediately into all three timeframes, and keeping only
# that small resampled cache in memory -- NOT the raw multi-day 1-min
# data (500 symbols x weeks of 1-min rows would be hundreds of MB; the
# resampled bars are a small fraction of that).
HISTORICAL_BOOTSTRAP_DAYS = 20  # wide net -- confirmed live that WS uptime
                                 # history is patchy (some days have near-
                                 # zero rows from past reliability issues),
                                 # so a plain "last 5 trading days" window
                                 # isn't safe; 20 calendar days comfortably
                                 # survives that and still covers 1-Hour's
                                 # 24-bar minimum many times over.
HISTORICAL_BOOTSTRAP_BUDGET_PER_CYCLE = 25  # new-symbol bootstraps allowed
                                 # per pattern-scan cycle -- paces the
                                 # one-time multi-day fetch across roughly
                                 # the first ~20 minutes after a restart
                                 # instead of firing ~500 heavier queries
                                 # at the Basic-tier (5 DTU) DB at once.
                                 # A symbol not yet bootstrapped just falls
                                 # back to today-only data (today's
                                 # original behavior) until its turn comes.
HISTORICAL_BARS_MAX_ROWS = 300  # defensive per-timeframe cap after
                                 # resampling, on top of the day-window above

# ── Tick-staleness watchdog (Sep 8) ───────────────────────────
# Confirmed live, Sep 8: the daily token expired ~5 AM IST, the WS
# handshake failed with 401, and the Upstox SDK's own auto_reconnect
# never called autoReconnectStopped -- it just went silent. Nothing
# in this file noticed, because _need_restart is ONLY ever set from
# that callback (or a crash in the connect thread itself). The
# listener sat "Healthy" (the container's liveness probe only checks
# the process is alive, not the socket) with a dead feed for the
# entire trading day, even though a fresh token was saved to Azure
# SQL at ~9:00 AM once the day's login ran -- this process never got
# as far as looking for it. This watchdog is independent of any SDK
# callback: if the market is open and no tick has arrived for
# WS_STALE_THRESHOLD_SEC, force a reconnect ourselves.
WS_STALE_THRESHOLD_SEC = 180

# Confirmed live, Sep 23: the watchdog above was gated on
# is_market_hours() -- the ALGO window (09:45-15:15), not the exchange
# session -- so a feed that died overnight (last tick ~23:30 the night
# before) sat undetected through the whole 09:15-09:45 opening half
# hour, and every symbol started the day with no candles until the
# 09:45 forced reconnect. The watchdog now runs over the real exchange
# session, and one proactive reconnect is made just before the open
# (after the ~09:00 daily login has saved the day's fresh token), so
# the socket is normally already live with a valid token at 09:15
# instead of being discovered dead 3 minutes later.
PRE_OPEN_RECONNECT_START = dtime(9, 8)
PRE_OPEN_RECONNECT_END   = dtime(9, 15)


def build_subscription_universe() -> list[dict]:
    """
    Resolve configs.universe's Nifty500+F&O+Index list to Upstox
    instrument keys. Commodities (GC=F etc.) have no Upstox equity
    key and are skipped here — they already run on yfinance only
    (see upstox_provider.py's dead MCX auto-detection).
    """
    _load_instruments()
    instruments = get_all_instruments_extended()
    resolved: dict[str, dict] = {}
    for inst in instruments:
        symbol = inst.get("symbol")
        if not symbol or symbol in resolved:
            continue
        key = get_instrument_key(symbol)
        if key:
            resolved[symbol] = {"instrument_key": key, "category": inst.get("category", "STOCK")}

    log.info(f"WS universe resolved: {len(resolved)} of "
             f"{len(instruments)} instruments have an Upstox key")
    return [{"symbol": s, "instrument_key": v["instrument_key"], "category": v["category"]}
            for s, v in resolved.items()]


class WSListener:

    def __init__(self):
        self._key_to_symbol: dict[str, str] = {}
        self._symbol_to_key: dict[str, str] = {}
        self._pending: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._streamer = None
        self._need_restart = False
        self._connect_generation = 0  # see _connect_once()'s pileup guard
        self._connect_lock = threading.Lock()  # guards generation bump + streamer assignment
        self._paper_trader = None  # lazy, see _get_paper_trader()
        self._paper_trader_lock = threading.Lock()
        self._candle_buffers: dict = {}    # symbol -> 1-min OHLCV DataFrame (rolling, TODAY only)
        self._buffer_last_ts: dict = {}    # symbol -> timestamp of that buffer's newest row
        self._candle_buffer_day = None     # calendar date the above buffers were built for --
                                            # see _refresh_today_buffer()'s day-rollover reset
        self._no_data_warned: set = set()  # symbols already logged as having zero candle data
                                            # today (Sep 16) -- warn once, not every 60s cycle
        self._historical_bars: dict = {}   # symbol -> {rule: DataFrame} resampled bars from
                                            # before today -- see _get_historical_bars()
        self._historical_bootstrap_day = None  # calendar date the above cache was built for
        # Per-SHARD budget (enterprise Phase 3, 2026-09-21) -- was a single
        # shared scalar, fine when one loop covered the whole universe.
        # Once the pattern scan is sharded into N parallel loops (see
        # run_forever()), each shard resets its own budget independently at
        # the top of its own cycle; a shared scalar would have let one
        # shard's reset clobber another shard's still-in-progress budget.
        self._historical_bootstrap_budget: dict[int, int] = {}
        self._last_tick_ts: float = time.time()  # last time ANY WS message arrived
        self._symbol_to_category: dict[str, str] = {}
        self._trend_engine = None  # lazy StrategyEngine("3 Bar Play"), see _get_trend_engine()
        self._trend_engine_lock = threading.Lock()

    # --------------------------------------------------------
    # connection
    # --------------------------------------------------------
    def _build_streamer(self, instrument_keys: list[str]):
        import upstox_client

        token = get_token()
        if not token:
            _send_ops_alert(
                "no_token",
                "No valid Upstox token — live market data is down. "
                "Run scripts/upstox_login.py (tokens expire daily at 3:30 AM IST).",
            )
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
        # Best-effort cleanup of whatever connection (if any) is being
        # replaced -- prevents a hung old streamer/thread from lingering
        # forever across repeated forced reconnects (see the watchdog).
        old_streamer = self._streamer
        if old_streamer is not None:
            try:
                old_streamer.disconnect()
            except Exception:
                pass

        self._last_tick_ts = time.time()

        # Reconnect-pileup guard (Sep 11 review) -- the disconnect() above
        # only affects a streamer that's already been ASSIGNED to
        # self._streamer. If the previous attempt's thread is still stuck
        # earlier -- inside _build_streamer()'s get_token() DB call, or the
        # initial handshake -- there's nothing yet to disconnect, so a
        # second connect thread would run in parallel with the first and
        # nothing would stop it. A generation counter fixes this: each
        # call here supersedes any earlier one, and a stale thread checks
        # (right before committing to the blocking .connect() loop)
        # whether a newer attempt has since started -- if so it abandons
        # itself instead of clobbering self._streamer out of order.
        with self._connect_lock:
            self._connect_generation += 1
            my_generation = self._connect_generation

        def _run():
            try:
                log.info(f"connecting WS — {len(instrument_keys)} instruments, mode=full")
                streamer = self._build_streamer(instrument_keys)
                # Check-and-assign must be one atomic step under the same
                # lock the generation bump above uses -- otherwise a
                # stale thread's check here can pass just before a newer
                # attempt bumps the generation, and its assignment below
                # can still land AFTER the newer attempt's, overwriting
                # the real live streamer with this superseded one (leaked
                # socket/thread, and duplicate _on_message delivery from
                # two simultaneously-live streams).
                with self._connect_lock:
                    if my_generation != self._connect_generation:
                        log.warning(f"connect attempt (gen {my_generation}) superseded before "
                                    f"connecting -- abandoning in favor of the newer attempt")
                        try:
                            streamer.disconnect()
                        except Exception:
                            pass
                        return
                    self._streamer = streamer
                self._streamer.connect()  # may block this thread indefinitely — that's fine
            except Exception as e:
                log.error(f"WS connect thread crashed: {e}")
                _send_ops_alert("connect_crashed", f"Connect attempt crashed: {e}")
                self._need_restart = True

        threading.Thread(target=_run, daemon=True, name="ws-listener-connect").start()

    # --------------------------------------------------------
    # event handlers (called from the SDK's own thread(s))
    # --------------------------------------------------------
    def _on_message(self, message) -> None:
        self._last_tick_ts = time.time()
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
        _send_ops_alert(
            "reconnect_exhausted",
            "Auto-reconnect exhausted — live market data has stopped. Most likely "
            "the Upstox token expired; run scripts/upstox_login.py to restore it.",
        )
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
        # Read-and-mutate must happen under ONE lock acquisition: this
        # dict is the same object sitting in self._pending, which
        # _flush_loop can read and hand to db.upsert_live_candles()
        # concurrently from another thread. Splitting the read and the
        # mutation across two separately-locked (or unlocked) steps let
        # a flush serialize a half-updated row (e.g. close bumped but
        # high/low not yet), producing an invalid close>high/low<close
        # candle in live_candles_1min with no error.
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
            # Sep 16 -- this cycle body used to have NO exception
            # handling at all, unlike _breakout_watch_loop/
            # _pattern_scan_loop which both wrap theirs. db.upsert_
            # live_candles() catches internally today, so this was
            # low-risk in practice, but any future change touching row/
            # dict handling here (e.g. a malformed row) would silently
            # and permanently kill candle flushing for the rest of the
            # process's life -- no crash, no log, no alert. Matching
            # the other two loops' defensive pattern.
            try:
                now_minute = datetime.now(IST).replace(second=0, microsecond=0)
                with self._lock:
                    # Shallow-copy each row while still holding the lock --
                    # these dicts are mutated in place by _extract_candle
                    # on later ticks, and handing out the live references
                    # (instead of a snapshot) let a tick mutate a row while
                    # db.upsert_live_candles() below was still serializing
                    # it, unlocked, on this thread.
                    rows = [dict(row) for row in self._pending.values()]
                    # Evict candles whose minute has already closed -- they
                    # were flushed at least once already and won't receive
                    # any more ticks (a live tick for a new minute replaces
                    # the dict entry instead of mutating this one; see
                    # _extract_candle). Without this, an instrument whose
                    # feed silently dies keeps the SAME stale row here
                    # forever, and it gets re-flushed every cycle with a
                    # bumped updated_at but unchanged ts/close -- making a
                    # dead price look fresh to any staleness check keyed
                    # off updated_at instead of the candle's own ts.
                    for key, row in list(self._pending.items()):
                        if row["ts"] < now_minute:
                            del self._pending[key]
                if not rows:
                    continue
                if db.upsert_live_candles(rows):
                    log.debug(f"flushed {len(rows)} candle rows")
                else:
                    log.warning(f"flush failed for {len(rows)} rows — will retry next cycle")
            except Exception as e:
                log.warning(f"flush cycle failed: {e}")

    # --------------------------------------------------------
    # fast breakout watch (main-process thread, Sep 2)
    # --------------------------------------------------------
    # A pattern strategy (currently "3 Bar Play") can find a valid
    # flagpole+consolidation setup on the normal 5-min scan whose
    # breakout hasn't happened yet -- surfaced as Watch_* indicators
    # and upserted into `pending_breakouts` by strategy_engine.py.
    # This loop watches just those symbols (usually none, sometimes a
    # handful) every BREAKOUT_WATCH_INTERVAL_SEC, using the same live
    # in-memory candle data _flush_loop persists, instead of waiting
    # up to 5 more minutes for the next full-market scan to notice.
    def _get_paper_trader(self):
        """
        Lazy singleton, mirrors signal_scheduler.py's _get_paper_monitor()
        -- only caches on SUCCESS, retries construction every call if it
        previously failed, so one transient failure doesn't permanently
        disable the fast breakout watch for the rest of the day.

        Locked (Sep 11 review) -- the pattern-scan loop and the
        breakout-watch loop both start ~simultaneously in run_forever()
        (each sleeps its own interval before its first tick) and both
        call this on first use; without a lock, both could see
        self._paper_trader is None at the same time and each construct
        their own PaperTrader/SandboxClient, silently discarding one.
        Not previously harmful in practice (the RMS/SandboxClient state
        that matters is either a module-level singleton or re-read per
        call), but exactly the kind of duplicate-instance pattern this
        codebase has already been bitten by once (the stale sandbox
        client bug, Sep 10) -- cheap to close off properly.
        """
        with self._paper_trader_lock:
            if self._paper_trader is None:
                try:
                    from core.execution.paper_trader import PaperTrader
                    from data.providers.upstox_provider import UpstoxProvider
                    self._paper_trader = PaperTrader(provider=UpstoxProvider())
                except Exception as e:
                    log.warning(f"breakout-watch PaperTrader construction failed "
                                f"(will retry next cycle): {e}")
                    return None
            return self._paper_trader

    def _breakout_watch_loop(self) -> None:
        while True:
            time.sleep(BREAKOUT_WATCH_INTERVAL_SEC)
            try:
                self._check_pending_breakouts()
            except Exception as e:
                log.warning(f"breakout-watch cycle failed: {e}")

    def _check_pending_breakouts(self) -> None:
        db.expire_stale_pending_breakouts()
        pending = db.get_active_pending_breakouts()
        if not pending:
            return

        for row in pending:
            instrument_key = self._symbol_to_key.get(row["symbol"])
            if not instrument_key:
                continue  # not in this listener's subscription universe

            # Copy the row (not just the reference) while still under the
            # lock -- _extract_candle mutates this same dict in place on
            # later ticks, and reading high/low after releasing the lock
            # could read a torn pair (e.g. high already bumped by a new
            # tick, low not yet), mis-detecting the breakout cross.
            with self._lock:
                live = self._pending.get(instrument_key)
                if live is not None:
                    live = dict(live)
            if not live:
                continue  # no live tick for this symbol yet this session

            side    = row["side"]
            trigger = float(row["trigger_price"])
            crossed = (side == "BUY"  and float(live["high"]) >= trigger) or \
                      (side == "SELL" and float(live["low"])  <= trigger)
            if not crossed:
                continue

            # Defense in depth alongside the same check in
            # _scan_universe_for_patterns -- this row's own TTL (default
            # 15 min, see upsert_pending_breakout) already makes a
            # multi-day-stale watch unlikely here, but it's cheap
            # insurance against the same "stale breakout candle" class
            # of bug regardless of which path a signal comes through.
            if _is_stale_breakout(db.anatomy_from_json(row.get("anatomy_json"))):
                # Row is still PENDING/RETRY here (not yet claimed) --
                # leave it for expire_stale_pending_breakouts()'s normal
                # TTL sweep rather than force-updating a status this
                # function has no claim on yet.
                log.warning(f"breakout-watch: {row['symbol']}/{row['timeframe']} crossed but its "
                            f"anatomy is from a prior session -- not triggering, letting it expire")
                continue

            # Claim atomically, but only mark a watch triggered after the
            # broker accepts its order. Upstox Sandbox can intermittently
            # return 401 for a token that succeeds again later; consuming a
            # watch before that call made the valid signal unrecoverable.
            if not db.claim_pending_breakout(row["id"]):
                continue

            outcome = self._act_on_breakout(row)
            if outcome.get("action") == "opened":
                final_status = "TRIGGERED"
            elif (outcome.get("action") == "error" and
                  str(outcome.get("reason", "")).startswith("sandbox:")):
                # Bounded by its existing expires_at and rechecked against
                # live price before each retry.
                final_status = "RETRY"
            else:
                final_status = "CANCELLED"
            # Sep 16 -- mark_pending_breakout()'s return value used to be
            # discarded entirely. Its UPDATE only matches a row still in
            # 'EXECUTING' (see claim_pending_breakout); if anything else
            # raced it back to a different status in between (e.g. a
            # concurrent pattern-scan cycle re-upserting the same
            # symbol/strategy/timeframe key), this call silently affects
            # zero rows and the outcome above is lost with no trace --
            # exactly the kind of stuck-row state that took a manual DB
            # query to diagnose live (Sep 16, ABSLAMC.NS). Not a crash,
            # not fatal (expire_stale_pending_breakouts() still reclaims
            # it once its TTL passes), but worth surfacing immediately
            # rather than only via its eventual expiry.
            if not db.mark_pending_breakout(row["id"], final_status):
                log.warning(f"mark_pending_breakout({row['id']}, {final_status}) affected 0 rows for "
                            f"{row['symbol']} -- likely raced by a concurrent writer; row may sit stale "
                            f"until its TTL expires it")

    def _act_on_breakout(self, row: dict) -> dict:
        # Sep 16: restore the flagpole/consolidation/breakout candles
        # upsert_pending_breakout() carried through in anatomy_json (if
        # this row was registered after that fix), so a BREAKOUT-WATCH
        # trigger can show the same full detail as a direct PATTERN-SCAN
        # signal instead of nothing. None for any row registered before
        # the fix shipped, or if it was never populated for some reason
        # -- _execute_trade()/the alert already handle anatomy=None.
        anatomy = db.anatomy_from_json(row.get("anatomy_json"))
        return self._execute_trade(
            symbol=row["symbol"], strategy=row["strategy"], timeframe=row["timeframe"],
            side=row["side"], price=float(row["trigger_price"]),
            stop=float(row["stop_loss"]), target=float(row["target"]),
            strength=row.get("strength"),
            reason=f"3-Bar Play fast breakout watch: price crossed {float(row['trigger_price']):.2f} "
                   f"within ~1 min of the flagpole breakout level.",
            source_label="BREAKOUT-WATCH",
            anatomy=anatomy,
        )

    def _execute_trade(
        self, symbol: str, strategy: str, timeframe: str, side: str,
        price: float, stop: float, target: float, strength: str | None,
        reason: str, source_label: str, anatomy: dict | None = None,
    ) -> dict:
        """
        Shared trade-execution path for both the price-level watch
        (_act_on_breakout, driven by pending_breakouts rows the normal
        5-min scan wrote) and the full-universe pattern scan
        (_pattern_scan_loop, Sep 7 -- detects AND confirms breakouts
        itself every ~1 min, for setups that complete faster than the
        5-min scan can ever see an intermediate "watching" state).
        Fills at the given price (the strategy's own computed level),
        logs the signal, and sends the Telegram alert via the same
        alert_states table the normal scan uses, so whichever path
        acts first naturally suppresses the other from re-firing.

        `anatomy` (Sep 9) -- the flagpole/consolidation/breakout candle
        dict from ThreeBarFlagStrategy's indicators, passed through to
        PaperTrader.on_signal() for persistence (see trade_anatomy
        table). Only the pattern-scan path has this available (it just
        ran generate_signal() itself); the pending-breakouts watch path
        doesn't carry raw candle data, so it's None there -- acceptable
        since that path is the exception now, not the primary one.
        """
        pt = self._get_paper_trader()
        if pt is None:
            log.warning(f"{source_label}: {symbol} crossed but PaperTrader unavailable — skipped")
            return {"action": "error", "reason": "paper trader unavailable"}

        outcome = pt.on_signal(
            symbol=symbol, side=side, price=price,
            strategy=strategy, timeframe=timeframe,
            strength=strength, custom_stop=stop, custom_target=target,
            anatomy=anatomy,
        )
        log.info(f"{source_label}  {symbol}  [{strategy}]  {side} @ {price}  -> {outcome}")

        if outcome.get("action") == "error":
            # Confirmed live, Sep 10: a genuine BUY/SELL was detected and
            # triggered correctly, but every single downstream attempt
            # failed silently for over an hour (a stale sandbox client in
            # this long-running singleton) -- nothing surfaced anywhere
            # except a container log line nobody was watching. "error"
            # (unlike "reject", which covers expected, high-frequency
            # outcomes like a full position cap) means something is
            # actually broken, not just a normal pass -- worth a direct,
            # rate-limited ops alert rather than silence.
            # Sep 16, Om: "have a full depth analysis of what's happening
            # with this 401 error" -- include the exact flagpole/
            # consolidation/breakout candles the strategy detected, so a
            # failed-execution alert shows precisely what pattern was
            # found (and when), not just "signal fired but failed."
            anatomy_suffix = ""
            if anatomy:
                try:
                    from core.alerts.alert_manager import _format_anatomy_block
                    block = _format_anatomy_block(anatomy)
                    if block:
                        anatomy_suffix = f"\n{block}"
                except Exception:
                    pass
            _send_ops_alert(
                "signal_execution_error",
                f"{source_label}: {symbol} {side} signal fired but failed to execute — "
                f"{outcome.get('reason', 'unknown error')} [{timeframe}]{anatomy_suffix}",
            )

        if outcome.get("action") != "opened":
            return outcome  # rejected/skipped/error — nothing further to log/alert

        from core.logger.signal_logger import SignalLogger
        from core.alerts.alert_manager import AlertManager
        from core.strategies.base_strategy import SignalResult

        # Sep 17 -- log_trade_signal(), NOT log_signal(). Same bug class
        # as the alert_states fix just below: log_signal()'s own dedup
        # (skip if the last logged signal for this symbol+timeframe+
        # strategy was already the same direction) silently ate M&M.NS's
        # entry into the `signals` table today -- its last recorded
        # signal was 'BUY' from 2026-07-27, so a genuinely new trade
        # opened today was treated as "no change" and never logged at
        # all. This call only ever happens after on_signal() has already
        # confirmed a brand-new position, so there's nothing left to
        # de-duplicate here either.
        SignalLogger().log_trade_signal(
            stock=symbol, timeframe=timeframe, signal=side,
            rsi=0.0, price=price, strategy=strategy,
        )

        # Sep 17 -- use send_trade_alert(), NOT check_alert(). This call
        # only ever happens after on_signal() already confirmed a
        # genuinely NEW position was just opened (the existing-position
        # guard inside on_signal() rejects any duplicate/racing attempt
        # for the same symbol before it ever gets here as "opened") --
        # there is nothing left to de-duplicate. check_alert()'s own
        # previous-signal dedup exists for continuously-polled strategies
        # (RSI Reversal etc.) that call it every scan cycle regardless of
        # outcome; using it here was actively wrong. Confirmed live:
        # M&M.NS opened a real new BUY today, but alert_states still had
        # 'BUY' recorded from an unrelated signal on 2026-07-27 -- almost
        # two months earlier -- so check_alert()'s "previous == current"
        # check silently swallowed the alert for a trade that clearly
        # should have been announced. send_trade_alert() still records
        # alert_state afterward (consistency/history), just never lets
        # it gate whether the message actually sends. No trend/RSI
        # enrichment here (that's a normal-scan-only step) -- the
        # message renders with neutral trend arrows, an accepted
        # simplification for this fast path. `anatomy` (Sep 16) still
        # passes through when available so 3 Bar Play alerts show the
        # full flagpole/consolidation/breakout candle detail.
        signal_result = SignalResult(side, strength or "MODERATE", reason, {}, strategy)
        AlertManager().send_trade_alert(
            timeframe=timeframe, stock=symbol, current_signal=side,
            rsi=0.0, price=price, strategy=strategy,
            signal_result=signal_result, data_source="upstox_ws",
            anatomy=anatomy,
        )

        return outcome

    # --------------------------------------------------------
    # full-universe pattern scan (Sep 7) -- see PATTERN_SCAN_INTERVAL_SEC's
    # docstring above for why this exists.
    # --------------------------------------------------------
    # 3 Bar Play's active timeframes (must match signal_scheduler.py's
    # THREE_BAR_PLAY_TIMEFRAMES) and their resample rules. Sep 9 -- this
    # loop used to only check "5 Minutes"; 15-min/1-hour breakouts still
    # depended entirely on the slow REST scanner, with no fast safety
    # net at all (confirmed: NEULANDLAB.NS's 1-Hour trade sat 22 hours
    # stale for exactly this reason). Now the sole path for all three.
    PATTERN_SCAN_TIMEFRAMES = [("5 Minutes", "5min"), ("15 Minutes", "15min"), ("1 Hour", "1h")]

    def _refresh_today_buffer(self, symbol: str):
        """
        Fetches/updates the rolling per-symbol 1-min TODAY buffer via a
        cheap incremental DB read. Called ONCE per symbol per pattern-
        scan cycle (Sep 11 fix -- this used to be embedded inside
        _get_candles(), which was called once PER TIMEFRAME, i.e. 3x per
        symbol per cycle. Each call issued its own DB query even though
        2 of the 3 always returned zero new rows within the same cycle,
        since the first call had already advanced _buffer_last_ts -- 1500
        queries/min instead of the intended 500 against the Basic-tier
        (5 DTU) DB this whole design was built to protect). Returns the
        buffer DataFrame, or None if there's no data at all yet today.
        """
        import pandas as pd
        from datetime import date

        # Day-rollover reset (Sep 16) -- this process is designed to run
        # all day, every day, with no scheduled restart (see module
        # docstring), but this buffer had no day-boundary check at all,
        # unlike _historical_bars (which does, see _get_historical_bars).
        # Without it, a process surviving past midnight would keep
        # appending onto yesterday's tail via get_live_candles_since()
        # and resample "today's" data from a mix of two calendar days --
        # get_live_candles_before_today() would then ALSO include
        # yesterday's rows, double-counting them into the pattern check.
        today = date.today()
        if self._candle_buffer_day != today:
            self._candle_buffers = {}
            self._buffer_last_ts = {}
            self._candle_buffer_day = today
            self._no_data_warned = set()

        buf = self._candle_buffers.get(symbol)
        if buf is None:
            buf = db.get_live_candles_today(symbol)
            if buf.empty:
                return None
        else:
            last_ts = self._buffer_last_ts.get(symbol)
            new_rows = db.get_live_candles_since(symbol, last_ts) if last_ts is not None else pd.DataFrame()
            if not new_rows.empty:
                buf = pd.concat([buf, new_rows], ignore_index=True)

        if len(buf) > CANDLE_BUFFER_MAX_1MIN_ROWS:
            buf = buf.iloc[-CANDLE_BUFFER_MAX_1MIN_ROWS:].reset_index(drop=True)

        self._candle_buffers[symbol] = buf
        self._buffer_last_ts[symbol] = buf["Datetime"].iloc[-1]
        return buf

    def _get_historical_bars(self, symbol: str, shard_id: int) -> dict:
        """
        Lazily bootstraps and caches this symbol's pre-today candles,
        resampled into all three PATTERN_SCAN_TIMEFRAMES -- see
        HISTORICAL_BOOTSTRAP_DAYS's module-level docstring for the bug
        this fixes (1-Hour/15-Minute could never accumulate enough
        same-day bars to pass ThreeBarFlagStrategy's minimum-data check).

        Paced via _historical_bootstrap_budget[shard_id] (reset once per
        cycle, per shard, by _scan_universe_for_patterns(), NOT here) so a
        cold start doesn't fire ~500 heavier multi-day queries at once -- a
        symbol that hasn't had its shard's turn yet just returns {} (falls
        back to today-only data, today's original behavior) until its budget
        today-only data, today's original behavior) until its budget
        comes up in a later cycle, typically within ~20 minutes of a
        restart. The cache itself (which symbols are already
        bootstrapped) persists across cycles within the same day --
        only cleared on a genuine calendar-day change, when "before
        today" widens by a day.
        """
        from datetime import date
        today = date.today()
        if self._historical_bootstrap_day != today:
            self._historical_bars = {}
            self._historical_bootstrap_day = today

        if symbol in self._historical_bars:
            return self._historical_bars[symbol]

        if self._historical_bootstrap_budget.get(shard_id, 0) <= 0:
            return {}  # not this symbol's shard's turn yet this cycle

        self._historical_bootstrap_budget[shard_id] -= 1
        from data.providers.upstox_provider import resample_ohlc

        bars: dict = {}
        try:
            raw = db.get_live_candles_before_today(symbol, days=HISTORICAL_BOOTSTRAP_DAYS)
            if not raw.empty:
                for _, rule in self.PATTERN_SCAN_TIMEFRAMES:
                    r = resample_ohlc(raw.copy(), rule)
                    if r is not None and not r.empty:
                        if len(r) > HISTORICAL_BARS_MAX_ROWS:
                            r = r.iloc[-HISTORICAL_BARS_MAX_ROWS:].reset_index(drop=True)
                        bars[rule] = r
        except Exception as e:
            # Do NOT cache on failure (a transient DB hiccup) -- caching
            # {} here would permanently mark this symbol "bootstrapped"
            # for the rest of the day with no multi-day history at all,
            # silently reintroducing the under-24-bar starvation bug
            # HISTORICAL_BOOTSTRAP_DAYS exists to fix. Leaving it
            # uncached means the next cycle retries it (consuming
            # another budget slot), same as a symbol that hasn't had
            # its turn yet.
            log.warning(f"historical-bars bootstrap failed for {symbol} (will retry next cycle): {e}")
            return {}

        self._historical_bars[symbol] = bars
        return bars

    def _get_candles(self, symbol: str, rule: str, shard_id: int, today_buf=None):
        """
        Resamples `rule` (e.g. "5min"/"15min"/"1h") from the combination
        of this symbol's cached pre-today bars and its live today-buffer
        -- the shape ThreeBarFlagStrategy needs. `today_buf` is
        _refresh_today_buffer()'s result, passed in by the caller so the
        DB read happens once per symbol per cycle, not once per
        timeframe. Returns None if there's not yet enough combined
        history -- the strategy itself already handles "insufficient
        data" gracefully, so callers can just skip.
        """
        import pandas as pd
        from data.providers.upstox_provider import resample_ohlc

        if today_buf is None or today_buf.empty:
            return None

        today_resampled = resample_ohlc(today_buf.copy(), rule)
        historical = self._get_historical_bars(symbol, shard_id).get(rule)

        if historical is not None and not historical.empty:
            if today_resampled is not None and not today_resampled.empty:
                combined = pd.concat([historical, today_resampled], ignore_index=True)
            else:
                combined = historical
        else:
            combined = today_resampled

        return combined if combined is not None and not combined.empty else None

    def _get_trend_engine(self):
        """
        Lazy singleton, mirrors _get_paper_trader() -- only caches on
        success. Used solely to reuse StrategyEngine._enrich_once()'s
        stock trend/RSI fetch (see _apply_trend_grading below), so the
        WS pattern-scan grades signals with the same nifty/stock-trend
        context the REST scanner used to, now that it's this strategy's
        sole detection path (Sep 9). Only ever called from the
        pattern-scan thread today, so this lock is defensive/cheap
        insurance rather than a fix for an observed race.
        """
        with self._trend_engine_lock:
            if self._trend_engine is None:
                try:
                    from core.engine.strategy_engine import StrategyEngine
                    self._trend_engine = StrategyEngine("3 Bar Play")
                except Exception as e:
                    log.warning(f"trend engine construction failed (will retry next cycle): {e}")
                    return None
            return self._trend_engine

    def _apply_trend_grading(self, symbol: str, timeframe: str, df, result):
        """
        Ports the REST scanner's suppression/grading step (see
        strategy_engine.py's _scan_multi, ~line 770-808) into this path
        -- only called when a signal actually fires (rare), so it adds
        no per-symbol-per-cycle REST cost despite using REST-backed
        trend/RSI fetches. Mutates `result` in place (strength) and
        returns False if the signal should be suppressed entirely.
        """
        from core.indicators.indicators import add_rsi, should_suppress_signal, calculate_signal_strength
        from core.engine.strategy_engine import get_nifty_all_trends
        from data.providers.upstox_provider import UpstoxProvider

        engine = self._get_trend_engine()
        if engine is None:
            return True  # best-effort -- don't block a real trade on this failing

        provider = UpstoxProvider()
        category = self._symbol_to_category.get(symbol, "STOCK")
        nifty_trends = get_nifty_all_trends(provider)  # cached per calendar day -- cheap
        nifty_trend = nifty_trends.get("daily", "NEUTRAL")
        stock_trends = engine._get_stock_all_trends(provider, symbol)
        stock_trend = stock_trends.get("daily", "NEUTRAL")

        if should_suppress_signal(result.signal, nifty_trend, stock_trend):
            return False

        try:
            df_with_rsi = add_rsi(df.copy())
            rsi_val = round(float(df_with_rsi["RSI"].iloc[-1]), 2)
        except Exception:
            rsi_val = 50.0

        result.strength = calculate_signal_strength(
            signal=result.signal, nifty_trend=nifty_trend, stock_trend=stock_trend,
            volume_ratio=result.indicators.get("Volume_Ratio", 0.0), tf_name=timeframe,
            nifty_hourly=nifty_trends.get("hourly", "NEUTRAL"), nifty_5min=nifty_trends.get("5min", "NEUTRAL"),
            stock_hourly=stock_trends.get("hourly", "NEUTRAL"), stock_5min=stock_trends.get("5min", "NEUTRAL"),
            rsi_val=rsi_val,
        )
        return True

    def _pattern_scan_loop(self, shard_id: int, symbols: list[str]) -> None:
        # Enterprise Phase 3 (2026-09-21): each shard runs this loop on its
        # own thread, covering only `symbols` (a disjoint slice of the full
        # universe -- see run_forever()'s sharding). Total scan-cycle wall
        # time then scales with shard size, not total universe size, which
        # is what actually lets the instrument count grow past where a
        # single 60s-cadence loop over everything would start lagging.
        while True:
            time.sleep(PATTERN_SCAN_INTERVAL_SEC)
            try:
                self._scan_symbols_for_patterns(shard_id, symbols)
            except Exception as e:
                log.warning(f"pattern-scan cycle failed (shard {shard_id}): {e}")

    def _scan_symbols_for_patterns(self, shard_id: int, symbols: list[str]) -> None:
        from core.strategies.strategies import ThreeBarFlagStrategy
        from core.engine.strategy_engine import _compute_catchup_n, _record_scan_progress
        from core.scheduler.signal_scheduler import is_market_hours

        # Confirmed live, Sep 7: right after a post-market deploy, this
        # loop's bootstrap read (get_live_candles_today, a full day of
        # already-closed candles) found BLUEJET.NS's pattern already
        # complete on the last (stale, hours-old) candle and tried to
        # act on it immediately -- only harmless because the sandbox
        # token happened to be invalid at that moment. Gate the whole
        # scan to the same trading window the real scanner uses, so a
        # stale end-of-day candle from before this process started (or
        # simply overnight/weekend idling) can never be mistaken for a
        # live breakout.
        if not is_market_hours():
            return

        # Fresh historical-bootstrap budget every cycle (Sep 11), now keyed
        # per shard (Phase 3) -- the cache of WHICH symbols are already
        # bootstrapped persists across cycles (see _get_historical_bars),
        # but the budget itself must reset each cycle, not just once per
        # calendar day, or only the first HISTORICAL_BOOTSTRAP_BUDGET_PER_CYCLE
        # symbols in iteration order would ever get bootstrapped for the
        # entire rest of the day. Per-shard so one shard's reset can never
        # stomp another shard's still-in-progress budget for this cycle --
        # but the per-shard allotment is HISTORICAL_BOOTSTRAP_BUDGET_PER_CYCLE
        # divided across PATTERN_SCAN_SHARDS (Sep 22 fix, code review), not
        # the full constant each: giving every one of the 4 shards its own
        # full 25 let a cold restart during market hours fire up to 100
        # simultaneous historical-bootstrap queries in one cycle -- 4x the
        # burst this budget exists to cap.
        self._historical_bootstrap_budget[shard_id] = max(
            1, HISTORICAL_BOOTSTRAP_BUDGET_PER_CYCLE // PATTERN_SCAN_SHARDS
        )

        strat = ThreeBarFlagStrategy()
        strategy_name = strat.name

        for symbol in symbols:
            try:
                today_buf = self._refresh_today_buffer(symbol)
            except Exception as e:
                log.warning(f"pattern-scan buffer refresh failed for {symbol}: {e}")
                continue
            if today_buf is None:
                # Sep 16 -- this used to be a fully silent, permanent
                # per-cycle skip with zero trace anywhere if a symbol's
                # WS feed never produced a tick (subscription/key
                # mismatch, etc). Warn once per symbol per day, not
                # every 60s, so it's visible without being log spam.
                if symbol not in self._no_data_warned:
                    self._no_data_warned.add(symbol)
                    log.warning(f"pattern-scan: no candle data at all today for {symbol} -- "
                                f"skipping until a tick arrives (won't repeat this warning today)")
                continue

            for timeframe, rule in self.PATTERN_SCAN_TIMEFRAMES:
                try:
                    df = self._get_candles(symbol, rule, shard_id, today_buf=today_buf)
                    if df is None:
                        continue

                    check_n = _compute_catchup_n(df, symbol, strategy_name, timeframe)
                    result = strat.generate_signal(df.copy(), check_last_n=check_n)
                    _record_scan_progress(df, symbol, strategy_name, timeframe)

                    if result.signal in ("BUY", "SELL"):
                        if not self._apply_trend_grading(symbol, timeframe, df, result):
                            continue  # suppressed -- opposing nifty+stock trend
                        anatomy = {
                            "flagpole": result.indicators.get("Anatomy_Flagpole"),
                            "consolidation": result.indicators.get("Anatomy_Consolidation"),
                            "breakout": result.indicators.get("Anatomy_Breakout"),
                        }
                        if _is_stale_breakout(anatomy):
                            # Confirmed live, Sep 21 (MARICO.NS): between market
                            # open and this timeframe's first candle actually
                            # closing (e.g. 09:15-09:30 for a 15-min bar), the
                            # most recent COMPLETE candle in the resampled
                            # series is still the PREVIOUS session's last bar.
                            # check_last_n is correctly capped at 1 by
                            # _compute_catchup_n's day-boundary guard, but that
                            # only bounds which candle is checked AS a
                            # breakout point -- it doesn't stop that single
                            # candle itself from being a prior-session one. The
                            # pattern had already been evaluated (and correctly
                            # suppressed by trend grading) Friday afternoon;
                            # today's freshly-recomputed trend just happened to
                            # stop suppressing it, "firing" on Monday morning a
                            # breakout that actually happened, and finished,
                            # last Friday. Guard here instead of loosening the
                            # catch-up window, since the underlying candles are
                            # genuinely stale, not merely uncounted.
                            log.warning(f"pattern-scan: {symbol}/{timeframe} matched but its "
                                        f"breakout candle is from a prior session -- skipping "
                                        f"as stale rather than firing on a completed old move")
                            continue
                        # Enterprise Phase 1 (2026-09-20): enqueue only --
                        # no network call, no blocking on this loop's
                        # thread. _order_worker_loop drains trade_intents
                        # with its own concurrency and retry/backoff
                        # (attempts/RETRY/FAILED), which supersedes the old
                        # "write a pending_breakouts RETRY row on a sandbox
                        # error" fallback this branch used to need -- that
                        # retry now lives in trade_intents itself.
                        db.enqueue_trade_intent(
                            symbol=symbol, side=result.signal, strategy=strategy_name,
                            timeframe=timeframe,
                            price=result.indicators.get("Pattern_Entry"),
                            custom_stop=result.indicators.get("Pattern_Stop"),
                            custom_target=result.indicators.get("Pattern_Target_Exact"),
                            strength=result.strength, reason=result.reason,
                            anatomy=anatomy,
                        )
                    elif result.indicators.get("Watch_Entry") is not None:
                        db.upsert_pending_breakout(
                            symbol=symbol, strategy=strategy_name, timeframe=timeframe,
                            side=result.indicators["Watch_Side"],
                            trigger_price=result.indicators["Watch_Entry"],
                            stop_loss=result.indicators["Watch_Stop"],
                            target=result.indicators["Watch_Target"],
                            strength=result.indicators.get("Watch_Strength"),
                            # Sep 16: same anatomy data the confirmed-signal
                            # branch above gets (ThreeBarFlagStrategy computes
                            # both together) -- carried through pending_
                            # breakouts.anatomy_json so a LATER BREAKOUT-WATCH
                            # trigger on this exact row can still render the
                            # full flagpole/consolidation/breakout detail,
                            # not just the trigger/stop/target numbers.
                            anatomy={
                                "flagpole": result.indicators.get("Anatomy_Flagpole"),
                                "consolidation": result.indicators.get("Anatomy_Consolidation"),
                                "breakout": result.indicators.get("Anatomy_Breakout"),
                            },
                        )
                    else:
                        db.cancel_pending_breakout(symbol, strategy_name, timeframe)
                except Exception as e:
                    log.warning(f"pattern-scan failed for {symbol}/{timeframe}: {e}")

    # --------------------------------------------------------
    # order-execution worker pool (enterprise Phase 1) -- drains
    # trade_intents rows written by _scan_universe_for_patterns above,
    # fully decoupled from that loop's cadence. See ORDER_WORKER_*
    # constants' comment for why this exists.
    # --------------------------------------------------------
    def _order_worker_loop(self) -> None:
        while True:
            time.sleep(ORDER_WORKER_POLL_SEC)
            try:
                self._drain_trade_intents()
            except Exception as e:
                log.warning(f"order-worker cycle failed: {e}")

    def _drain_trade_intents(self) -> None:
        db.expire_stale_trade_intents(ORDER_INTENT_STALE_AFTER_SEC)
        intents = db.claim_next_trade_intents(ORDER_WORKER_CLAIM_BATCH)
        if not intents:
            return

        with ThreadPoolExecutor(max_workers=ORDER_WORKER_MAX_WORKERS) as pool:
            futures = {pool.submit(self._run_trade_intent, row): row for row in intents}
            for fut in as_completed(futures):
                row = futures[fut]
                try:
                    fut.result()
                except Exception as e:
                    log.error(f"trade_intent {row['id']} ({row['symbol']}) worker crashed: {e}")
                    db.mark_trade_intent_failed(row["id"], str(e))

    def _run_trade_intent(self, row: dict) -> None:
        anatomy = db.anatomy_from_json(row.get("anatomy_json"))
        outcome = self._execute_trade(
            symbol=row["symbol"], strategy=row["strategy"], timeframe=row["timeframe"],
            side=row["side"], price=float(row["price"]),
            stop=float(row["custom_stop"]) if row.get("custom_stop") is not None else float(row["price"]),
            target=float(row["custom_target"]) if row.get("custom_target") is not None else float(row["price"]),
            strength=row.get("strength"), reason=row.get("reason") or "",
            source_label="PATTERN-SCAN", anatomy=anatomy,
        )

        created_at = row.get("created_at")
        if created_at is not None:
            try:
                if created_at.tzinfo is None:
                    created_at = pytz.utc.localize(created_at)
                latency = (datetime.now(created_at.tzinfo) - created_at).total_seconds()
                record_signal_to_order_latency(latency, row["strategy"], "PATTERN-SCAN")
            except Exception as e:
                log.debug(f"latency metric failed (non-fatal): {e}")

        if outcome.get("action") == "error":
            db.mark_trade_intent_failed(row["id"], str(outcome.get("reason", "unknown error")))
        else:
            # "opened"/"rejected"/"skip" are all terminal from the
            # outbox's point of view -- a reject (e.g. cap full,
            # duplicate signal) isn't a transient failure worth
            # retrying, same posture _execute_trade already takes with
            # its own alerting (only "error" gets an ops alert).
            db.mark_trade_intent_done(row["id"])

    def run_forever(self) -> None:
        universe = build_subscription_universe()
        if not universe:
            log.error("empty subscription universe — aborting")
            return

        self._key_to_symbol = {u["instrument_key"]: u["symbol"] for u in universe}
        self._symbol_to_key = {u["symbol"]: u["instrument_key"] for u in universe}
        self._symbol_to_category = {u["symbol"]: u.get("category", "STOCK") for u in universe}
        instrument_keys = list(self._key_to_symbol.keys())

        threading.Thread(target=self._flush_loop, daemon=True, name="ws-listener-flush").start()
        threading.Thread(target=self._breakout_watch_loop, daemon=True, name="ws-listener-breakout-watch").start()

        # Enterprise Phase 3 -- split the universe into PATTERN_SCAN_SHARDS
        # disjoint symbol lists, one dedicated thread per shard, instead of
        # one thread looping the entire universe. A symbol's shard
        # assignment (index into the sorted symbol list, mod shard count)
        # is stable across restarts since it only depends on the symbol
        # list itself, not on iteration/dict order.
        all_symbols = sorted(self._symbol_to_key.keys())
        shards: list[list[str]] = [[] for _ in range(PATTERN_SCAN_SHARDS)]
        for i, sym in enumerate(all_symbols):
            shards[i % PATTERN_SCAN_SHARDS].append(sym)
        for shard_id, shard_symbols in enumerate(shards):
            if not shard_symbols:
                continue
            threading.Thread(
                target=self._pattern_scan_loop, args=(shard_id, shard_symbols),
                daemon=True, name=f"ws-listener-pattern-scan-{shard_id}",
            ).start()
            log.info(f"pattern-scan shard {shard_id}: {len(shard_symbols)} symbols")

        threading.Thread(target=self._order_worker_loop, daemon=True, name="ws-listener-order-worker").start()
        self._connect_once(instrument_keys)

        from core.scheduler.signal_scheduler import is_exchange_hours, is_market_day

        pre_open_reconnect_day = None
        while True:
            time.sleep(SUPERVISOR_TICK_SEC)
            if self._need_restart:
                log.info("rebuilding WS connection with a fresh token")
                self._need_restart = False
                self._connect_once(instrument_keys)
                continue

            now_ist = datetime.now(IST)
            if (is_market_day() and pre_open_reconnect_day != now_ist.date()
                    and PRE_OPEN_RECONNECT_START <= now_ist.time() < PRE_OPEN_RECONNECT_END):
                pre_open_reconnect_day = now_ist.date()
                log.info("pre-open reconnect — rebuilding WS connection with today's token")
                self._connect_once(instrument_keys)
                continue

            stale_for = time.time() - self._last_tick_ts
            if is_exchange_hours() and stale_for > WS_STALE_THRESHOLD_SEC:
                log.error(f"no WS ticks for {stale_for:.0f}s during market hours — "
                          f"forcing reconnect (SDK gave no callback)")
                _send_ops_alert(
                    "stale_no_ticks",
                    f"No live ticks received for over {WS_STALE_THRESHOLD_SEC // 60} min "
                    f"during market hours — forcing a reconnect with a fresh token.",
                )
                self._connect_once(instrument_keys)
