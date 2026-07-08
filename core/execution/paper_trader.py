# ============================================================
# core/execution/paper_trader.py
#
# Paper-trading orchestrator — the loop that ties the layers:
#
#   Signal → RMS → Order Manager → Sandbox → Position Tracker (DB)
#
# SYMMETRIC BUY/SELL (Jwala Jul 8: "let's add both buying and
# selling... let us see how it works"). Each symbol holds at most
# ONE position at a time — flat, LONG, or SHORT:
#
#   flat + BUY  -> opens a LONG        flat + SELL -> opens a SHORT
#   LONG + SELL -> closes (reversal)   SHORT + BUY -> closes (reversal)
#   LONG + BUY  -> skip (no re-entry)  SHORT + SELL-> skip (no re-entry)
#
# Stop/target math is mirrored for shorts (handled in rms.py, already
# symmetric) and monitor_open() checks both directions.
#
# REALISTIC CONSTRAINT: cash-equity shorts can't carry overnight, so
# any open SHORT is force-closed at SQUARE_OFF_TIME regardless of
# stop/target (reason "square_off"). Longs are unaffected.
#
# Two entry points:
#   on_signal(...)  — a BUY/SELL fired; open/close per the table above.
#   monitor_open()  — check open positions against current price; close
#                     on stop/target/square-off and record P&L.
#
# Manual controls (Jwala Jul 8 — dashboard buttons):
#   close_manual(...) — book P&L now regardless of stop/target.
#   update_stop(...)  — move the stop (e.g. to breakeven) by hand.
#   These act directly via db.py (no RMS/order-manager involvement —
#   there's no new order being placed). NOTE: the RMS daily-loss kill
#   switch tracks realized P&L in-memory in the SCHEDULER process only;
#   manual closes issued from the dashboard (a separate process) won't
#   feed into that counter in real time. Doesn't affect correctness of
#   the manual action itself, but is worth fixing later (e.g. compute
#   daily P&L from the DB each scan instead of an in-memory counter)
#   if the kill switch needs to see manual closes immediately.
#
# EQUITY-first. Indexes/commodities are skipped (not equity-tradeable).
# ============================================================

import logging
from datetime import datetime, time as dtime

import pytz

from core.execution.rms import RMS, RMSConfig
from core.execution.order_manager import OrderManager
from core.execution.sandbox_client import SandboxClient
from core.database import db

log = logging.getLogger("paper_trader")

IST = pytz.timezone("Asia/Kolkata")

# Max concurrent open paper positions (per earlier decision).
MAX_OPEN_POSITIONS = 15

# Cash-equity shorts cannot carry overnight — force-close any open
# SHORT once this IST time is reached, regardless of stop/target.
# (Jwala confirmed "realistic expectations" — Jul 8.) Set a few
# minutes ahead of the 15:30 close so it lands on a real scan cycle.
SQUARE_OFF_TIME = dtime(15, 15)


