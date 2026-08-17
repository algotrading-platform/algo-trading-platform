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
    # Per-strategy pools (Om, Jul 27: "let each strategy have only 5
    # positions opened out of 15... let's allot 20 lakhs for each
    # strategy") — each of the 4 parallel strategies (RSI + MA, Volume
    # Spike, Experiment 3 Bar Play, 3 Bar Play) gets its OWN ₹20L
    # capital pool and its own 5-position concurrency cap, tracked
    # independently per strategy rather than pooled. paper_trader.py
    # passes strategy-scoped counts/deployed-capital into evaluate()
    # below so sizing and the concurrency check are both per-strategy.
    # (Was 3 parallel strategies; became 4 when "3 Bar Play" was
    # relabeled "Experiment 3 Bar Play" and a new "3 Bar Play" strategy
    # was added alongside it.)
    CAPITAL_PER_STRATEGY            = 2_000_000.0  # ₹20L per strategy
    MAX_OPEN_POSITIONS_PER_STRATEGY = 5            # per strategy

    # Aggregate figures — derived from the per-strategy numbers above,
    # not independent settings. Kept for anything that still wants a
    # single portfolio-wide total (e.g. the dashboard's "Total Capital"
    # card): 4 strategies x ₹20L = ₹80L, 4 x 5 = 20 total slots.
    CAPITAL            = CAPITAL_PER_STRATEGY * 4
    MAX_OPEN_POSITIONS = MAX_OPEN_POSITIONS_PER_STRATEGY * 4

    STOP_LOSS_PCT        = 0.01          # 1% stop from entry (Jwala, Jul 17:
                                          # "reducing the stop so that when we
                                          # are wrong, we would exit quickly" —
                                          # was 1.5%)
    RISK_REWARD          = 1.2           # default target multiple — RSI Reversal.
                                          # Was 1.2, briefly raised to 1.5 on Jul 17
                                          # ("when we are right, we would be taking
                                          # a bigger profit"), then reverted back to
                                          # 1.2 shortly after (Jwala's own follow-up
                                          # correction) — net effect across both
                                          # calls is no change from the original 1.2.
    DAILY_MAX_LOSS_PCT   = 0.03          # stop trading after -3% in a day
    MIN_QUANTITY         = 1             # need at least 1 share to trade

    # Strategy-specific reward ratio (Jwala, Jul 11: "once we enter we
    # can keep a 1 is to 2 ratio" for Volume Spike — wider than RSI's
    # 1.2×, since volume-driven moves can run further). Anything not
    # listed here falls back to RISK_REWARD above.
    RISK_REWARD_BY_STRATEGY = {
        "Volume Spike":            2.0,
        "Experiment 3 Bar Play":   2.0,  # matches Jwala's own reference code example
                                         # (reward_multiple=2); spec said "2x-3x,
                                         # configurable" with no single number chosen —
                                         # this is the conservative end, easy to bump.
        "3 Bar Play":              1.5,  # new volatility-contraction strategy —
                                         # tighter target than the continuation
                                         # pattern above, fits a breakout-from-
                                         # compression setup.
    }

    # Grade-based position sizing (Jwala, Jul 14, 4:19-5:22): "If the
    # signal is very strong, we'll allocate 3 units. If the signal is
    # strong, we'll allocate 2 units. If the signal is moderate, then
    # one unit of capital... The risk is still 1.5%[now 1%] only" — so
    # the STOP-LOSS % never changes, only how many "units" (each unit
    # = CAPITAL/MAX_OPEN_POSITIONS) get allocated. Anything not listed
    # (or ungraded) defaults to 1 unit. WEAK never reaches here at all
    # — filtered out upstream in strategy_engine.py before on_signal()
    # is ever called.
    UNITS_BY_STRENGTH = {
        "VERY STRONG": 3,
        "STRONG":      2,
        "MODERATE":    1,
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
        symbol:           str,
        side:             str,          # "BUY" or "SELL"
        entry_price:      float,
        strategy:         str   = None, # picks the reward ratio — see RISK_REWARD_BY_STRATEGY
        strength:         str   = None, # picks unit count — see UNITS_BY_STRENGTH
        capital_deployed: float = 0.0,  # currently deployed for THIS strategy only (Jul 27:
                                         # pools are per-strategy now) — caller (paper_trader.py)
                                         # fetches this from the DB scoped to `strategy`. Needed
                                         # so a 2x/3x-unit allocation can't push this strategy's
                                         # deployed capital past its own CAPITAL_PER_STRATEGY —
                                         # see step 4 below.
        custom_stop:      float = None, # strategy-supplied stop level (Jwala, Jul 24: 3 Bar
                                         # Play should use its OWN natural stop — pullback bar's
                                         # opposite extreme — rather than RMS's generic %-based
                                         # one). When provided, this OVERRIDES the % calculation
                                         # below entirely; target is still recomputed from
                                         # reward_ratio × the ACTUAL distance between entry_price
                                         # and custom_stop, so risk:reward stays correct relative
                                         # to whatever price the trade actually fills at — not
                                         # the strategy's own theoretical entry (which may differ
                                         # slightly, since e.g. 3 Bar Play's "entry" is a breakout
                                         # LEVEL, but by the time the signal fires and this runs,
                                         # price has typically already moved past that level).
                                         # Generic (not strategy-name-gated): ANY strategy that
                                         # supplies a custom_stop gets this treatment, not just
                                         # "3 Bar Play" specifically — keeps this reusable for
                                         # whatever pattern-based strategy comes next.
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

        if custom_stop is not None:
            # Strategy-native stop (e.g. 3 Bar Play's pullback-bar
            # extreme) — use it directly instead of the %-based calc,
            # but still recompute target from the ACTUAL entry price
            # so the reward ratio stays meaningful (see param note above).
            stop_loss = round(float(custom_stop), 2)
            stop_dist = abs(entry_price - stop_loss)
            if stop_dist <= 0:
                return reject(f"Invalid custom stop (no distance from entry): {custom_stop}")
            if side == "BUY":
                target = round(entry_price + stop_dist * reward_ratio, 2)
            else:  # SELL (short)
                target = round(entry_price - stop_dist * reward_ratio, 2)
        else:
            stop_dist = entry_price * self.cfg.STOP_LOSS_PCT
            if side == "BUY":
                stop_loss = round(entry_price - stop_dist, 2)
                target    = round(entry_price + stop_dist * reward_ratio, 2)
            else:  # SELL (short)
                stop_loss = round(entry_price + stop_dist, 2)
                target    = round(entry_price - stop_dist * reward_ratio, 2)

        # 4. Position size — CAPITAL-BASED (Jwala, Jul 9), now scaled by
        # signal grade (Jwala, Jul 14): base unit = this strategy's own
        # CAPITAL_PER_STRATEGY / MAX_OPEN_POSITIONS_PER_STRATEGY; a
        # STRONG/VERY STRONG signal gets 2x/3x that unit, per
        # UNITS_BY_STRENGTH.
        #
        # A 3-unit allocation across many concurrent VERY STRONG
        # signals could, in the worst case, push this strategy's
        # deployed capital well past its own pool if sized blindly (5
        # slots × 3 units would be 3x over) — Jwala's spec covers the
        # per-trade risk % staying fixed, but doesn't address this
        # aggregate case, so this is my own addition: cap the capital
        # actually used at whatever's genuinely still AVAILABLE in
        # THIS strategy's pool (CAPITAL_PER_STRATEGY minus
        # capital_deployed), sizing DOWN gracefully rather than
        # blindly honoring the full 2x/3x request once capacity is
        # tight. This preserves the "deployed never exceeds the pool"
        # guarantee from the Jul 9 fix even with grade-based sizing
        # layered on top — now enforced per-strategy instead of
        # portfolio-wide.
        base_unit  = self.cfg.CAPITAL_PER_STRATEGY / self.cfg.MAX_OPEN_POSITIONS_PER_STRATEGY
        units      = self.cfg.UNITS_BY_STRENGTH.get(strength, 1)
        desired_capital = base_unit * units

        available = max(0.0, self.cfg.CAPITAL_PER_STRATEGY - capital_deployed)
        capital_to_use = min(desired_capital, available)

        if entry_price <= 0:
            return reject(f"Invalid entry price for sizing: {entry_price}")

        quantity = int(capital_to_use // entry_price)

        if quantity < self.cfg.MIN_QUANTITY:
            return reject(
                f"Position size < 1 share "
                f"(price ₹{entry_price:,.0f} too high for available capital "
                f"₹{capital_to_use:,.0f} — wanted {units} unit(s) = "
                f"₹{desired_capital:,.0f}, but only ₹{available:,.0f} available)"
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
                "base_unit":         round(base_unit, 2),
                "units":             units,
                "desired_capital":   round(desired_capital, 2),
                "capital_used":      round(quantity * entry_price, 2),
                "per_share_risk":    round(stop_dist, 2),
                "position_value":    round(quantity * entry_price, 2),
                "reward_if_target":  round(quantity * stop_dist * reward_ratio, 2),
                "reward_ratio":      reward_ratio,
                "daily_pnl_before":  round(self._realized_pnl_today, 2),
            },
        )