"""
Run this script to diagnose Upstox API issues.
It tests different instrument key formats and intervals.
"""
import os, sys, requests
sys.path.insert(0, ".")

from core.database.db import get_upstox_token

token = get_upstox_token()
if not token:
    print("❌ No valid Upstox token. Run: python scripts/upstox_login.py")
    sys.exit(1)

print(f"✅ Token found: {token[:20]}...")

headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

# Test 1: Search for HDFCBANK
print("\n--- Test 1: Search for HDFCBANK ---")
r = requests.get(
    "https://api.upstox.com/v2/instruments/search",
    headers=headers,
    params={"q": "HDFCBANK", "asset_type": "EQUITY"},
    timeout=10,
)
print(f"Status: {r.status_code}")
if r.status_code == 200:
    data = r.json()
    instruments = data.get("data", [])[:3]
    for inst in instruments:
        print(f"  Key: {inst.get('instrument_key')} | Symbol: {inst.get('tradingsymbol')} | Exchange: {inst.get('exchange')}")
else:
    print(f"Error: {r.text[:200]}")

# Test 2: Try historical candle with different key formats
print("\n--- Test 2: Historical candle API tests ---")
test_keys = [
    "NSE_EQ|HDFCBANK",
    "NSE_EQ|TCS",
    "NSE_EQ|RELIANCE",
]
from datetime import datetime, timedelta
to_date   = datetime.now().strftime("%Y-%m-%d")
from_date = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")

for key in test_keys:
    encoded = requests.utils.quote(key, safe="")
    url = f"https://api.upstox.com/v2/historical-candle/{encoded}/day/{to_date}/{from_date}"
    r = requests.get(url, headers=headers, timeout=10)
    candles = r.json().get("data", {}).get("candles", []) if r.status_code == 200 else []
    print(f"  {key}: {r.status_code} | candles: {len(candles)}")
    if r.status_code != 200:
        print(f"    Error: {r.text[:150]}")

# Test 3: Try 1minute candle
print("\n--- Test 3: 1minute intraday ---")
key = "NSE_EQ|HDFCBANK"
encoded = requests.utils.quote(key, safe="")
url = f"https://api.upstox.com/v2/historical-candle/{encoded}/1minute/{to_date}/{from_date}"
r = requests.get(url, headers=headers, timeout=10)
candles = r.json().get("data", {}).get("candles", []) if r.status_code == 200 else []
print(f"  1minute: {r.status_code} | candles: {len(candles)}")
if r.status_code != 200:
    print(f"  Error: {r.text[:200]}")

print("\n--- Test 4: 30minute intraday ---")
url = f"https://api.upstox.com/v2/historical-candle/{encoded}/30minute/{to_date}/{from_date}"
r = requests.get(url, headers=headers, timeout=10)
candles = r.json().get("data", {}).get("candles", []) if r.status_code == 200 else []
print(f"  30minute: {r.status_code} | candles: {len(candles)}")
if r.status_code != 200:
    print(f"  Error: {r.text[:200]}")

print("\nDone.")