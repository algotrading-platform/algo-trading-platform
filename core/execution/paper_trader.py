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
# EOD SQUARE-OFF — SHORT ONLY (reverted Jul 24: "short should be
# closed once market closes, long can be carried forward"). History:
# started SHORT-only (Jul 8, regulatory — NSE cash-equity shorts can't
# carry overnight), was widened to BOTH sides on Jul 11 ("this algo
# system would be for a day trade only"), and is reverted back to
# SHORT-only here. Longs now exit only via stop/target/manual/kill-
# switch, with no forced same-day or same-week close at all — an
# open-ended hold, by design, not yet subject to any maximum holding
# period (worth confirming there isn't meant to be one). The
# start-of-day catch-up sweep below follows the same SHORT-only rule,
# for the same reason — a carried-forward long is now correct
# behavior, not stale state to clean up.
#
# VOLUME SPIKE TRAILING STOP (Jwala Jul 11): Volume Spike positions
# get a wider reward ratio (1:2, set in rms.py) BUT that target is
# reference-only, not a hard close — the trailing stop is the real
# exit (21:16-21:43: "stock can run up to 10%, 15%... we'll have to
# go for trailing the stop loss... to capture big moves"). A hard 1:2
# close would defeat that: the position would exit at 2x before the
# trailing logic ever got a chance to ride further. So: once price
# has moved favorably by at least the original stop distance, the
# stop trails behind the peak instead of staying fixed, and that
# trailing stop is the only thing that closes a Volume Spike position
# early (plus the universal EOD square-off). RSI Reversal positions
# are unaffected (fixed stop/target throughout, as before). The exact
# trailing formula wasn't nailed down on the call ("we can design
# that trailing stop loss kind of thing" — confirms the concept, not
# a formula) — this is my own concrete choice, flagged for Jwala to
# confirm/adjust: breakeven once price has moved 1× the initial risk,
# then trail behind the peak by that same distance.
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

from core.execution.rms import RMS, RMSConfig, shared_rms
from core.execution.order_manager import OrderManager
from core.execution.sandbox_client import SandboxClient
from core.database import db

log = logging.getLogger("paper_trader")

IST = pytz.timezone("Asia/Kolkata")

# Max concurrent open paper positions PER STRATEGY — single source of
# truth is RMSConfig.MAX_OPEN_POSITIONS_PER_STRATEGY (rms.py). Each of
# the 3 parallel strategies gets its own 5-slot cap (Om, Jul 27), not a
# shared 15-slot pool — unified with RMS's own sizing divisor so the
# concurrency cap and the capital-per-trade divisor can never drift
# apart, same reasoning as the original Jul 9 unification.
MAX_OPEN_POSITIONS_PER_STRATEGY = RMSConfig.MAX_OPEN_POSITIONS_PER_STRATEGY

# Cash-equity shorts cannot carry overnight — force-close any open
# SHORT once this IST time is reached, regardless of stop/target.
# (Jwala confirmed "realistic expectations" — Jul 8.) Set a few
# minutes ahead of the 15:30 close so it lands on a real scan cycle.
SQUARE_OFF_TIME = dtime(15, 15)  # was 15:00 — moved to match the new
                                 # trading window's end (Jwala, Jul 17:
                                 # "start at 9:45... at 3:15 PM")


def _past_square_off_time() -> bool:
    return datetime.now(IST).time() >= SQUARE_OFF_TIME


def _is_from_previous_day(opened_at) -> bool:
    """
    True if opened_at's IST calendar date is before today's IST
    calendar date. Feeds the start-of-day catch-up sweep below —
    distinct from same-day square-off, this catches anything that
    somehow survived into a NEW day (Jwala/Om, Jul 14: positions
    opened before last night's deploy were still sitting open the
    next morning, since new code doesn't retroactively touch rows
    already in the DB — this sweep is what actually clears them, the
    first time it runs, rather than waiting on a fresh 3PM boundary).
    """
    try:
        if opened_at.tzinfo is None:
            opened_at = pytz.utc.localize(opened_at)
        return opened_at.astimezone(IST).date() < datetime.now(IST).date()
    except Exception:
        return False

# Only equities are paper-traded. Skip index/commodity symbols.
def _is_equity(symbol: str) -> bool:
    if symbol.startswith("^"):        # ^NSEI, ^BSESN — indexes
        return False
    if "=" in symbol:                 # GC=F, SI=F — commodities
        return False
    return symbol.endswith(".NS")     # NSE equity


