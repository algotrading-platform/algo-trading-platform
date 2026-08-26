# ============================================================
# core/engine/strategy_engine.py
#
# FIXES (2026-06-19):
#   - Fetches Nifty D/H/5m trends correctly per Jwala spec
#   - Fetches Stock D/H/5m trends correctly per Jwala spec
#   - Passes all trend values to calculate_signal_strength
#   - Stores trend values in indicators for alert message
#   - Added timeout to ThreadPoolExecutor (fixes job hanging)
# ============================================================

import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import pytz

from core.strategies.strategies import get_strategy, STRATEGY_NAMES
from core.strategies.arbitrage_strategy import ArbitrageStrategy, arbitrage_strategy
from core.indicators.indicators import (
    add_rsi,
    get_nifty_trend,
    get_nifty_hourly_trend,
    get_nifty_5min_trend,
    get_stock_daily_trend,
    get_stock_hourly_trend,
    get_stock_5min_trend,
    get_daily_trend,
    calculate_signal_strength,
    should_suppress_signal,
    get_multi_timeframe_trend,
    get_volume_spike_ratio,
    get_volume_spike_label,
)
from core.logger.signal_logger import SignalLogger
from core.alerts.alert_manager import AlertManager
from core.backtesting.rsi_backtest import RSIBacktest
from core.backtesting.backtest_store import write_result

log = logging.getLogger("strategy_engine")
IST = pytz.timezone("Asia/Kolkata")

# ── Paper trading integration (lazy singleton) ───────────────
_paper_trader = None

def _get_paper_trader(provider):
    """Lazily create one shared PaperTrader. Returns None if unavailable
    (e.g. sandbox token not set) so signal generation is never affected."""
    global _paper_trader
    if _paper_trader is None:
        try:
            from core.execution.paper_trader import PaperTrader
            _paper_trader = PaperTrader(provider=provider)
        except Exception as e:
            log.warning(f"PaperTrader unavailable — paper trading disabled: {e}")
            _paper_trader = False  # sentinel: tried and failed
    return _paper_trader or None

def _run_paper_trading(provider, results):
    """
    Open positions for newly-alerted BUY/SELL signals. Called once per
    scan, single-threaded, after the scan's thread pool has joined.

    Signal-grade gating REMOVED (Jwala, Jul 23: "Now let's include all
    signals no filtering we'll take weak signals to."). Was added Jul
    11 ("we can skip the weak ones"), then explicitly put under
    reconsideration Jul 17 after a win-rate drop ("let's run it for a
    day. And if this doesn't work, then we'll try to incorporate even
    the weak one") — this is that reconsideration resolving to "yes,
    include WEAK." Every BUY/SELL signal is now eligible regardless of
    grade; `strength` is still passed through to on_signal() since it
    drives unit-based position sizing in RMS (MODERATE=1/STRONG=2/VERY
    STRONG=3 units) — that sizing logic is unrelated to and unaffected
    by removing this gate.
    """
    pt = _get_paper_trader(provider)
    if pt is None:
        return
    for r in (results or []):
        # Only act on signals that just ALERTED (a real transition),
        # matching what we send to Telegram. Skip HOLD / repeats.
        if not r.get("alerted"):
            continue
        if r.get("signal") not in ("BUY", "SELL"):
            continue
        try:
            # Pattern-based strategies (3 Bar Play, Jul 24) can supply
            # their own natural stop level via "Pattern_Stop" in their
            # indicators — generic extraction, not gated on strategy
            # name, so any future pattern strategy gets the same
            # treatment automatically just by including this key.
            # "Pattern_Target_Exact" (Aug 26) is the same idea for an
            # exact target level, e.g. 3 Bar Play's 70%-of-flagpole
            # target. Deliberately a DIFFERENT key from the pre-existing
            # "Pattern_Target" that Experiment 3 Bar Play also sets —
            # that one is explicitly documented as reference/display
            # only (see ThreeBarPlayStrategy's docstring), and must stay
            # untouched rather than suddenly become enforced just
            # because this generic extraction exists.
            indicators    = r.get("indicators") or {}
            custom_stop   = indicators.get("Pattern_Stop")
            custom_target = indicators.get("Pattern_Target_Exact")

            outcome = pt.on_signal(
                symbol=r["symbol"],
                side=r["signal"],
                price=r["price"],
                strategy=r["strategy"],
                timeframe=r.get("timeframe", ""),
                strength=r.get("strength"),  # drives unit-based sizing in RMS
                custom_stop=custom_stop,
                custom_target=custom_target,
            )
            action = outcome.get("action")
            if action == "opened":
                log.info(f"PAPER OPEN  {r['symbol']}  {r['signal']}  "
                         f"qty={outcome['quantity']}  @ {outcome['entry']}")
            elif action == "closed":
                log.info(f"PAPER CLOSE {r['symbol']}  reason={outcome.get('reason')}  "
                         f"@ {outcome.get('exit')}")
            elif action == "reject":
                # Expected sometimes (cap full, RMS sizing) but a signal
                # that alerted and then went nowhere is exactly what was
                # invisible before — log it so it can't happen silently.
                log.info(f"PAPER REJECT {r['symbol']}  {r['signal']}  "
                         f"strategy={r['strategy']}  reason={outcome.get('reason')}")
            elif action == "error":
                log.warning(f"PAPER ERROR {r['symbol']}  {r['signal']}  "
                            f"strategy={r['strategy']}  reason={outcome.get('reason')}")
            elif action == "skip":
                log.debug(f"PAPER SKIP  {r['symbol']}  {r['signal']}  "
                          f"reason={outcome.get('reason')}")
        except Exception as e:
            log.warning(f"paper on_signal failed for {r.get('symbol')}: {e}")

