# ============================================================
# core/execution/charges.py
#
# Estimates real-world brokerage + statutory charges for a round-trip
# intraday equity trade, so paper trading can show Gross P&L (what
# the old "pnl" meant) alongside a realistic Net P&L (after charges).
#
# CORRECTED against the actual Upstox brokerage calculator (screenshots,
# not a verbal quote) — qty=10000, buy@100, sell@103, intraday:
#   Brokerage ₹40.00 | STT/CTT ₹257.50 | Transaction Charges ₹70.04 |
#   Clearing ₹0 | DP ₹0 | Stamp Duty ₹30.00 | SEBI ₹2.03 | GST ₹20.17
#   Total taxes & charges (excl. brokerage) ₹379.74 | Grand total ₹419.74
# Every constant below reproduces this exactly.
#
# This SUPERSEDES an earlier fix (Jul 14) that moved BROKERAGE_PER_ORDER
# from 20 to 40 based on Jwala's verbal arithmetic on a call ("15 buys
# and 15 sells... 30 into 40 = 1200"). That verbal math assumed ₹40 was
# the PER-ORDER rate — but the real calculator shows ₹40.00 is the
# TOTAL for one round trip (1 buy + 1 sell = 2 orders), i.e. ₹20/order.
# That also matches Upstox's own published rate card ("Equity Intraday:
# ₹20 or 0.1%, whichever lower"). The Jul 14 fix was a real regression,
# caught by this more authoritative source rather than assumed correct
# — reverted here, along with two other constants that were off:
#   - EXCHANGE_TXN_PCT was estimated at 0.00297%; the real rate implied
#     by this example is 0.00345%.
#   - GST was computed on (brokerage + exchange charges) only; the real
#     breakdown shows it's on (brokerage + exchange charges + SEBI fees).
#
# STILL an ESTIMATE, not a live Upstox API call — rates can change,
# and this is fit to one real example (large enough turnover that
# rounding is unlikely to hide a wrong rate, but not a guarantee across
# every trade size). Re-verify against Upstox's calculator periodically.
# ============================================================

BROKERAGE_PER_ORDER = 20.0        # Upstox: ₹20 or 0.1% (whichever lower), equity intraday
STT_PCT             = 0.00025     # sell leg only, intraday equity
EXCHANGE_TXN_PCT     = 0.0000345  # both legs (NSE) — empirically matched, see header
SEBI_PCT             = 0.000001   # both legs
STAMP_DUTY_PCT       = 0.00003    # buy leg only
GST_PCT              = 0.18       # on (brokerage + exchange charges + SEBI charges)


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
    gst              = GST_PCT * (brokerage + exchange_charges + sebi_charges)

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