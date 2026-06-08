import requests, gzip, json
from io import BytesIO

r = requests.get(
    "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz",
    timeout=30
)
with gzip.GzipFile(fileobj=BytesIO(r.content)) as f:
    data = json.load(f)

print("All NSE_INDEX instruments:")
for inst in data:
    seg = inst.get("segment", "")
    sym = inst.get("tradingsymbol", "")
    key = inst.get("instrument_key", "")
    if seg == "NSE_INDEX":
        print(f"  {sym!r:40s} -> {key}")