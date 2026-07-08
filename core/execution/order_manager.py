# ============================================================
# core/execution/order_manager.py
#
# Order Manager — the safety layer between RMS and the broker API.
#
# Architecture:
#   Signal → RMS → [Order Manager] → Upstox Sandbox → Tracking
#
# Responsibilities (per plan):
#   - lot-size validation (futures in lots; equity in shares)
#   - LIMIT orders by default (safety — never accidental market order)
#   - idempotency (never double-fire the same signal)
#
# This layer does NOT call the broker. It takes an approved RMSDecision
# and produces a clean, validated OrderRequest that the sandbox client
# executes. Kept broker-agnostic so sandbox→live is a client swap only.
# ============================================================

from dataclasses import dataclass, field
from typing import Optional

from configs.universe import get_lot_size


@dataclass
class OrderRequest:
    """A validated, ready-to-place order (broker-agnostic)."""
    valid:         bool
    symbol:        str
    side:          str                 # "BUY" / "SELL"
    quantity:      int
    order_type:    str   = "LIMIT"     # LIMIT by default (safety)
    price:         float = 0.0         # limit price
    product:       str   = "D"         # delivery (paper); intraday would be "I"
    stop_loss:     float = 0.0
    target:        float = 0.0
    idempotency_key: str = ""
    instrument:    str   = "EQUITY"    # EQUITY | FUTURES
    reason:        str   = ""
    details:       dict  = field(default_factory=dict)


class OrderManager:
    """
    Builds validated orders from RMS decisions.

    Idempotency: we track keys of orders already placed this session so the
    same signal can never open two positions. The key is
    (symbol, side, timeframe, strategy, signal_bucket) — the caller passes a
    signal identity; we also expose a check against currently-open positions
    (via a callable) so a re-fire while a position is already open is blocked.
    """

    def __init__(self, is_open_position_fn=None):
        # optional callback: symbol -> bool ("is there an open position?")
        # wired to the Position Tracker so we don't re-enter an open name.
        self._is_open = is_open_position_fn
        self._placed_keys: set[str] = set()

    def _round_to_lot(self, quantity: int, symbol: str, instrument: str) -> tuple[int, str]:
        """
        Equity: any whole share count is valid.
        Futures: quantity must be a whole multiple of the lot size.
        Returns (adjusted_qty, note).
        """
        if instrument != "FUTURES":
            return int(quantity), ""

        lot = get_lot_size(symbol)
        if lot <= 0:
            return int(quantity), "unknown lot size — left as-is"

        lots = int(quantity // lot)
        if lots < 1:
            return 0, f"below 1 lot (lot size {lot})"
        adj = lots * lot
        note = "" if adj == quantity else f"rounded to {lots} lot(s) = {adj} (lot {lot})"
        return adj, note

    def build_order(
        self,
        decision,                      # RMSDecision (approved)
        signal_identity: str,          # unique-ish id of the signal that fired
        instrument: str = "EQUITY",
    ) -> OrderRequest:
        """
        Turn an approved RMS decision into a validated LIMIT order.
        Rejects (valid=False) on: unapproved decision, duplicate signal,
        already-open position, or invalid lot sizing.
        """

        def reject(reason: str) -> OrderRequest:
            return OrderRequest(
                valid=False, symbol=getattr(decision, "symbol", "?"),
                side=getattr(decision, "side", "?"), quantity=0,
                reason=reason,
            )

        # 1. Only place if RMS approved
        if not getattr(decision, "approved", False):
            return reject(f"RMS did not approve: {getattr(decision, 'reason', '')}")

        symbol = decision.symbol
        side   = decision.side

        # 2. Idempotency — same signal must not fire twice
        key = f"{symbol}|{side}|{signal_identity}"
        if key in self._placed_keys:
            return reject(f"duplicate signal (already placed): {key}")

        # 3. Don't open a second position in a name we're already in
        if self._is_open is not None:
            try:
                if self._is_open(symbol):
                    return reject(f"position already open for {symbol}")
            except Exception:
                pass  # if the check fails, don't block — tracker will dedupe

        # 4. Lot-size validation
        qty, lot_note = self._round_to_lot(decision.quantity, symbol, instrument)
        if qty < 1:
            return reject(f"quantity invalid after lot check: {lot_note}")

        # 5. LIMIT price = entry price (safety: never a bare market order)
        limit_price = round(float(decision.entry_price), 2)

        # 6. Product type — realistic constraint (Jwala Jul 8: "let's add
        # both buying and selling"). Cash-equity SELL-to-open (a short)
        # cannot be a delivery ("D") order on NSE — it must be intraday
        # ("I") and squared off same-day. BUY (long) stays delivery.
        # PaperTrader.monitor_open() enforces the matching square-off by
        # ~15:15 IST so the paper track record stays realistic.
        product = "I" if side == "SELL" else "D"

        # Record the key so a re-fire is blocked
        self._placed_keys.add(key)

        return OrderRequest(
            valid=True,
            symbol=symbol,
            side=side,
            quantity=qty,
            order_type="LIMIT",
            price=limit_price,
            product=product,
            stop_loss=decision.stop_loss,
            target=decision.target,
            idempotency_key=key,
            instrument=instrument,
            reason="ok" + (f"; {lot_note}" if lot_note else ""),
            details={
                "risk_amount": getattr(decision, "risk_amount", 0.0),
                **getattr(decision, "details", {}),
            },
        )

    def clear_key(self, symbol: str, side: str, signal_identity: str) -> None:
        """Allow a signal to fire again (e.g. after its position closes)."""
        self._placed_keys.discard(f"{symbol}|{side}|{signal_identity}")