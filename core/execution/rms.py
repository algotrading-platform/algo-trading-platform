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
    STOP_LOSS_PCT        = 0.015         # 1.5% stop from entry
    RISK_REWARD          = 1.2           # default target multiple — RSI Reversal (per Jwala, reduced from 2.0)
    DAILY_MAX_LOSS_PCT   = 0.03          # stop trading after -3% in a day
    MAX_OPEN_POSITIONS   = 15            # single source of truth — paper_trader.py
                                          # imports this instead of its own copy, so
                                          # the concurrency cap and the capital-per-
                                          # trade divisor can never drift apart again.
    MIN_QUANTITY         = 1             # need at least 1 share to trade

    # Strategy-specific reward ratio (Jwala, Jul 11: "once we enter we
    # can keep a 1 is to 2 ratio" for Volume Spike — wider than RSI's
    # 1.2×, since volume-driven moves can run further). Anything not
    # listed here falls back to RISK_REWARD above.
    RISK_REWARD_BY_STRATEGY = {
        "Volume Spike": 2.0,
    }

    # ── RETIRED (Jwala, Jul 9 call) — kept only so nothing importing
    # these old names breaks; no longer used for sizing. Sizing is now
    # CAPITAL / MAX_OPEN_POSITIONS per trade (see evaluate() below),
    # not a risk-budget calc capped at a % of capital. The old
    # combination (risk-budget sizing capped at MAX_POSITION_PCT=20%)
    # is exactly what let deployed capital hit ~₹29-30L on a ₹10L
    # book once 15 positions were open (15 × 20% = 300%) — the bug
    # Jwala caught live from the capital-deployed card.
    RISK_PCT_PER_TRADE   = 0.01          # unused — see MAX_OPEN_POSITIONS above
    MAX_POSITION_PCT     = 0.20          # unused — see MAX_OPEN_POSITIONS above


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
        strategy:    str = None,   # picks the reward ratio — see RISK_REWARD_BY_STRATEGY
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

        # 3. Stop-loss & target — reward ratio is strategy-specific
        # (Jwala Jul 11: Volume Spike gets 1:2, wider than RSI's 1.2×).
        reward_ratio = self.cfg.RISK_REWARD_BY_STRATEGY.get(strategy, self.cfg.RISK_REWARD)
        stop_dist = entry_price * self.cfg.STOP_LOSS_PCT
        if side == "BUY":
            stop_loss = round(entry_price - stop_dist, 2)
            target    = round(entry_price + stop_dist * reward_ratio, 2)
        else:  # SELL (short)
            stop_loss = round(entry_price + stop_dist, 2)
            target    = round(entry_price - stop_dist * reward_ratio, 2)

        # 4. Position size — CAPITAL-BASED, not risk-based (Jwala,
        # Jul 9 call, worked out explicitly on the call): every
        # concurrent slot gets an equal, fixed share of total capital.
        #   capital_per_trade = CAPITAL / MAX_OPEN_POSITIONS
        #   quantity          = floor(capital_per_trade / entry_price)
        # This structurally caps total deployed capital at
        # MAX_OPEN_POSITIONS × capital_per_trade == CAPITAL, regardless
        # of entry price or stop distance — no separate "don't
        # over-concentrate" cap is needed, this IS that cap.
        capital_per_trade = self.cfg.CAPITAL / self.cfg.MAX_OPEN_POSITIONS

        if entry_price <= 0:
            return reject(f"Invalid entry price for sizing: {entry_price}")

        quantity = int(capital_per_trade // entry_price)

        if quantity < self.cfg.MIN_QUANTITY:
            return reject(
                f"Position size < 1 share "
                f"(price ₹{entry_price:,.0f} too high for per-trade capital "
                f"₹{capital_per_trade:,.0f})"
            )

        actual_risk = round(quantity * stop_dist, 2)

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
                "capital_per_trade": round(capital_per_trade, 2),
                "per_share_risk":    round(stop_dist, 2),
                "position_value":    round(quantity * entry_price, 2),
                "reward_if_target":  round(quantity * stop_dist * reward_ratio, 2),
                "reward_ratio":      reward_ratio,
                "daily_pnl_before":  round(self._realized_pnl_today, 2),
            },
        )