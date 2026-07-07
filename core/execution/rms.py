# ============================================================
# core/execution/rms.py
#
# Risk Management System — the decision layer that validates
# EVERY signal BEFORE any order is placed.
#
# Architecture (per plan):
#   Signal → [RMS] → Order Manager → Upstox Sandbox → Tracking
#
# The RMS answers: "Should we take this trade, and if so, how big,
# with what stop-loss and target?" It never places orders itself —
# it returns a decision the Order Manager acts on.
#
# All parameters live in ONE config block below. When Jwala gives
# his numbers, change them here — nothing else.
#
# EQUITY sizing first (buy/sell N shares). Futures/lots is a later
# addition (would size in whole lots using configs.universe lot data).
# ============================================================

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


# ============================================================
# CONFIG — the only place risk parameters live.
# (Defaults are industry-standard; adjust per Jwala's inputs.)
# ============================================================
class RMSConfig:
    CAPITAL              = 1_000_000.0   # ₹10L simulated capital
    RISK_PCT_PER_TRADE   = 0.01          # 1% of capital risked per trade
    STOP_LOSS_PCT        = 0.015         # 1.5% stop from entry
    RISK_REWARD          = 1.2           # target = 1.2× the stop distance (per Jwala, reduced from 2.0)
    DAILY_MAX_LOSS_PCT   = 0.03          # stop trading after -3% in a day
    MAX_POSITION_PCT     = 0.20          # no single position > 20% of capital
    MIN_QUANTITY         = 1             # need at least 1 share to trade


@dataclass
class RMSDecision:
    """What the RMS hands to the Order Manager."""
    approved:    bool
    symbol:      str
    side:        str                 # "BUY" or "SELL"
    entry_price: float
    quantity:    int   = 0
    stop_loss:   float = 0.0
    target:      float = 0.0
    risk_amount: float = 0.0         # ₹ at risk if stop is hit
    reason:      str   = ""          # why approved / rejected
    details:     dict  = field(default_factory=dict)


class RMS:
    """
    Stateful RMS. Tracks realized P&L per day so it can enforce the
    daily loss limit / kill switch. One instance per trading session.
    """

    def __init__(self, config: RMSConfig = RMSConfig):
        self.cfg = config
        self._day = date.today()
        self._realized_pnl_today = 0.0
        self._trading_halted = False

    # --------------------------------------------------------
    # Daily P&L tracking (feeds the daily-loss kill switch)
    # --------------------------------------------------------
    def record_realized_pnl(self, pnl: float) -> None:
        """Call when a paper trade closes, to update the day's P&L."""
        self._roll_day_if_needed()
        self._realized_pnl_today += pnl
        loss_limit = -abs(self.cfg.CAPITAL * self.cfg.DAILY_MAX_LOSS_PCT)
        if self._realized_pnl_today <= loss_limit:
            self._trading_halted = True

    def _roll_day_if_needed(self) -> None:
        if date.today() != self._day:
            self._day = date.today()
            self._realized_pnl_today = 0.0
            self._trading_halted = False

    @property
    def daily_pnl(self) -> float:
        self._roll_day_if_needed()
        return round(self._realized_pnl_today, 2)

    @property
    def halted(self) -> bool:
        self._roll_day_if_needed()
        return self._trading_halted

    # --------------------------------------------------------
    # THE CORE CHECK — validate a signal, return a decision
    # --------------------------------------------------------
    def evaluate(
        self,
        symbol:      str,
        side:        str,          # "BUY" or "SELL"
        entry_price: float,
    ) -> RMSDecision:

        self._roll_day_if_needed()

        def reject(reason: str) -> RMSDecision:
            return RMSDecision(
                approved=False, symbol=symbol, side=side,
                entry_price=entry_price, reason=reason,
            )

        # 1. Kill switch: daily loss limit already breached?
        if self._trading_halted:
            return reject(
                f"Trading halted — daily loss limit "
                f"({self.cfg.DAILY_MAX_LOSS_PCT*100:.0f}%) reached "
                f"(P&L today ₹{self._realized_pnl_today:,.0f})"
            )

        # 2. Sanity on price
        if entry_price is None or entry_price <= 0:
            return reject(f"Invalid entry price: {entry_price}")

        if side not in ("BUY", "SELL"):
            return reject(f"Unsupported side: {side}")

        # 3. Stop-loss & target
        stop_dist = entry_price * self.cfg.STOP_LOSS_PCT
        if side == "BUY":
            stop_loss = round(entry_price - stop_dist, 2)
            target    = round(entry_price + stop_dist * self.cfg.RISK_REWARD, 2)
        else:  # SELL (short)
            stop_loss = round(entry_price + stop_dist, 2)
            target    = round(entry_price - stop_dist * self.cfg.RISK_REWARD, 2)

        # 4. Position size = risk budget ÷ per-share risk
        risk_budget   = self.cfg.CAPITAL * self.cfg.RISK_PCT_PER_TRADE
        per_share_risk = stop_dist
        if per_share_risk <= 0:
            return reject("Stop distance is zero — cannot size")

        quantity = int(risk_budget // per_share_risk)

        # 5. Cap position value (don't over-concentrate)
        max_position_value = self.cfg.CAPITAL * self.cfg.MAX_POSITION_PCT
        if quantity * entry_price > max_position_value:
            quantity = int(max_position_value // entry_price)

        if quantity < self.cfg.MIN_QUANTITY:
            return reject(
                f"Position size < 1 share "
                f"(price ₹{entry_price:,.0f} too high for risk budget "
                f"₹{risk_budget:,.0f})"
            )

        actual_risk = round(quantity * per_share_risk, 2)

        return RMSDecision(
            approved=True,
            symbol=symbol,
            side=side,
            entry_price=round(entry_price, 2),
            quantity=quantity,
            stop_loss=stop_loss,
            target=target,
            risk_amount=actual_risk,
            reason="approved",
            details={
                "risk_budget":      round(risk_budget, 2),
                "per_share_risk":   round(per_share_risk, 2),
                "position_value":   round(quantity * entry_price, 2),
                "reward_if_target": round(quantity * stop_dist * self.cfg.RISK_REWARD, 2),
                "daily_pnl_before": round(self._realized_pnl_today, 2),
            },
        )