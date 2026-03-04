import json
from options_engine import recommend_from_marketdata

# 1) Paste your MarketData JSON into a file called md.json (same folder)
with open("md.json", "r") as f:
    md = json.load(f)

# 2) Set the current SPY spot price manually for the test
SPOT = 680.33  # <-- replace with live SPY price you’re using

# 3) Your response has dte as an array; it’s the same for all entries, so grab [0]
DTE = int(md["dte"][0])

for direction in ["bull", "bear"]:
    result = recommend_from_marketdata(md, direction=direction, dte=DTE, spot=SPOT)
    print("\n", "="*60)
    print(direction.upper(), result)
