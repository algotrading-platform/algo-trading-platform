"""
Test Upstox with correct instrument key from their instruments file.
"""
import os, sys, requests, gzip, json
from io import BytesIO
from datetime import datetime, timedelta
sys.path.insert(0, ".")

from core.database.db import get_upstox_token
token = get_upstox_token()
headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

print("Step 1: Downloading Upstox NSE instruments file...")
r = requests.get(
    "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz",
    timeout=30
)
print(f"Status: {r.status_code}")

if r.status_code == 200:
    with gzip.GzipFile(fileobj=BytesIO(r.content)) as f:
        instruments = json.load(f)

    print(f"Total instruments: {len(instruments)}")

    # Find HDFCBANK, TCS, RELIANCE
    targets = ["HDFCBANK", "TCS", "RELIANCE", "NIFTY 50", "NIFTY50"]
    found = []
    for inst in instruments:
        sym = inst.get("tradingsymbol", "") or inst.get("trading_symbol", "")
        seg = inst.get("segment", "") or inst.get("exchange_segment", "")
        key = inst.get("instrument_key", "") or inst.get("key", "")
        itype = inst.get("instrument_type", "") or inst.get("type", "")
        
        if sym.upper() in targets and "NSE" in str(seg).upper():
            found.append({"sym": sym, "key": key, "seg": seg, "type": itype})
            print(f"  Found: {sym} → key={key} | seg={seg} | type={itype}")

    if found:
        # Test historical candle with first found key
        test_key = found[0]["key"]
        print(f"\nStep 2: Testing historical candle with key: {test_key}")
        encoded = requests.utils.quote(test_key, safe="")
        to_date   = datetime.now().strftime("%Y-%m-%d")
        from_date = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")

        for interval in ["day", "30minute", "1minute"]:
            url = f"https://api.upstox.com/v2/historical-candle/{encoded}/{interval}/{to_date}/{from_date}"
            r2 = requests.get(url, headers=headers, timeout=10)
            candles = r2.json().get("data", {}).get("candles", []) if r2.status_code == 200 else []
            print(f"  {interval}: {r2.status_code} | candles: {len(candles)}")
            if r2.status_code != 200:
                print(f"    Error: {r2.text[:150]}")
    else:
        print("  No matches found. Printing first 3 instruments:")
        for inst in instruments[:3]:
            print(f"  {inst}")
else:
    print(f"Failed: {r.text[:200]}")