class PaperTrader:

    def __init__(self, provider=None, rms: RMS = None):
        # Defaults to the module-level shared_rms singleton (see rms.py)
        # so the entry-side PaperTrader (strategy_engine.py) and the
        # monitor-side PaperTrader (signal_scheduler.py) see the SAME
        # daily P&L / halted state — pass an explicit rms= for tests
        # that need an isolated instance.
        self.rms  = rms if rms is not None else shared_rms
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
        symbol:      str,
        side:        str,        # "BUY" / "SELL"
        price:       float,
        strategy:    str,
        timeframe:   str,
        strength:    str   = None,  # signal grade — picks unit count in RMS
                                    # (Jwala, Jul 14: MODERATE=1, STRONG=2,
                                    # VERY STRONG=3 units)
        custom_stop: float = None,  # strategy-native stop level (Jwala, Jul
                                    # 24: 3 Bar Play uses its own pullback-bar
                                    # stop, not RMS's generic % stop) — passed
                                    # straight through to RMS.evaluate(); see
                                    # that function's docstring for how target
                                    # gets recomputed around it.
        custom_target: float = None,  # strategy-native target level (Jwala,
                                    # Aug 26: 3 Bar Play's target is an exact
                                    # 70% of the flagpole candle's range, not
                                    # a reward-ratio multiple of risk) —
                                    # passed straight through to RMS.evaluate().
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

        # Concurrency cap — per-strategy (applies across longs + shorts
        # combined WITHIN this strategy; each strategy has its own
        # 5-slot pool, Om Jul 27). Cheap non-atomic pre-check to skip
        # RMS/sandbox work when obviously full; the authoritative check
        # is the atomic one at insert time below (this one alone raced
        # across concurrent scan jobs and let the cap overshoot to 14).
        if db.count_open_paper_positions(strategy=strategy) >= MAX_OPEN_POSITIONS_PER_STRATEGY:
            return {"action": "reject",
                    "reason": f"max {MAX_OPEN_POSITIONS_PER_STRATEGY} open positions for {strategy}"}

        # 1. RMS — evaluate() is already symmetric: for SELL it sizes
        # and mirrors stop/target for a short (stop above entry,
        # target below). strategy picks the reward ratio (Jwala Jul
        # 11: Volume Spike gets 1:2, wider than RSI's 1.2×). strength
        # picks the unit count (Jwala Jul 14); capital_deployed is
        # fetched fresh here, SCOPED TO THIS STRATEGY (Jul 27: pools
        # are per-strategy), so RMS can cap grade-based sizing against
        # what's actually still available in this strategy's own pool.
        capital_deployed = db.get_capital_deployed(strategy=strategy)
        decision = self.rms.evaluate(
            symbol, side, price,
            strategy=strategy, strength=strength,
            capital_deployed=capital_deployed,
            custom_stop=custom_stop,
            custom_target=custom_target,
        )
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

        # 5. Record the open position in the DB — atomic with a fresh
        # cap re-check (see open_paper_position_if_capacity docstring):
        # closes the race where two concurrent scan jobs both pass the
        # pre-check above and jointly overshoot the strategy's cap.
        insert = db.open_paper_position_if_capacity(
            symbol=symbol,
            side=side,
            quantity=order.quantity,
            entry_price=order.price,
            stop_loss=order.stop_loss,
            target=order.target,
            strategy=strategy,
            timeframe=timeframe,
            max_positions=MAX_OPEN_POSITIONS_PER_STRATEGY,
            risk_amount=decision.risk_amount,
            order_id=result["order_id"],
        )
        if not insert["opened"]:
            action = "reject" if insert.get("cause") == "cap" else "error"
            return {"action": action, "reason": insert["reason"]}

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
          - it's a SHORT and we're past SQUARE_OFF_TIME (regulatory —
            NSE cash-equity shorts can't carry overnight — checked
            FIRST, ahead of stop/target; longs are exempt as of Jul 24
            and can carry forward indefinitely), or
          - the stop-loss or target has been hit (direction-aware —
            a short's stop is above entry, target below).
        Volume Spike positions get their trailing stop updated first
        (may raise the stop before the hit-check below runs, so a
        newly-trailed stop can close the SAME cycle if price has
        already retraced through it).
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

            # ── Start-of-day catch-up sweep — SHORT ONLY (reverted
            # Jul 24: "short should be closed once market closes, long
            # can be carried forward"). This REVERSES the Jul 11/14
            # decision that made this apply to both sides — longs are
            # now explicitly allowed to persist across days, so the
            # sweep must no longer force-close a carried-forward long
            # just for being from an earlier calendar day; only a
            # SHORT surviving past its mandatory same-day close is a
            # genuine stale-carryover case anymore.
            if side == "SELL" and _is_from_previous_day(pos["opened_at"]):
                price = self._current_price(symbol)
                if price is None:
                    log.warning(f"stale_carryover check: no price for {symbol}, skipped this cycle")
                    continue
                if db.close_paper_position(pid, price, exit_reason="stale_carryover"):
                    qty   = int(pos["quantity"])
                    entry = float(pos["entry_price"])
                    pnl   = (entry - price) * qty  # SELL/short only, mirrored math
                    self.rms.record_realized_pnl(pnl)
                    self.om.clear_key(symbol, side, f"{pos['timeframe']}|{pos['strategy']}")
                    closed.append({
                        "symbol": symbol, "reason": "stale_carryover",
                        "exit": price, "pnl": round(pnl, 2),
                    })
                continue  # already resolved — skip everything below for this position

            # ── Forced square-off — SHORT ONLY (reverted Jul 24: "short
            # should be closed once market closes, long can be carried
            # forward"). This REVERSES the Jul 11 "day-trade-only, both
            # sides" decision — back to the original regulatory-driven
            # rule: NSE cash-equity shorts cannot legally carry
            # overnight and must close same-day regardless of anything
            # else; longs have no such requirement and are now allowed
            # to run past today, exiting only via stop/target/manual/
            # kill-switch.
            if side == "SELL" and square_off_now:
                price = self._current_price(symbol)
                if price is None:
                    log.warning(f"square_off check: no price for {symbol}, skipped this cycle — still open past cutoff")
                    continue
                if db.close_paper_position(pid, price, exit_reason="square_off"):
                    qty   = int(pos["quantity"])
                    entry = float(pos["entry_price"])
                    pnl   = (entry - price) * qty  # SELL/short only, mirrored math
                    self.rms.record_realized_pnl(pnl)
                    self.om.clear_key(symbol, side, f"{pos['timeframe']}|{pos['strategy']}")
                    closed.append({
                        "symbol": symbol, "reason": "square_off",
                        "exit": price, "pnl": round(pnl, 2),
                    })
                continue  # already resolved — skip the stop/target check below

            price = self._current_price(symbol)
            if price is None:
                log.warning(f"stop/target check: no price for {symbol}, skipped this cycle")
                continue

            stop   = float(pos["stop_loss"])
            target = float(pos["target"])

            # ── Volume Spike: trailing stop is the REAL exit, not
            # the fixed target (Jwala Jul 11, 21:16-21:43: "the stock
            # can run up to 10%, 15%... we'll have to go for trailing
            # the stop loss... so that we can capture big moves").
            # A hard 1:2 target would close the position before the
            # trailing stop ever gets a chance to ride a bigger move —
            # defeats the purpose. So for Volume Spike, `target` is
            # kept on the row for reference/display only; the actual
            # hit-check below skips it and relies purely on the
            # (possibly-trailed) stop plus the universal EOD square-off.
            is_volume_spike = str(pos.get("strategy", "")) == "Volume Spike"
            if is_volume_spike:
                stop = self._apply_trailing_stop(pos, price, side, stop)

            hit = None
            if side == "BUY":
                if price <= stop:
                    hit = "stop"
                elif not is_volume_spike and price >= target:
                    hit = "target"
            else:  # SELL (short) — mirrored: stop above, target below
                if price >= stop:
                    hit = "stop"
                elif not is_volume_spike and price <= target:
                    hit = "target"

            if hit:
                if db.close_paper_position(pid, price, exit_reason=hit):
                    # update RMS daily P&L for the kill switch
                    qty   = int(pos["quantity"])
                    entry = float(pos["entry_price"])
                    pnl   = (price - entry) * qty if side == "BUY" else (entry - price) * qty
                    self.rms.record_realized_pnl(pnl)
                    self.om.clear_key(symbol, side, f"{pos['timeframe']}|{pos['strategy']}")
                    closed.append({
                        "symbol": symbol, "reason": hit,
                        "exit": price, "pnl": round(pnl, 2),
                    })

        # ── Kill switch: daily loss limit breached (by a close just
        # above, or already halted from an earlier cycle this same
        # day) — force-close everything still open. Jwala, Aug 19:
        # the hard stop should "kill all the existing paper trades",
        # not just block new entries (that alone was step 1 above,
        # via RMS.evaluate() rejecting on_signal() while halted).
        if self.rms.halted:
            closed.extend(self._kill_switch_close_all())

        return closed

    def _kill_switch_close_all(self) -> list[dict]:
        """Force-close every remaining open paper position at current
        market price. Called once the RMS daily-loss kill switch has
        tripped — positions are closed regardless of their own
        stop/target, since the portfolio-level limit overrides them."""
        closed = []
        open_df = db.get_open_paper_positions()
        if open_df is None or open_df.empty:
            return closed

        for _, pos in open_df.iterrows():
            symbol = pos["symbol"]
            side   = pos["side"]
            pid    = int(pos["id"])

            price = self._current_price(symbol)
            if price is None:
                log.warning(f"kill_switch: no price for {symbol}, could not force-close this cycle")
                continue

            if db.close_paper_position(pid, price, exit_reason="kill_switch"):
                qty   = int(pos["quantity"])
                entry = float(pos["entry_price"])
                pnl   = (price - entry) * qty if side == "BUY" else (entry - price) * qty
                self.rms.record_realized_pnl(pnl)
                self.om.clear_key(symbol, side, f"{pos['timeframe']}|{pos['strategy']}")
                closed.append({
                    "symbol": symbol, "reason": "kill_switch",
                    "exit": price, "pnl": round(pnl, 2),
                })
        return closed

    def _apply_trailing_stop(self, pos, current_price: float, side: str, current_stop: float) -> float:
        """
        Volume Spike trailing stop (Jwala Jul 11 — concept confirmed
        on the call, exact formula is my own design choice, flagged
        for confirmation):

          1. Track peak_price — the best price seen since entry
             (highest for LONG, lowest for SHORT).
          2. Once price has moved favorably by at least
             initial_stop_distance (the entry-to-stop gap at open
             time — snapshotted once, never recomputed, so a moving
             stop doesn't change what counts as "moved enough"),
             move the stop to breakeven (entry price).
          3. Beyond that, trail behind the peak by the same
             initial_stop_distance.

        Persists peak_price every call, and stop_loss only when it
        actually changes. Returns the stop to use for THIS cycle's
        hit-check (may be unchanged from current_stop).
        """
        pid = int(pos["id"])
        entry = float(pos["entry_price"])
        risk_dist = pos.get("initial_stop_distance")
        peak = pos.get("peak_price")

        # Defensive fallback for any row opened before this migration
        # (peak_price/initial_stop_distance NULL) — skip trailing
        # rather than guess, this cycle's ordinary stop/target still
        # applies via current_stop.
        if risk_dist is None or peak is None:
            return current_stop

        risk_dist = float(risk_dist)
        peak      = float(peak)
        new_stop  = current_stop
        peak_changed = False

        if side == "BUY":
            if current_price > peak:
                peak, peak_changed = current_price, True
            moved_favorably = peak - entry
            if moved_favorably >= risk_dist:
                breakeven_or_better = max(entry, peak - risk_dist)
                if breakeven_or_better > new_stop:
                    new_stop = round(breakeven_or_better, 2)
        else:  # SELL (short) — mirrored: peak is the LOWEST price seen
            if current_price < peak:
                peak, peak_changed = current_price, True
            moved_favorably = entry - peak
            if moved_favorably >= risk_dist:
                breakeven_or_better = min(entry, peak + risk_dist)
                if breakeven_or_better < new_stop:
                    new_stop = round(breakeven_or_better, 2)

        stop_changed = new_stop != current_stop
        if peak_changed or stop_changed:
            db.update_trailing_state(
                pid, peak_price=peak,
                new_stop=new_stop if stop_changed else None,
            )

        return new_stop

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
        self.om.clear_key(pos["symbol"], side, f"{pos['timeframe']}|{pos['strategy']}")

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
            self.om.clear_key(symbol, pos["side"], f"{pos['timeframe']}|{pos['strategy']}")
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
        # Fast path: latest price from the WS listener's live table
        # (core/marketdata/ws_listener.py), a few seconds stale at
        # most. Falls through to the REST/yfinance path below if the
        # listener is down or hasn't seen this symbol recently.
        try:
            live_price = db.get_latest_live_price(symbol)
            if live_price is not None:
                return live_price
        except Exception as e:
            log.warning(f"live price lookup failed for {symbol}: {e}")

        if self.provider is None:
            return None
        try:
            df = self.provider.fetch_data(symbol=symbol, interval="5m", period="1d")
            if df is not None and not df.empty:
                return round(float(df["Close"].iloc[-1]), 2)
            log.warning(f"Price fetch returned nothing for {symbol} after Upstox+yfinance fallback")
        except Exception as e:
            log.warning(f"_current_price failed for {symbol}: {e}")
        return None