ARBITRAGE_STRATEGY_NAME = "Cash-Futures Arbitrage"
NIFTY_SYMBOL = "^NSEI"

# ── Cache for Nifty trends ───────────────────────────────────
_nifty_trend_cache: dict = {
    "daily":  "NEUTRAL",
    "hourly": "NEUTRAL",
    "5min":   "NEUTRAL",
    "date":   None,
}


def _trend_arrow(trend: str) -> str:
    if trend == "RISING":  return "↑"
    if trend == "FALLING": return "↓"
    return "→"


# ── Data freshness guard (CURRENTLY DISABLED) ────────────────
# This over-fired in production (11:41 IST it skipped ~the whole
# universe -> zero signals) due to a timezone-frame mismatch when
# computing candle age. Kept here for a corrected reimplementation:
# the fix is to compare candle time and 'now' in the SAME tz frame
# (both UTC), account for label="left" resampling (a just-closed 1h
# candle is labeled up to 1h earlier), and TEST against live candle
# timestamps before re-enabling. Not called anywhere right now.
_INTERVAL_MINUTES = {
    "5m": 5, "15m": 15, "30m": 30, "1h": 60, "60m": 60,
    "1d": 1440, "1day": 1440, "1wk": 10080, "1mo": 43200,
}


def _is_stale(df: pd.DataFrame, interval: str, max_factor: float = 3.0) -> bool:
    """
    True if the latest candle is older than max_factor * interval.
    Conservative (3x) so we don't reject legitimately-spaced candles
    around market open or low-liquidity gaps. Intraday only — daily/
    weekly/monthly are never treated as stale (their gaps are normal).
    """
    mins = _INTERVAL_MINUTES.get(interval, 0)
    if mins == 0 or mins >= 1440:
        return False  # only guard intraday timeframes
    try:
        ts = df["Datetime"].iloc[-1]
        ts = pd.Timestamp(ts)
        if ts.tzinfo is None:
            ts = ts.tz_localize("Asia/Kolkata")
        now = pd.Timestamp.now(tz="Asia/Kolkata")
        age_min = (now - ts.tz_convert("Asia/Kolkata")).total_seconds() / 60.0
        return age_min > (mins * max_factor)
    except Exception:
        return False  # never block a scan on a parsing error


def _fetch_rsi_value(provider, symbol: str, interval: str, period: str):
    """Fetch RSI for a specific timeframe. Returns float or None."""
    try:
        df = provider.fetch_data(symbol=symbol, interval=interval, period=period)
        if df is None or df.empty or len(df) < 15:
            return None
        df_r = add_rsi(df.copy())
        df_r.dropna(subset=["RSI"], inplace=True)
        if df_r.empty:
            return None
        return round(float(df_r["RSI"].iloc[-1]), 1)
    except Exception:
        return None


def get_nifty_all_trends(provider) -> dict:
    """
    Fetch Nifty trends for D, H, 5m. Cached per calendar day.

    Per Jwala's EXACT spec:
      Daily  → current day close vs previous day close
      Hourly → current hour close vs previous hour close
      5min   → current 15min close vs previous 15min close

    Returns: {"daily": "RISING", "hourly": "NEUTRAL", "5min": "FALLING"}
    """
    global _nifty_trend_cache

    from datetime import date
    today = date.today().isoformat()

    if _nifty_trend_cache["date"] == today:
        return _nifty_trend_cache

    result = {"daily": "NEUTRAL", "hourly": "NEUTRAL", "5min": "NEUTRAL", "date": today}

    try:
        df_daily = provider.fetch_data(symbol=NIFTY_SYMBOL, interval="1d", period="5d")
        result["daily"] = get_nifty_trend(df_daily)
    except Exception:
        pass

    try:
        df_1h = provider.fetch_data(symbol=NIFTY_SYMBOL, interval="1h", period="5d")
        result["hourly"] = get_nifty_hourly_trend(df_1h)
    except Exception:
        pass

    try:
        # CRITICAL: use 15min candles for 5min trend per Jwala
        df_15m = provider.fetch_data(symbol=NIFTY_SYMBOL, interval="15m", period="3d")
        result["5min"] = get_nifty_5min_trend(df_15m)
    except Exception:
        pass

    _nifty_trend_cache = result
    log.info(
        f"Nifty trends: D{_trend_arrow(result['daily'])} "
        f"H{_trend_arrow(result['hourly'])} "
        f"5m{_trend_arrow(result['5min'])}"
    )
    return result