def _past_square_off_time() -> bool:
    return datetime.now(IST).time() >= SQUARE_OFF_TIME

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
        what happened (opened / closed / rejected / error), for
        logging/alerting.

        Symmetric BUY/SELL — see module docstring for the state table.
        """
        if side not in ("BUY", "SELL"):
            return {"action": "skip", "reason": f"non-tradeable signal {side}"}

        if not _is_equity(symbol):
            return {"action": "skip", "reason": f"{symbol} not equity — not paper-traded"}

        # ── What (if anything) is currently open for this symbol? ──
        existing = db.get_open_position(symbol)

        if existing is not None:
            held_side = existing["side"]

            # Opposite signal to what's held = reversal exit.
            if held_side != side:
                closed = self.close_by_symbol(symbol, price, reason="reversal")
                if closed:
                    return {"action": "closed", "symbol": symbol,
                            "reason": "reversal", "exit": price}
                return {"action": "error", "reason": f"failed to close {symbol} on reversal"}

            # Same-direction signal while already holding that
            # direction — no duplicate entry, no action.
            return {"action": "skip",
                    "reason": f"{side} for {symbol} but already {held_side} — no re-entry"}

        # ── Flat: BUY opens a LONG, SELL opens a SHORT ─────────────

        # Concurrency cap (applies across longs + shorts combined).
        if db.count_open_paper_positions() >= MAX_OPEN_POSITIONS:
            return {"action": "reject", "reason": f"max {MAX_OPEN_POSITIONS} open positions"}

        # 1. RMS — evaluate() is already symmetric: for SELL it sizes
        # and mirrors stop/target for a short (stop above entry,
        # target below). No change needed there.
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
        For each open position, fetch current price and close it if:
          - a SHORT and we're past SQUARE_OFF_TIME (forced, realistic
            constraint — checked FIRST, ahead of stop/target), or
          - the stop-loss or target has been hit (direction-aware —
            a short's stop is above entry, target below).
        Returns list of close events.
        (Exit-on-opposite-signal is handled via on_signal + close_by_symbol.)
        """
        closed = []
        open_df = db.get_open_paper_positions()
        if open_df is None or open_df.empty:
            return closed

        square_off_now = _past_square_off_time()

        for _, pos in open_df.iterrows():
            symbol = pos["symbol"]
            side   = pos["side"]
            pid    = int(pos["id"])

            # ── Forced square-off: cash-equity shorts can't carry
            # overnight. Close any open SHORT once the square-off
            # window starts, regardless of stop/target. Longs are
            # unaffected — this check only applies to SELL positions.
            if side == "SELL" and square_off_now:
                price = self._current_price(symbol)
                if price is None:
                    continue
                if db.close_paper_position(pid, price, exit_reason="square_off"):
                    qty   = int(pos["quantity"])
                    entry = float(pos["entry_price"])
                    pnl   = (entry - price) * qty
                    self.rms.record_realized_pnl(pnl)
                    closed.append({
                        "symbol": symbol, "reason": "square_off",
                        "exit": price, "pnl": round(pnl, 2),
                    })
                continue  # already resolved — skip the stop/target check below

            price = self._current_price(symbol)
            if price is None:
                continue

            stop   = float(pos["stop_loss"])
            target = float(pos["target"])

            hit = None
            if side == "BUY":
                if price <= stop:   hit = "stop"
                elif price >= target: hit = "target"
            else:  # SELL (short) — mirrored: stop above, target below
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

    # --------------------------------------------------------
    # MANUAL CONTROLS — dashboard buttons (Jwala Jul 8)
    # --------------------------------------------------------
    def close_manual(self, position_id: int, price: float) -> dict:
        """
        Manual "Close" button — book P&L now regardless of stop/target.
        Looked up by position id (not symbol) so it targets exactly the
        row the user clicked, even if — in a future world with re-entry
        timing edge cases — more than one row could match a symbol.
        """
        open_df = db.get_open_paper_positions()
        if open_df is None or open_df.empty:
            return {"action": "error", "reason": "no open positions"}

        match = open_df[open_df["id"] == position_id]
        if match.empty:
            return {"action": "error", "reason": f"position {position_id} not open"}

        pos = match.iloc[0]
        ok  = db.close_paper_position(position_id, price, exit_reason="manual")
        if not ok:
            return {"action": "error", "reason": "close failed"}

        qty   = int(pos["quantity"])
        entry = float(pos["entry_price"])
        side  = pos["side"]
        pnl   = (price - entry) * qty if side == "BUY" else (entry - price) * qty
        self.rms.record_realized_pnl(pnl)

        return {"action": "closed", "symbol": pos["symbol"], "reason": "manual",
                "exit": price, "pnl": round(pnl, 2)}

    def update_stop(self, position_id: int, new_stop: float) -> dict:
        """
        Manual "Edit Stop" button — move the stop by hand (e.g. to
        breakeven once a trade is in profit). Does not touch RMS/order
        logic — it's a direct DB update, no new order is placed.
        """
        ok = db.update_paper_position_stop(position_id, new_stop)
        if not ok:
            return {"action": "error",
                    "reason": f"position {position_id} not open or update failed"}
        return {"action": "stop_updated", "position_id": position_id,
                "new_stop": round(float(new_stop), 2)}

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