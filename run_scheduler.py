#!/usr/bin/env python3
# ============================================================
# run_scheduler.py
#
# Entry point for the background signal scheduler.
#
# Usage:
#   python run_scheduler.py
#
# This runs independently of the dashboard.
# Keep this running during market hours (or 24/7).
# The dashboard only reads CSV files — it never needs to
# be open for signals to be generated and Telegram to fire.
#
# To run in background on Windows:
#   start /B python run_scheduler.py > logs/scheduler.log 2>&1
#
# To run in background on Linux/Mac:
#   nohup python run_scheduler.py > logs/scheduler.log 2>&1 &
# ============================================================

import os
import sys

# Ensure project root is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Create logs directory
os.makedirs("logs", exist_ok=True)
os.makedirs("data", exist_ok=True)

from core.scheduler.signal_scheduler import start

if __name__ == "__main__":
    start()
