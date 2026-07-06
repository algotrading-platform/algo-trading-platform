# ============================================================
# core/execution/paper_trader.py
#
# Paper-trading orchestrator — the loop that ties the layers:
#
#   Signal → RMS → Order Manager → Sandbox → Position Tracker (DB)
#
# Two entry points:
#   on_signal(...)  — a BUY/SELL fired; open a paper position if RMS
#                     approves, Order Manager validates, sandbox accepts.
#   monitor_open()  — check open positions against current price; close
#                     on opposite exit (stop/target) and record P&L.
#
# EQUITY-first. Indexes/commodities are skipped (not equity-tradeable).
# ============================================================

import logging

from core.execution.rms import RMS, RMSConfig
from core.execution.order_manager import OrderManager
from core.execution.sandbox_client import SandboxClient
from core.database import db

log = logging.getLogger("paper_trader")

# Max concurrent open paper positions (per earlier decision).
MAX_OPEN_POSITIONS = 15

# Only equities are paper-traded. Skip index/commodity symbols.
def _is_equity(symbol: str) -> bool:
    if symbol.startswith("^"):        # ^NSEI, ^BSESN — indexes
        return False
    if "=" in symbol:                 # GC=F, SI=F — commodities
        return False
    return symbol.endswith(".NS")     # NSE equity


class PaperTrader:

    def __init__(self, provider=None):
        self.rms  = RMS(RMSConfig)
        # Order Manager knows what's open (for idempotency + no re-entry)
        self.om   = OrderManager(is_open_position_fn=db.is_paper_position_open)
        self.sbx  = SandboxClient(sandbox=True)
        # provider is used to resolve symbol -> Upstox instrument key
        # and to fetch current prices for monitoring.
        self.provider = provider

    # --------------------------------------------------------
    # ENTRY — a signal fired
    # --------------------------------------------------------
    def on_signal(
        self,
        symbol:     str,
        side:       str,        # "BUY" / "SELL"
        price:      float,
        strategy:   str,
        timeframe:  str,
    ) -> dict:
        """
        Full pipeline for one signal. Returns a result dict describing
        what happened (opened / rejected / error), for logging/alerting.
        """
        if side not in ("BUY", "SELL"):
            return {"action": "skip", "reason": f"non-tradeable signal {side}"}

        if not _is_equity(symbol):
            return {"action": "skip", "reason": f"{symbol} not equity — not paper-traded"}

        # Concurrency cap
        if db.count_open_paper_positions() >= MAX_OPEN_POSITIONS:
            return {"action": "reject", "reason": f"max {MAX_OPEN_POSITIONS} open positions"}

        # 1. RMS
        decision = self.rms.evaluate(symbol, side, price)
        if not decision.approved:
            return {"action": "reject", "reason": f"RMS: {decision.reason}"}

        # 2. Order Manager (idempotency + already-open guard + validation)
        signal_identity = f"{timeframe}|{strategy}"
        order = self.om.build_order(decision, signal_identity, instrument="EQUITY")
        if not order.valid:
            return {"action": "reject", "reason": f"OrderMgr: {order.reason}"}

        # 3. Resolve instrument key for the sandbox order
        inst_key = self._resolve_key(symbol)
        if not inst_key:
            return {"action": "error", "reason": f"no instrument key for {symbol}"}

        # 4. Place in sandbox
        result = self.sbx.place_order(order, inst_key)
        if not result["ok"]:
            return {"action": "error", "reason": f"sandbox: {result['error']}"}

        # 5. Record the open position in the DB
        ok = db.open_paper_position(
            symbol=symbol,
            side=side,
            quantity=order.quantity,
            entry_price=order.price,
            stop_loss=order.stop_loss,
            target=order.target,
            strategy=strategy,
            timeframe=timeframe,
            risk_amount=decision.risk_amount,
            order_id=result["order_id"],
        )
        if not ok:
            return {"action": "error", "reason": "sandbox order placed but DB write failed"}

        return {
            "action":   "opened",
            "symbol":   symbol,
            "side":     side,
            "quantity": order.quantity,
            "entry":    order.price,
            "stop":     order.stop_loss,
            "target":   order.target,
            "order_id": result["order_id"],
        }

    # --------------------------------------------------------
    # MONITOR — check open positions, close on stop/target
    # --------------------------------------------------------
    def monitor_open(self) -> list[dict]:
        """
        For each open position, fetch current price and close it if the
        stop-loss or target has been hit. Returns list of close events.
        (Exit-on-opposite-signal is handled via on_signal + close_by_symbol.)
        """
        closed = []
        open_df = db.get_open_paper_positions()
        if open_df is None or open_df.empty:
            return closed

        for _, pos in open_df.iterrows():
            symbol = pos["symbol"]
            price  = self._current_price(symbol)
            if price is None:
                continue

            side   = pos["side"]
            stop   = float(pos["stop_loss"])
            target = float(pos["target"])
            pid    = int(pos["id"])

            hit = None
            if side == "BUY":
                if price <= stop:   hit = "stop"
                elif price >= target: hit = "target"
            else:  # SELL
                if price >= stop:   hit = "stop"
                elif price <= target: hit = "target"

            if hit:
                if db.close_paper_position(pid, price, exit_reason=hit):
                    # update RMS daily P&L for the kill switch
                    qty   = int(pos["quantity"])
                    entry = float(pos["entry_price"])
                    pnl   = (price - entry) * qty if side == "BUY" else (entry - price) * qty
                    self.rms.record_realized_pnl(pnl)
                    closed.append({
                        "symbol": symbol, "reason": hit,
                        "exit": price, "pnl": round(pnl, 2),
                    })
        return closed

    def close_by_symbol(self, symbol: str, price: float, reason: str = "signal") -> bool:
        """Close an open position for a symbol (e.g. opposite signal fired)."""
        open_df = db.get_open_paper_positions(symbol=symbol)
        if open_df is None or open_df.empty:
            return False
        pid = int(open_df.iloc[0]["id"])
        ok = db.close_paper_position(pid, price, exit_reason=reason)
        if ok:
            pos   = open_df.iloc[0]
            qty   = int(pos["quantity"]); entry = float(pos["entry_price"])
            pnl = (price - entry) * qty if pos["side"] == "BUY" else (entry - price) * qty
            self.rms.record_realized_pnl(pnl)
        return ok

    # --------------------------------------------------------
    # helpers
    # --------------------------------------------------------
    def _resolve_key(self, symbol: str):
        if self.provider is None:
            return None
        try:
            from core.database.db import get_upstox_token
            token = get_upstox_token()
            return self.provider._resolve_symbol(symbol, token)
        except Exception as e:
            log.warning(f"_resolve_key failed for {symbol}: {e}")
            return None

    def _current_price(self, symbol: str):
        if self.provider is None:
            return None
        try:
            df = self.provider.fetch_data(symbol=symbol, interval="5m", period="1d")
            if df is not None and not df.empty:
                return round(float(df["Close"].iloc[-1]), 2)
        except Exception as e:
            log.warning(f"_current_price failed for {symbol}: {e}")
        return None