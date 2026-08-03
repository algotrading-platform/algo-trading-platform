#!/usr/bin/env python3
# ============================================================
# diagnose_ws_feed.py
#
# One-off: connect to Upstox WS V3 (full mode) for a couple of
# liquid instruments and print raw decoded messages as they
# arrive, so core/marketdata/ws_listener.py's field-name parsing
# (_extract_candle) can be checked/fixed against real data instead
# of guessed from docs.
#
# Usage:
#   python diagnose_ws_feed.py
#   (Ctrl+C to stop — it prints up to MAX_MESSAGES then exits on its own)
# ============================================================

import json
import sys
import time

from data.providers.upstox_provider import get_token, get_instrument_key, _load_instruments

MAX_MESSAGES = 10

count = 0


def on_message(message):
    global count
    count += 1
    print(f"\n--- message #{count} ({type(message).__name__}) ---")
    try:
        print(json.dumps(message, indent=2, default=str)[:3000])
    except Exception:
        print(repr(message)[:3000])
    if count >= MAX_MESSAGES:
        print(f"\nGot {MAX_MESSAGES} messages, exiting.")
        sys.exit(0)


def on_error(error):
    print(f"ERROR: {error}")


def on_open():
    print("WS opened")


def main():
    import upstox_client

    token = get_token()
    if not token:
        print("No valid Upstox token — run scripts/upstox_login.py first")
        sys.exit(1)

    _load_instruments()
    instrument_keys = [k for k in (get_instrument_key("RELIANCE.NS"), get_instrument_key("HDFCBANK.NS")) if k]
    instrument_keys.append("NSE_INDEX|Nifty 50")
    if len(instrument_keys) < 2:
        print("Could not resolve instrument keys — check the instruments cache")
        sys.exit(1)

    cfg = upstox_client.Configuration()
    cfg.access_token = token
    streamer = upstox_client.MarketDataStreamerV3(
        upstox_client.ApiClient(cfg),
        instrument_keys,
        "full",
    )
    streamer.on("message", on_message)
    streamer.on("error", on_error)
    streamer.on("open", on_open)

    print(f"Connecting, subscribing to {instrument_keys}, mode=full ...")
    streamer.connect()

    # if connect() returns instead of blocking, keep the process alive
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
