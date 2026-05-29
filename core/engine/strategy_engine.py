# ============================================================
# core/engine/strategy_engine.py
#
# Runs the selected strategy on all instruments.
# Handles both regular strategies and Cash-Futures Arbitrage.
#
# Usage:
#   engine = StrategyEngine("RSI Reversal")
#   results = engine.run_scan(tf_name, instruments)
# ============================================================

import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import pytz

from core.strategies.strategies import get_strategy, STRATEGY_NAMES
from core.strategies.arbitrage_strategy import ArbitrageStrategy, arbitrage_strategy
from core.indicators.indicators import add_rsi
from core.logger.signal_logger import SignalLogger
from core.alerts.alert_manager import AlertManager
from core.backtesting.rsi_backtest import RSIBacktest
from core.backtesting.backtest_store import write_result

log = logging.getLogger("strategy_engine")
IST = pytz.timezone("Asia/Kolkata")

ARBITRAGE_STRATEGY_NAME = "Cash-Futures Arbitrage"


class StrategyEngine:
    """
    Runs a selected strategy on a list of instruments.
    Handles data fetching, signal generation, logging, alerting.
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
        symbol:   str,
        name:     str,
        category: str,
        tf_name:  str,
        interval: str,
        period:   str,
        retries:  int = 3,
    ) -> dict | None:
        """
        Scan a single instrument with the selected strategy.
        Returns result dict or None on failure.
        """
        if self.is_arbitrage:
            return self._scan_arbitrage(
                provider, symbol, name, category, tf_name, interval, period, retries
            )
        return self._scan_standard(
            provider, symbol, name, category, tf_name, interval, period, retries
        )

    def _scan_standard(
        self,
        provider,
        symbol:   str,
        name:     str,
        category: str,
        tf_name:  str,
        interval: str,
        period:   str,
        retries:  int = 3,
    ) -> dict | None:
        """Run a standard (non-arbitrage) strategy on one instrument."""

        for attempt in range(1, retries + 1):
            try:
                df = provider.fetch_data(
                    symbol=symbol,
                    interval=interval,
                    period=period,
                )

                if df is None or df.empty or len(df) < 20:
                    return None

                # Ensure RSI is available for backtest + price/rsi logging
                df_with_rsi = add_rsi(df.copy())
                df_with_rsi.dropna(subset=["RSI"], inplace=True)

                if len(df_with_rsi) < 3:
                    return None

                latest    = df_with_rsi.iloc[-1]
                rsi_val   = round(float(latest["RSI"]), 2)
                price_val = round(float(latest["Close"]), 2)

                # Run selected strategy
                result = self.strategy.generate_signal(df.copy())
                signal = result.signal

                # Log signal
                self.logger.log_signal(
                    stock=symbol,
                    timeframe=tf_name,
                    signal=signal,
                    rsi=rsi_val,
                    price=price_val,
                    strategy=self.strategy_name,
                )

                # Alert on state change — pass full result for rich message
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
                        f"RSI={rsi_val}  [{result.strength}]"
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
                    "symbol":    symbol,
                    "signal":    signal,
                    "strength":  result.strength,
                    "reason":    result.reason,
                    "rsi":       rsi_val,
                    "price":     price_val,
                    "trades":    summary["trades"],
                    "pnl":       summary["pnl"],
                    "win_rate":  summary["win_rate"],
                    "alerted":   alert is not None,
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
        symbol:   str,
        name:     str,
        category: str,
        tf_name:  str,
        interval: str,
        period:   str,
        retries:  int = 3,
    ) -> dict | None:
        """
        Run Cash-Futures Arbitrage scan on one instrument.
        Only applicable to STOCK category (not indexes or commodities).
        """
        # Arbitrage only applies to NSE stocks with F&O contracts
        if category != "STOCK":
            return None

        # Get Upstox token
        try:
            from data.providers.upstox_provider import get_token
            token = get_token()
        except Exception:
            token = None

        if not token:
            log.warning(f"[Arbitrage] No Upstox token — skipping {symbol}")
            return None

        for attempt in range(1, retries + 1):
            try:
                # Fetch spot price
                df = provider.fetch_data(
                    symbol=symbol,
                    interval=interval,
                    period=period,
                )

                if df is None or df.empty:
                    return None

                spot_price = round(float(df["Close"].iloc[-1]), 2)

                # Generate arbitrage signal
                result = arbitrage_strategy.generate_arbitrage_signal(
                    symbol=symbol,
                    spot_price=spot_price,
                    token=token,
                )

                signal = result.signal

                # Use spread_pct as RSI equivalent for logging
                spread_pct = result.indicators.get("Spread_Pct", 0.0)

                # Log signal
                self.logger.log_signal(
                    stock=symbol,
                    timeframe=tf_name,
                    signal=signal,
                    rsi=spread_pct,  # store spread % in RSI field
                    price=spot_price,
                    strategy=self.strategy_name,
                )

                # Alert
                alert = self.alerts.check_alert(
                    timeframe=tf_name,
                    stock=symbol,
                    current_signal=signal,
                    rsi=spread_pct,
                    price=spot_price,
                    strategy=self.strategy_name,
                    signal_result=result,
                )

                if alert:
                    log.info(
                        f"ARBITRAGE  {symbol:20s}  "
                        f"Spread={spread_pct}%  "
                        f"[{result.strength}]"
                    )

                return {
                    "symbol":      symbol,
                    "signal":      signal,
                    "strength":    result.strength,
                    "reason":      result.reason,
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
                    log.error(f"[Arbitrage] FAILED {symbol} after {retries} attempts: {e}")
                    return None

    def run_scan(
        self,
        provider,
        tf_name:     str,
        interval:    str,
        period:      str,
        instruments: list[dict],
        max_workers: int = 10,
    ) -> list[dict]:
        """
        Run strategy on all instruments in parallel.
        Returns list of result dicts.
        """
        if not instruments:
            return []

        log.info(
            f"SCAN START  [{self.strategy_name}]  {tf_name}  "
            f"{len(instruments)} instruments"
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