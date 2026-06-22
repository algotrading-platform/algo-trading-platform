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
                    return None

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

            # FIXED: added timeout=300 (5 min max per scan) to prevent hanging
            for future in as_completed(futures, timeout=300):
                try:
                    result = future.result(timeout=30)
                    if result:
                        results.append(result)
                        if result["signal"] != "HOLD":
                            signals += 1
                except Exception as e:
                    log.warning(f"Instrument scan error: {e}")

        elapsed = round(time.time() - start_time, 1)
        log.info(
            f"SCAN DONE   [{self.strategy_name}]  {tf_name}  "
            f"{len(results)}/{len(instruments)} processed  "
            f"{signals} signals  {elapsed}s"
        )

        return results