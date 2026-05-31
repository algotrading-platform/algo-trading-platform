# ============================================================
# core/strategies/base_strategy.py
# ============================================================

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SignalResult:
    """Standardised signal output from any strategy."""
    signal:      str              # BUY | SELL | HOLD
    strength:    str              # STRONG | MODERATE | WEAK
    reason:      str              # Human-readable explanation
    indicators:  dict = field(default_factory=dict)
    strategy:    str  = ""
    nifty_trend: str  = "NEUTRAL"  # RISING | FALLING | NEUTRAL
    stock_trend: str  = "NEUTRAL"  # RISING | FALLING | NEUTRAL

    def is_actionable(self) -> bool:
        return self.signal in ("BUY", "SELL")

    def emoji(self) -> str:
        if self.signal == "BUY":  return "🟢"
        if self.signal == "SELL": return "🔴"
        return "⚪"

    def trend_line(self) -> str:
        """One-line trend summary for Telegram."""
        def _arrow(t):
            if t == "RISING":  return "↑"
            if t == "FALLING": return "↓"
            return "→"
        return (
            f"Nifty {_arrow(self.nifty_trend)} {self.nifty_trend.capitalize()}  |  "
            f"Stock {_arrow(self.stock_trend)} {self.stock_trend.capitalize()}"
        )


class BaseStrategy:
    name:        str = "Base Strategy"
    description: str = "Base strategy — override in subclass"

    def generate_signal(self, df) -> SignalResult:
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement generate_signal()"
        )

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r})"