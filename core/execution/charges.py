# ============================================================
# core/execution/charges.py
#
# Estimates real-world brokerage + statutory charges for a round-trip
# intraday equity trade, so paper trading can show Gross P&L (what
# the old "pnl" meant) alongside a realistic Net P&L (after charges).
#
# Jwala, Jul 11 call — walked through Upstox's brokerage calculator
# live: 2000 qty, buy@100 (₹2,00,000) / sell@104 (₹2,08,000) →
# brokerage ₹40, other charges ₹82 (₹122 total on ~₹4.08L turnover).
# "For paper trading we can keep these values as zeros, but... I
# think we can keep it here also so that we will get exactly [real
# figures]."
#
# IMPORTANT — this is an ESTIMATE, not a live Upstox API call:
#   - Brokerage: flat ₹20 per executed order (buy leg + sell leg)
#   - STT (Securities Transaction Tax): 0.025% on the SELL leg only
#   - Exchange transaction charges (NSE): ~0.00297% of turnover
#   - SEBI charges: ~0.0001% of turnover
#   - Stamp duty: 0.003% on the BUY leg only
#   - GST: 18% on (brokerage + exchange charges)
# These rates match Upstox's intraday-equity structure as of the
# numbers read out on the call, but brokers change rates — if this
# needs to be exact rather than "close enough for paper trading",
# verify against Upstox's current published rate card before relying
# on it for real-money decisions.
# ============================================================

BROKERAGE_PER_ORDER = 20.0
STT_PCT             = 0.00025    # sell leg only, intraday equity
EXCHANGE_TXN_PCT     = 0.0000297  # both legs
SEBI_PCT             = 0.000001   # both legs
STAMP_DUTY_PCT       = 0.00003    # buy leg only
GST_PCT              = 0.18       # on (brokerage + exchange charges)


def estimate_charges(buy_value: float, sell_value: float) -> float:
    """
    buy_value / sell_value: the rupee value of whichever leg was the
    BUY and whichever was the SELL — NOT "entry"/"exit". For a LONG,
    buy_value=entry*qty, sell_value=exit*qty. For a SHORT, it's the
    reverse: the opening leg was a SELL, the closing leg was a BUY.
    Caller is responsible for passing the right one to the right side
    (see estimate_charges_for_trade below, which does this for you).
    """
    turnover = buy_value + sell_value

    brokerage        = BROKERAGE_PER_ORDER * 2
    stt              = sell_value * STT_PCT
    exchange_charges = turnover * EXCHANGE_TXN_PCT
    sebi_charges     = turnover * SEBI_PCT
    stamp_duty       = buy_value * STAMP_DUTY_PCT
    gst              = GST_PCT * (brokerage + exchange_charges)

    total = brokerage + stt + exchange_charges + sebi_charges + stamp_duty + gst
    return round(total, 2)


def estimate_charges_for_trade(side: str, entry_price: float, exit_price: float, quantity: int) -> float:
    """
    Convenience wrapper — pass the trade as it's actually stored
    (side, entry, exit, qty) and this sorts out which leg was the
    buy and which was the sell.
      LONG  (side="BUY"):  buy leg = entry,  sell leg = exit
      SHORT (side="SELL"): sell leg = entry, buy leg  = exit
    """
    entry_value = entry_price * quantity
    exit_value  = exit_price * quantity

    if side == "BUY":
        buy_value, sell_value = entry_value, exit_value
    else:
        buy_value, sell_value = exit_value, entry_value

    return estimate_charges(buy_value, sell_value)