# ============================================================
# core/strategies/base_strategy.py
#
# Base class for all trading strategies.
# Every strategy must implement:
#   - name: str
#   - description: str
#   - generate_signal(df) -> SignalResult
# ============================================================

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SignalResult:
    """
    Standardised signal output from any strategy.
    """
    signal:      str              # BUY | SELL | HOLD
    strength:    str              # STRONG | MODERATE | WEAK
    reason:      str              # Human-readable explanation
    indicators:  dict = field(default_factory=dict)  # Key indicator values
    strategy:    str  = ""        # Strategy name (set by engine)

    def is_actionable(self) -> bool:
        return self.signal in ("BUY", "SELL")

    def emoji(self) -> str:
        if self.signal == "BUY":  return "🟢"
        if self.signal == "SELL": return "🔴"
        return "⚪"

    def strength_emoji(self) -> str:
        if self.strength == "STRONG":   return "💪"
        if self.strength == "MODERATE": return "👍"
        return "👌"


class BaseStrategy:
    """
    Abstract base class for all strategies.
    """
    name:        str = "Base Strategy"
    description: str = "Base strategy — override in subclass"

    def generate_signal(self, df) -> SignalResult:
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement generate_signal()"
        )

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r})"