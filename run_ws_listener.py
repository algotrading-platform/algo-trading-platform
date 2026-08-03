#!/usr/bin/env python3
# ============================================================
# run_ws_listener.py
#
# Entry point for the Upstox WebSocket market-data listener.
# Long-running — deploy as an always-on Azure Container App
# (NOT the scheduled Job that runs run_single_scan.py). Runs
# for as long as the container lives; the scan Job keeps
# running independently and just gets its data faster/wider
# via live_candles_1min once this is deployed.
#
# Usage:
#   python run_ws_listener.py
# ============================================================

import logging
import sys
import os

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from core.marketdata.ws_listener import WSListener

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("run_ws_listener")


def main():
    log.info("Starting Upstox WebSocket market-data listener...")
    WSListener().run_forever()


if __name__ == "__main__":
    main()
