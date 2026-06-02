# ============================================================
# core/engine/strategy_engine.py
#
# Runs the selected strategy on all instruments.
# Applies Jwala's trend-based signal strength logic:
#   - Fetches Nifty daily trend once per scan cycle
#   - Fetches each stock's daily trend per instrument
#   - Calculates STRONG / MODERATE / WEAK from trends
#   - Suppresses signals when both trends oppose the signal
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
    get_daily_trend,
    calculate_signal_strength,
    should_suppress_signal,
)
from core.logger.signal_logger import SignalLogger
from core.alerts.alert_manager import AlertManager
from core.backtesting.rsi_backtest import RSIBacktest
from core.backtesting.backtest_store import write_result

log = logging.getLogger("strategy_engine")
IST = pytz.timezone("Asia/Kolkata")

ARBITRAGE_STRATEGY_NAME = "Cash-Futures Arbitrage"
NIFTY_SYMBOL = "^NSEI"


# ============================================================
# NIFTY TREND — fetched once per scan cycle
# ============================================================

_nifty_trend_cache: dict = {"trend": "NEUTRAL", "date": None}


def get_nifty_daily_trend(provider) -> str:
    """
    Fetch Nifty daily trend. Cached per calendar day.
    Returns RISING | FALLING | NEUTRAL.
    """
    global _nifty_trend_cache

    from datetime import date
    today = date.today().isoformat()

    if _nifty_trend_cache["date"] == today:
        return _nifty_trend_cache["trend"]

    try:
        df = provider.fetch_data(
            symbol=NIFTY_SYMBOL,
            interval="1d",
            period="3mo",
        )
        trend = get_daily_trend(df)
        _nifty_trend_cache = {"trend": trend, "date": today}
        log.info(f"Nifty daily trend: {trend}")
        return trend
    except Exception as e:
        log.warning(f"Could not fetch Nifty trend: {e}")
        return "NEUTRAL"


class StrategyEngine:
    """
    Runs a selected strategy on a list of instruments.
    Enriches every signal with Nifty + stock trend context.
    Suppresses low-quality signals automatically.
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
        symbol:       str,
        name:         str,
        category:     str,
        tf_name:      str,
        interval:     str,
        period:       str,
        nifty_trend:  str = "NEUTRAL",
        retries:      int = 3,
    ) -> dict | None:
        if self.is_arbitrage:
            return self._scan_arbitrage(
                provider, symbol, name, category,
                tf_name, interval, period, nifty_trend, retries
            )
        return self._scan_standard(
            provider, symbol, name, category,
            tf_name, interval, period, nifty_trend, retries
        )

    def _get_stock_daily_trend(self, provider, symbol: str) -> str:
        """Fetch stock's own daily trend for strength calculation."""
        try:
            df_daily = provider.fetch_data(
                symbol=symbol,
                interval="1d",
                period="3mo",
            )
            return get_daily_trend(df_daily)
        except Exception:
            return "NEUTRAL"

    def _scan_standard(
        self,
        provider,
        symbol:      str,
        name:        str,
        category:    str,
        tf_name:     str,
        interval:    str,
        period:      str,
        nifty_trend: str = "NEUTRAL",
        retries:     int = 3,
    ) -> dict | None:

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

                # Run strategy
                result = self.strategy.generate_signal(df.copy())
                signal = result.signal

                # ── Trend-based strength (Jwala's logic) ──
                if signal in ("BUY", "SELL"):
                    # For indexes and commodities use NEUTRAL stock trend
                    if category in ("INDEX", "COMMODITY"):
                        stock_trend = "NEUTRAL"
                    else:
                        stock_trend = self._get_stock_daily_trend(provider, symbol)

                    # Check suppression
                    if should_suppress_signal(signal, nifty_trend, stock_trend):
                        log.debug(
                            f"SUPPRESSED {symbol} {signal} — "
                            f"Nifty:{nifty_trend} Stock:{stock_trend}"
                        )
                        signal = "HOLD"

                    else:
                        # Recalculate strength based on trends
                        trend_strength = calculate_signal_strength(
                            signal, nifty_trend, stock_trend
                        )
                        result.strength    = trend_strength
                        result.nifty_trend = nifty_trend
                        result.stock_trend = stock_trend
                else:
                    stock_trend = "NEUTRAL"

                # Log signal
                self.logger.log_signal(
                    stock=symbol,
                    timeframe=tf_name,
                    signal=signal,
                    rsi=rsi_val,
                    price=price_val,
                    strategy=self.strategy_name,
                )

                # Alert on state change
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
                        f"Nifty:{nifty_trend}  Stock:{stock_trend}"
                    )

                # Backtest
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
                    "stock_trend": stock_trend,
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
        symbol:      str,
        name:        str,
        category:    str,
        tf_name:     str,
        interval:    str,
        period:      str,
        nifty_trend: str = "NEUTRAL",
        retries:     int = 3,
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
        instruments: list[dict],
        max_workers: int = 5,   # reduced to stay within 1GB RAM
    ) -> list[dict]:

        if not instruments:
            return []

        # Fetch Nifty trend ONCE per scan cycle
        nifty_trend = get_nifty_daily_trend(provider)

        log.info(
            f"SCAN START  [{self.strategy_name}]  {tf_name}  "
            f"{len(instruments)} instruments  Nifty:{nifty_trend}"
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
                    nifty_trend,
                ): inst
                for inst in instruments
            }

            for future in as_completed(futures):
                result = future.result()
                if result:
                    results.append(result)
                    if result["signal"] != "HOLD":
                        signals += 1

        elapsed = round(time.time() - start_time, 1)
        log.info(
            f"SCAN DONE   [{self.strategy_name}]  {tf_name}  "
            f"{len(results)}/{len(instruments)} processed  "
            f"{signals} signals  {elapsed}s"
        )

        return results