def get_nifty_daily_trend(provider) -> str:
    """Backward compatibility — returns daily trend only."""
    return get_nifty_all_trends(provider)["daily"]


class StrategyEngine:
    """
    Runs a selected strategy on a list of instruments.
    Enriches every signal with Nifty + stock D/H/5m trend context.
    """

    def __init__(self, strategy_name: str):
        if strategy_name not in STRATEGY_NAMES + [ARBITRAGE_STRATEGY_NAME]:
            raise ValueError(
                f"Unknown strategy: {strategy_name}. "
                f"Available: {STRATEGY_NAMES + [ARBITRAGE_STRATEGY_NAME]}"
            )
        self.strategy_name = strategy_name
        self.is_arbitrage  = (strategy_name == ARBITRAGE_STRATEGY_NAME)

        if not self.is_arbitrage:
            self.strategy = get_strategy(strategy_name)

        self.logger   = SignalLogger()
        self.alerts   = AlertManager()
        self.backtest = RSIBacktest()

    def scan_instrument(
        self,
        provider,
        symbol:        str,
        name:          str,
        category:      str,
        tf_name:       str,
        interval:      str,
        period:        str,
        nifty_trends:  dict = None,
        retries:       int  = 3,
    ) -> dict | None:
        if nifty_trends is None:
            nifty_trends = {"daily": "NEUTRAL", "hourly": "NEUTRAL", "5min": "NEUTRAL"}

        if self.is_arbitrage:
            return self._scan_arbitrage(
                provider, symbol, name, category,
                tf_name, interval, period, nifty_trends, retries
            )
        return self._scan_standard(
            provider, symbol, name, category,
            tf_name, interval, period, nifty_trends, retries
        )

    def _get_stock_all_trends(self, provider, symbol: str) -> dict:
        """
        Fetch stock trends for D, H, 5m per Jwala's spec.
        Returns dict with daily, hourly, 5min trends.
        """
        result = {"daily": "NEUTRAL", "hourly": "NEUTRAL", "5min": "NEUTRAL"}
        try:
            df_daily = provider.fetch_data(symbol=symbol, interval="1d", period="3mo")
            result["daily"] = get_stock_daily_trend(df_daily)
        except Exception:
            pass
        try:
            df_1h = provider.fetch_data(symbol=symbol, interval="1h", period="5d")
            result["hourly"] = get_stock_hourly_trend(df_1h)
        except Exception:
            pass
        try:
            # CRITICAL: use 15min candles for 5min trend per Jwala
            df_15m = provider.fetch_data(symbol=symbol, interval="15m", period="3d")
            result["5min"] = get_stock_5min_trend(df_15m)
        except Exception:
            pass
        return result

    def _scan_standard(
        self,
        provider,
        symbol:       str,
        name:         str,
        category:     str,
        tf_name:      str,
        interval:     str,
        period:       str,
        nifty_trends: dict = None,
        retries:      int  = 3,
    ) -> dict | None:

        if nifty_trends is None:
            nifty_trends = {"daily": "NEUTRAL", "hourly": "NEUTRAL", "5min": "NEUTRAL"}

        nifty_trend = nifty_trends.get("daily", "NEUTRAL")

        for attempt in range(1, retries + 1):
            try:
                df = provider.fetch_data(
                    symbol=symbol,
                    interval=interval,
                    period=period,
                )

                if df is None or df.empty or len(df) < 20:
                    log.warning(f"No data for {symbol} {tf_name} (interval={interval}) after fetch — got None/empty/insufficient rows (attempt {attempt}/{retries})")
                    return None

                # NOTE: a data-freshness guard was tried here but over-fired
                # (timezone-frame mismatch made every candle look ~5.5h stale,
                # skipping the whole universe -> zero signals). Removed until it
                # can be reimplemented with correct tz handling and tested.

                df_with_rsi = add_rsi(df.copy())
                df_with_rsi.dropna(subset=["RSI"], inplace=True)

                if len(df_with_rsi) < 3:
                    return None

                latest    = df_with_rsi.iloc[-1]
                rsi_val   = round(float(latest["RSI"]), 2)
                price_val = round(float(latest["Close"]), 2)

                result = self.strategy.generate_signal(df.copy())
                signal = result.signal

                stock_trends = {"daily": "NEUTRAL", "hourly": "NEUTRAL", "5min": "NEUTRAL"}

                if signal in ("BUY", "SELL"):
                    if category in ("INDEX", "COMMODITY"):
                        stock_trends = {"daily": "NEUTRAL", "hourly": "NEUTRAL", "5min": "NEUTRAL"}
                    else:
                        stock_trends = self._get_stock_all_trends(provider, symbol)

                    stock_trend = stock_trends.get("daily", "NEUTRAL")

                    if should_suppress_signal(signal, nifty_trend, stock_trend):
                        log.debug(
                            f"SUPPRESSED {symbol} {signal} — "
                            f"Nifty:{nifty_trend} Stock:{stock_trend}"
                        )
                        signal = "HOLD"

                    else:
                        volume_ratio = get_volume_spike_ratio(df.copy())

                        # Calculate strength with full Jwala spec
                        trend_strength = calculate_signal_strength(
                            signal=signal,
                            nifty_trend=nifty_trend,
                            stock_trend=stock_trends.get("daily", "NEUTRAL"),
                            volume_ratio=volume_ratio,
                            tf_name=tf_name,
                            nifty_hourly=nifty_trends.get("hourly", "NEUTRAL"),
                            nifty_5min=nifty_trends.get("5min", "NEUTRAL"),
                            stock_hourly=stock_trends.get("hourly", "NEUTRAL"),
                            stock_5min=stock_trends.get("5min", "NEUTRAL"),
                            rsi_val=rsi_val,
                        )
                        result.strength    = trend_strength
                        result.nifty_trend = nifty_trend
                        result.stock_trend = stock_trends.get("daily", "NEUTRAL")

                        # Store all trend info in indicators for alert message
                        result.indicators["volume_ratio"]       = volume_ratio
                        result.indicators["volume_label"]       = get_volume_spike_label(volume_ratio)
                        result.indicators["nifty_daily_trend"]  = nifty_trends.get("daily", "NEUTRAL")
                        result.indicators["nifty_hourly_trend"] = nifty_trends.get("hourly", "NEUTRAL")
                        result.indicators["nifty_5min_trend"]   = nifty_trends.get("5min", "NEUTRAL")
                        result.indicators["stock_daily_trend"]  = stock_trends.get("daily", "NEUTRAL")
                        result.indicators["stock_hourly_trend"] = stock_trends.get("hourly", "NEUTRAL")
                        result.indicators["stock_5min_trend"]   = stock_trends.get("5min", "NEUTRAL")

                        # Stock RSI D/H/5m values
                        try:
                            result.indicators["stock_rsi_daily"]  = _fetch_rsi_value(provider, symbol, "1d",  "3mo")
                            result.indicators["stock_rsi_hourly"] = _fetch_rsi_value(provider, symbol, "1h",  "5d")
                            result.indicators["stock_rsi_5min"]   = _fetch_rsi_value(provider, symbol, "15m", "3d")
                        except Exception:
                            pass

                else:
                    stock_trend = "NEUTRAL"

                self.logger.log_signal(
                    stock=symbol,
                    timeframe=tf_name,
                    signal=signal,
                    rsi=rsi_val,
                    price=price_val,
                    strategy=self.strategy_name,
                )

                alert = self.alerts.check_alert(
                    timeframe=tf_name,
                    stock=symbol,
                    current_signal=signal,
                    rsi=rsi_val,
                    price=price_val,
                    strategy=self.strategy_name,
                    signal_result=result,
                )

                if alert:
                    log.info(
                        f"ALERT  {symbol:20s}  {tf_name:12s}  "
                        f"{alert['previous']:5s} → {signal:5s}  "
                        f"[{result.strength}]  "
                        f"Nifty:D{_trend_arrow(nifty_trends.get('daily','NEUTRAL'))}"
                        f"H{_trend_arrow(nifty_trends.get('hourly','NEUTRAL'))}"
                        f"5m{_trend_arrow(nifty_trends.get('5min','NEUTRAL'))}"
                    )

                trades  = self.backtest.run(df_with_rsi)
                summary = self.backtest.summarise(trades)

                write_result(
                    symbol=symbol,
                    name=name,
                    timeframe=tf_name,
                    category=category,
                    summary=summary,
                    period=period,
                    strategy=self.strategy_name,
                )

                return {
                    "symbol":      symbol,
                    "signal":      signal,
                    "strength":    result.strength,
                    "reason":      result.reason,
                    "nifty_trend": nifty_trend,
                    "stock_trend": stock_trends.get("daily", "NEUTRAL"),
                    "rsi":         rsi_val,
                    "price":       price_val,
                    "trades":      summary["trades"],
                    "pnl":         summary["pnl"],
                    "win_rate":    summary["win_rate"],
                    "alerted":     alert is not None,
                }

            except Exception as e:
                if attempt < retries:
                    log.warning(f"Retry {attempt}/{retries}  {symbol}  {tf_name}: {e}")
                    time.sleep(2 * attempt)
                else:
                    log.error(f"FAILED {symbol}  {tf_name}  after {retries} attempts: {e}")
                    return None

    # ========================================================
    # MULTI-STRATEGY SCAN (parallel strategies, shared fetch)
    #
    # Runs several standard strategies (e.g. RSI Reversal +
    # Volume Spike) on the SAME instrument with a SINGLE data
    # fetch and a SINGLE enrichment pass per instrument.
    #
    # This is what keeps Upstox API load flat: without it, every
    # extra strategy would re-fetch the candles and re-run the
    # ~6 enrichment fetches (D/H/5m trend + 3 RSI values) per
    # signal — the exact thing that caused the earlier 429s/OOM.
    # ========================================================

    def _enrich_once(self, provider, symbol: str, category: str) -> dict:
        """
        Compute the expensive per-instrument context ONCE:
          - stock D/H/5m trends
          - stock RSI D/H/5m values
        Shared across every strategy's signal for this instrument.
        Indices/commodities get NEUTRAL stock trends (per Jwala).
        """
        enrich = {
            "stock_trends": {"daily": "NEUTRAL", "hourly": "NEUTRAL", "5min": "NEUTRAL"},
            "rsi_daily":  None,
            "rsi_hourly": None,
            "rsi_5min":   None,
        }
        if category in ("INDEX", "COMMODITY"):
            return enrich
        enrich["stock_trends"] = self._get_stock_all_trends(provider, symbol)
        try:
            enrich["rsi_daily"]  = _fetch_rsi_value(provider, symbol, "1d",  "3mo")
            enrich["rsi_hourly"] = _fetch_rsi_value(provider, symbol, "1h",  "5d")
            enrich["rsi_5min"]   = _fetch_rsi_value(provider, symbol, "15m", "3d")
        except Exception:
            pass
        return enrich

    def _scan_multi(
        self,
        provider,
        strategies:   dict,        # {name: strategy_instance}
        symbol:       str,
        name:         str,
        category:     str,
        tf_name:      str,
        interval:     str,
        period:       str,
        nifty_trends: dict = None,
        retries:      int  = 3,
    ) -> list:
        """
        Fetch one instrument ONCE, run every strategy in `strategies`
        on the same dataframe, enrich once, log+alert each tagged signal.
        Returns a list of per-signal result dicts (one per strategy).
        """
        if nifty_trends is None:
            nifty_trends = {"daily": "NEUTRAL", "hourly": "NEUTRAL", "5min": "NEUTRAL"}
        nifty_trend = nifty_trends.get("daily", "NEUTRAL")

        for attempt in range(1, retries + 1):
            try:
                df = provider.fetch_data(symbol=symbol, interval=interval, period=period)

                if df is None or df.empty or len(df) < 20:
                    log.warning(f"No data for {symbol} {tf_name} (interval={interval}) after fetch — got None/empty/insufficient rows (attempt {attempt}/{retries})")
                    return []

                # NOTE: freshness guard removed here too (see multi-scan note).

                # Real data source for the truthful Telegram tag (✅/⚠)
                data_source = "yfinance"
                try:
                    data_source = df.attrs.get("data_source", getattr(provider, "last_source", "yfinance"))
                except Exception:
                    data_source = getattr(provider, "last_source", "yfinance")

                df_with_rsi = add_rsi(df.copy())
                df_with_rsi.dropna(subset=["RSI"], inplace=True)
                if len(df_with_rsi) < 3:
                    return []

                latest    = df_with_rsi.iloc[-1]
                rsi_val   = round(float(latest["RSI"]), 2)
                price_val = round(float(latest["Close"]), 2)

                # Volume ratio computed once — shared by all strategies
                volume_ratio = get_volume_spike_ratio(df.copy())

                # Lazily computed (only if some strategy actually fires)
                enrich = None
                out    = []

                for strat_name, strat in strategies.items():
                    try:
                        result = strat.generate_signal(df.copy())
                    except Exception as e:
                        log.warning(f"{strat_name} failed on {symbol}: {e}")
                        continue

                    signal = result.signal

                    if signal in ("BUY", "SELL"):
                        # Enrich ONCE for this instrument, reuse for every strategy
                        if enrich is None:
                            enrich = self._enrich_once(provider, symbol, category)
                        stock_trends = enrich["stock_trends"]
                        stock_trend  = stock_trends.get("daily", "NEUTRAL")

                        if should_suppress_signal(signal, nifty_trend, stock_trend):
                            log.debug(f"SUPPRESSED {symbol} {signal} [{strat_name}] — "
                                      f"Nifty:{nifty_trend} Stock:{stock_trend}")
                            signal = "HOLD"
                        else:
                            trend_strength = calculate_signal_strength(
                                signal=signal,
                                nifty_trend=nifty_trend,
                                stock_trend=stock_trend,
                                volume_ratio=volume_ratio,
                                tf_name=tf_name,
                                nifty_hourly=nifty_trends.get("hourly", "NEUTRAL"),
                                nifty_5min=nifty_trends.get("5min", "NEUTRAL"),
                                stock_hourly=stock_trends.get("hourly", "NEUTRAL"),
                                stock_5min=stock_trends.get("5min", "NEUTRAL"),
                                rsi_val=rsi_val,
                            )
                            result.strength    = trend_strength
                            result.nifty_trend = nifty_trend
                            result.stock_trend = stock_trend

                            result.indicators["volume_ratio"]       = volume_ratio
                            result.indicators["volume_label"]       = get_volume_spike_label(volume_ratio)
                            result.indicators["nifty_daily_trend"]  = nifty_trends.get("daily", "NEUTRAL")
                            result.indicators["nifty_hourly_trend"] = nifty_trends.get("hourly", "NEUTRAL")
                            result.indicators["nifty_5min_trend"]   = nifty_trends.get("5min", "NEUTRAL")
                            result.indicators["stock_daily_trend"]  = stock_trends.get("daily", "NEUTRAL")
                            result.indicators["stock_hourly_trend"] = stock_trends.get("hourly", "NEUTRAL")
                            result.indicators["stock_5min_trend"]   = stock_trends.get("5min", "NEUTRAL")
                            result.indicators["stock_rsi_daily"]    = enrich["rsi_daily"]
                            result.indicators["stock_rsi_hourly"]   = enrich["rsi_hourly"]
                            result.indicators["stock_rsi_5min"]     = enrich["rsi_5min"]

                    # Log signal (skips HOLD + dups internally), tagged per strategy
                    self.logger.log_signal(
                        stock=symbol, timeframe=tf_name, signal=signal,
                        rsi=rsi_val, price=price_val, strategy=strat_name,
                    )

                    alert = self.alerts.check_alert(
                        timeframe=tf_name, stock=symbol, current_signal=signal,
                        rsi=rsi_val, price=price_val, strategy=strat_name,
                        signal_result=result, data_source=data_source,
                    )

                    if alert:
                        log.info(
                            f"ALERT  {symbol:20s}  {tf_name:12s}  [{strat_name}]  "
                            f"{alert['previous']:5s} → {signal:5s}  "
                            f"[{result.strength}]  src={data_source}"
                        )

                    out.append({
                        "symbol":      symbol,
                        "strategy":    strat_name,
                        "timeframe":   tf_name,
                        "signal":      signal,
                        "strength":    result.strength,
                        "reason":      result.reason,
                        "rsi":         rsi_val,
                        "price":       price_val,
                        "data_source": data_source,
                        "alerted":     alert is not None,
                        # Pattern-based strategies (3 Bar Play) expose their own
                        # natural stop via indicators["Pattern_Stop"] -- _run_paper_trading()
                        # reads it from here to pass as custom_stop. Without this key,
                        # every pattern-strategy trade silently fell back to RMS's
                        # generic 1%-of-entry stop (found 2026-08-25 audit).
                        "indicators":  result.indicators,
                    })

                return out

            except Exception as e:
                if attempt < retries:
                    log.warning(f"Retry {attempt}/{retries}  {symbol}  {tf_name} [multi]: {e}")
                    time.sleep(2 * attempt)
                else:
                    log.error(f"FAILED {symbol}  {tf_name} [multi] after {retries} attempts: {e}")
                    return []

    def _scan_arbitrage(
        self,
        provider,
        symbol:       str,
        name:         str,
        category:     str,
        tf_name:      str,
        interval:     str,
        period:       str,
        nifty_trends: dict = None,
        retries:      int  = 3,
    ) -> dict | None:

        if category != "STOCK":
            return None

        try:
            from data.providers.upstox_provider import get_token
            token = get_token()
        except Exception:
            token = None

        if not token:
            return None

        for attempt in range(1, retries + 1):
            try:
                df = provider.fetch_data(
                    symbol=symbol,
                    interval=interval,
                    period=period,
                )

                if df is None or df.empty:
                    log.warning(f"No data for {symbol} {tf_name} (interval={interval}) after fetch — got None/empty (attempt {attempt}/{retries})")
                    return None

                spot_price = round(float(df["Close"].iloc[-1]), 2)

                result = arbitrage_strategy.generate_arbitrage_signal(
                    symbol=symbol,
                    spot_price=spot_price,
                    token=token,
                )

                signal     = result.signal
                spread_pct = result.indicators.get("Spread_Pct", 0.0)

                self.logger.log_signal(
                    stock=symbol,
                    timeframe=tf_name,
                    signal=signal,
                    rsi=spread_pct,
                    price=spot_price,
                    strategy=self.strategy_name,
                )

                alert = self.alerts.check_alert(
                    timeframe=tf_name,
                    stock=symbol,
                    current_signal=signal,
                    rsi=spread_pct,
                    price=spot_price,
                    strategy=self.strategy_name,
                    signal_result=result,
                )

                return {
                    "symbol":      symbol,
                    "signal":      signal,
                    "strength":    result.strength,
                    "reason":      result.reason,
                    "nifty_trend": "NEUTRAL",
                    "stock_trend": "NEUTRAL",
                    "rsi":         spread_pct,
                    "price":       spot_price,
                    "indicators":  result.indicators,
                    "alerted":     alert is not None,
                }

            except Exception as e:
                if attempt < retries:
                    log.warning(f"[Arbitrage] Retry {attempt}/{retries}  {symbol}: {e}")
                    time.sleep(2 * attempt)
                else:
                    log.error(f"[Arbitrage] FAILED {symbol}: {e}")
                    return None

    def run_scan(
        self,
        provider,
        tf_name:     str,
        interval:    str,
        period:      str,
        instruments: list,
        max_workers: int = 10,
    ) -> list:

        if not instruments:
            return []

        # Fetch all Nifty trends ONCE per scan cycle
        nifty_trends = get_nifty_all_trends(provider)
        nifty_trend  = nifty_trends.get("daily", "NEUTRAL")

        log.info(
            f"SCAN START  [{self.strategy_name}]  {tf_name}  "
            f"{len(instruments)} instruments  "
            f"Nifty:D{_trend_arrow(nifty_trends['daily'])}"
            f"H{_trend_arrow(nifty_trends['hourly'])}"
            f"5m{_trend_arrow(nifty_trends['5min'])}"
        )

        start_time = time.time()
        results    = []
        signals    = 0

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    self.scan_instrument,
                    provider,
                    inst["symbol"],
                    inst["name"],
                    inst["category"],
                    tf_name,
                    interval,
                    period,
                    nifty_trends,  # Pass full trends dict
                ): inst
                for inst in instruments
            }

            # timeout=300 (5 min max per scan) to prevent hanging. NOTE:
            # as_completed() itself raises TimeoutError once the overall
            # deadline passes with futures still unfinished — that's NOT
            # caught by the per-future try/except below (which only
            # guards future.result()), so it must be caught around the
            # whole loop. Previously uncaught, this escaped run_scan()
            # entirely on slow cycles, discarding every result gathered
            # so far and erroring out the whole scheduler job.
            try:
                for future in as_completed(futures, timeout=300):
                    try:
                        result = future.result(timeout=30)
                        if result:
                            results.append(result)
                            if result["signal"] != "HOLD":
                                signals += 1
                    except Exception as e:
                        log.warning(f"Instrument scan error: {e}")
            except TimeoutError:
                unfinished = sum(1 for f in futures if not f.done())
                log.warning(
                    f"SCAN [{self.strategy_name}] {tf_name} hit the 300s deadline "
                    f"with {unfinished}/{len(futures)} instruments still running — "
                    f"proceeding with the {len(results)} results collected so far."
                )

        elapsed = round(time.time() - start_time, 1)
        log.info(
            f"SCAN DONE   [{self.strategy_name}]  {tf_name}  "
            f"{len(results)}/{len(instruments)} processed  "
            f"{signals} signals  {elapsed}s"
        )

        return results

    def run_multi_scan(
        self,
        provider,
        strategy_names: list,      # e.g. ["RSI Reversal", "Volume Spike"]
        tf_name:        str,
        interval:       str,
        period:         str,
        instruments:    list,
        max_workers:    int = 10,
    ) -> list:
        """
        Run MULTIPLE standard strategies in parallel on a shared fetch.

        Each instrument is fetched ONCE; every strategy in
        `strategy_names` is evaluated on that same dataframe, and the
        per-instrument enrichment (trends + RSI) is computed once and
        shared. Each signal is logged/alerted tagged with its strategy.

        Volume Spike is restricted to STOCK instruments (Jwala's
        institutional-buying framing); other strategies run on all.
        """
        if not instruments:
            return []

        # Build strategy instances once
        strategies = {}
        for sname in strategy_names:
            try:
                strategies[sname] = get_strategy(sname)
            except Exception as e:
                log.warning(f"Skipping unknown strategy '{sname}': {e}")

        if not strategies:
            log.warning("run_multi_scan: no valid strategies — nothing to do")
            return []

        # Strategies that only make sense on stocks
        STOCK_ONLY = {"Volume Spike"}

        nifty_trends = get_nifty_all_trends(provider)

        log.info(
            f"MULTI SCAN START  {list(strategies.keys())}  {tf_name}  "
            f"{len(instruments)} instruments  "
            f"Nifty:D{_trend_arrow(nifty_trends['daily'])}"
            f"H{_trend_arrow(nifty_trends['hourly'])}"
            f"5m{_trend_arrow(nifty_trends['5min'])}"
        )

        start_time = time.time()
        results    = []
        signals    = 0

        def _scan_one(inst):
            category = inst["category"]
            # Filter strategies applicable to this instrument's category
            applicable = {
                n: s for n, s in strategies.items()
                if not (n in STOCK_ONLY and category != "STOCK")
            }
            if not applicable:
                return []
            return self._scan_multi(
                provider, applicable,
                inst["symbol"], inst["name"], category,
                tf_name, interval, period, nifty_trends,
            )

        # NOT a `with` block on purpose: ThreadPoolExecutor's context-manager
        # __exit__ calls shutdown(wait=True) unconditionally, which blocks
        # until EVERY submitted future finishes — not just the ones done by
        # the 300s deadline below. With max_workers=10, at most ~10 futures
        # are actually running at that moment; the other 300+ (of 507) are
        # merely queued. The `with` block was silently waiting for all of
        # those to run to completion too, 10 at a time, after already
        # logging "proceeding with results collected so far" — a second,
        # unlogged, 10-20+ minute stall on every slow cycle. That stall
        # was the real reason "15 Minutes"/"1 Hour" scans never got reached
        # in the same execution: this function simply never returned in
        # time, no matter how the outer scheduling logic was fixed.
        # cancel_futures=True (3.9+) drops the still-queued ones immediately;
        # wait=False means we don't block on the handful genuinely in flight
        # either — they finish in the background and their results are
        # simply never collected, which is fine, this data is already stale.
        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
            futures = {executor.submit(_scan_one, inst): inst for inst in instruments}
            # NOTE: as_completed() itself raises TimeoutError once the
            # overall 300s deadline passes with futures still unfinished
            # — that's NOT caught by the per-future try/except below
            # (which only guards future.result()), so it must be caught
            # around the whole loop. Previously uncaught, this escaped
            # run_multi_scan() entirely on slow cycles (seen repeatedly
            # in production — up to 150/174 instruments still running at
            # the deadline), which meant _run_paper_trading() below never
            # ran at all: every signal from that cycle, including ones
            # whose Telegram alert had already fired from inside the
            # worker threads, was silently discarded.
            try:
                for future in as_completed(futures, timeout=300):
                    try:
                        res_list = future.result(timeout=60)
                        for r in (res_list or []):
                            results.append(r)
                            if r["signal"] != "HOLD":
                                signals += 1
                    except Exception as e:
                        log.warning(f"Instrument multi-scan error: {e}")
            except TimeoutError:
                unfinished = sum(1 for f in futures if not f.done())
                log.warning(
                    f"MULTI SCAN {tf_name} hit the 300s deadline with "
                    f"{unfinished}/{len(futures)} instruments still running — "
                    f"proceeding with the {len(results)} results collected so far "
                    f"(paper trading still runs on these)."
                )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        elapsed = round(time.time() - start_time, 1)
        log.info(
            f"MULTI SCAN DONE   {list(strategies.keys())}  {tf_name}  "
            f"{len(instruments)} instruments  {signals} signals  {elapsed}s"
        )

        # ── Paper trading ─────────────────────────────────────────
        # Feed newly-alerted BUY/SELL signals to the paper trader (equity
        # only; the trader itself filters non-equity + enforces RMS limits).
        # Runs here — AFTER the thread pool joins — so it's single-threaded
        # and safe (RMS is stateful, DB writes must not race). Fully guarded
        # so a paper-trading error can never break signal generation.
        try:
            _run_paper_trading(provider, results)
        except Exception as e:
            log.warning(f"paper trading hook error (non-fatal): {e}")

        return results