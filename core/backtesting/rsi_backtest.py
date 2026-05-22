# ============================================================
# core/backtesting/rsi_backtest.py
# ============================================================


class RSIBacktest:

    def run(self, df) -> list[dict]:
        """
        Simulate RSI reversal strategy on historical data.
        Uses same reversal logic as ReversalRSISignal:
          BUY  when RSI bounces back above 30 after being below
          SELL when RSI drops back below 70 after being above

        Returns list of trade dicts.
        Completed trades (BUY + SELL pair) contain PnL and PnL%.
        """
        trades   = []
        position = None
        buy_price = 0.0

        for i in range(2, len(df)):
            try:
                current_rsi  = float(df["RSI"].iloc[i])
                previous_rsi = float(df["RSI"].iloc[i - 1])
                close        = float(df["Close"].iloc[i])
            except (TypeError, ValueError):
                continue

            # ---- BUY: RSI bouncing from below 30 ----
            if (
                previous_rsi < 30
                and current_rsi > previous_rsi
                and position is None
            ):
                position  = "BUY"
                buy_price = close
                trades.append({
                    "Type":  "BUY",
                    "Price": round(close, 2),
                })

            # ---- SELL: RSI reversing from above 70 ----
            elif (
                previous_rsi > 70
                and current_rsi < previous_rsi
                and position == "BUY"
            ):
                pnl     = close - buy_price
                pnl_pct = (pnl / buy_price) * 100 if buy_price > 0 else 0.0

                trades.append({
                    "Type":  "SELL",
                    "Price": round(close, 2),
                    "PnL":   round(pnl, 2),
                    "PnL %": round(pnl_pct, 2),
                })
                position = None

        return trades

    def summarise(self, trades: list[dict]) -> dict:
        """
        Returns a summary dict from a list of trades.
        Used by the scheduler to write backtest_results.csv.
        """
        completed = [t for t in trades if "PnL" in t]
        wins      = [t for t in completed if t.get("PnL", 0) > 0]

        total_pnl  = round(sum(t.get("PnL", 0) for t in completed), 2)
        win_rate   = round(len(wins) / len(completed) * 100, 1) if completed else 0.0

        buy_trades = [t for t in trades if t.get("Type") == "BUY"]
        avg_buy    = (
            sum(t["Price"] for t in buy_trades) / len(buy_trades)
            if buy_trades else 0.0
        )
        pnl_pct = (
            round((total_pnl / (avg_buy * len(buy_trades))) * 100, 2)
            if avg_buy > 0 and buy_trades else 0.0
        )

        return {
            "trades":    len(completed),
            "pnl":       total_pnl,
            "pnl_pct":   pnl_pct,
            "win_rate":  win_rate,
            "wins":      len(wins),
            "losses":    len(completed) - len(wins),
        }
