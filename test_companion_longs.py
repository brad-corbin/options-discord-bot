"""
Verifies _record_companion_long_for_spread:
  A. Feature OFF (default) → no companion record created
  B. Feature ON, debit spread → companion long_call/put recorded with correct
     strike (long-leg strike for debit spreads, lean (b))
  C. Feature ON, credit spread (income) → companion uses short-leg strike,
     directionally-correct right (bull_put → long_call)
  D. Feature ON, no chain row for the strike → companion is skipped, no crash
  E. Duplicate spread record → no new companion (dedup at spread level
     should not produce orphan companions)
"""
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import recommendation_tracker as rt


class FakeStore:
    def __init__(self): self.kv = {}
    def get_fn(self, k): return self.kv.get(k)
    def set_fn(self, k, v): self.kv[k] = v
    def scan_fn(self, prefix): return [k for k in self.kv if k.startswith(prefix)]


def fresh_store():
    f = FakeStore()
    return rt.RecommendationStore(get_fn=f.get_fn, set_fn=f.set_fn,
                                  scan_fn=f.scan_fn)


def find_strike_mid(chain_rows, strike, right):
    """Mirror app.py:_find_strike_mid_in_chain."""
    target_strike = float(strike)
    target_right = (right or "").lower()
    for row in chain_rows:
        try:
            row_strike = float(row.get("strike") or 0)
            if abs(row_strike - target_strike) > 0.01:
                continue
            row_side = (row.get("side") or row.get("right") or "").lower()
            if row_side and target_right and row_side != target_right:
                continue
            bid = float(row.get("bid") or 0)
            ask = float(row.get("ask") or 0)
            last = float(row.get("last") or 0)
            if bid > 0 and ask > 0:
                return (bid + ask) / 2.0
            if last > 0:
                return last
            if ask > 0:
                return ask
            return 0.0
        except (TypeError, ValueError):
            continue
    return 0.0


def companion_for_spread(spread_result, ticker, direction, trade_type,
                         spread_legs, chain_rows, store, enabled, orig_source):
    """Mirror app.py:_record_companion_long_for_spread, parameterized for
    test (uses the test store and bypasses the global flag)."""
    if not enabled:
        return None
    if not spread_result or not spread_result.get("is_new_campaign"):
        return None
    spread_record = spread_result.get("record") or {}
    spread_cid = spread_record.get("campaign_id") or spread_result.get("campaign_id")
    if not spread_cid:
        return None
    long_leg = next((l for l in spread_legs if l.get("action") == "buy"), None)
    if not long_leg:
        return None
    right = (long_leg.get("right") or "").lower()
    strike = long_leg.get("strike")
    expiry = (long_leg.get("expiry") or "")[:10]
    if right not in ("call", "put") or strike is None or not expiry:
        return None
    entry_mid = find_strike_mid(chain_rows or [], strike, right)
    if entry_mid <= 0:
        return None
    extra = {"companion_of": spread_cid, "companion_strike_basis": "long_leg",
             "review_only": False, "confirmed_entry": False, "vehicle_compare": True}
    return rt.record_recommendation(
        store=store, source=f"companion_{orig_source}",
        ticker=ticker, direction=direction, trade_type=trade_type,
        structure=f"long_{right}", legs=[{"right": right, "strike": strike,
                                          "expiry": expiry, "action": "buy"}],
        entry_option_mark=entry_mid,
        entry_underlying=float(spread_record.get("entry_underlying") or 0),
        extra_metadata=extra, pricing_mode="long_mark",
    )


# ────────────────────────────────────────────────────────────────────
# Scenario A — feature OFF (default) means no companion is created
# ────────────────────────────────────────────────────────────────────
print("=" * 60)
print("Scenario A — feature OFF, no companion created")
print("=" * 60)
store = fresh_store()
spread_legs = [
    {"right": "call", "strike": 200, "expiry": "2026-05-15", "action": "buy"},
    {"right": "call", "strike": 205, "expiry": "2026-05-15", "action": "sell"},
]
spread_result = rt.record_recommendation(
    store=store, source="check_ticker", ticker="NVDA",
    direction="bull", trade_type="immediate",
    structure="bull_call_spread", legs=spread_legs,
    entry_option_mark=2.10, entry_underlying=199.5,
    pricing_mode="debit_spread_net",
)
chain_rows = [{"strike": 200, "side": "call", "bid": 4.40, "ask": 4.60}]
companion = companion_for_spread(spread_result, "NVDA", "bull", "immediate",
                                  spread_legs, chain_rows, store,
                                  enabled=False, orig_source="check_ticker")
assert companion is None, "FAIL: feature OFF should not create companion"
print("  ✓ Feature OFF → no companion record (as expected)")

# ────────────────────────────────────────────────────────────────────
# Scenario B — debit spread, feature ON, companion uses long-leg strike
# ────────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("Scenario B — debit spread, companion = long_call at long-leg strike")
print("=" * 60)
store = fresh_store()
spread_result = rt.record_recommendation(
    store=store, source="check_ticker", ticker="NVDA",
    direction="bull", trade_type="immediate",
    structure="bull_call_spread", legs=spread_legs,
    entry_option_mark=2.10, entry_underlying=199.5,
    pricing_mode="debit_spread_net",
)
companion = companion_for_spread(spread_result, "NVDA", "bull", "immediate",
                                  spread_legs, chain_rows, store,
                                  enabled=True, orig_source="check_ticker")
assert companion is not None, "FAIL: feature ON should create companion"
crec = companion["record"]
print(f"  Spread cid    = {spread_result['campaign_id']}")
print(f"  Companion cid = {companion['campaign_id']}")
print(f"  structure     = {crec['structure']}")
print(f"  legs          = {crec['legs']}")
print(f"  entry_mark    = ${crec['entry_option_mark']:.2f}")
print(f"  companion_of  = {crec['extra']['companion_of']}")
assert crec["structure"] == "long_call"
assert crec["legs"][0]["strike"] == 200
assert crec["entry_option_mark"] == 4.50  # (4.40 + 4.60) / 2
assert crec["extra"]["companion_of"] == spread_result["campaign_id"]
print("  ✓ Companion correctly references spread's long-leg strike")

# ────────────────────────────────────────────────────────────────────
# Scenario C — credit spread (bull_put), companion = long_call at short
#               leg strike, directionally aligned
# ────────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("Scenario C — credit bull_put → long_call at short-leg strike")
print("=" * 60)
store = fresh_store()
credit_legs = [
    {"right": "put", "strike": 400, "expiry": "2026-05-15", "action": "sell"},
    {"right": "put", "strike": 395, "expiry": "2026-05-15", "action": "buy"},
]
spread_result = rt.record_recommendation(
    store=store, source="income_scanner", ticker="SPY",
    direction="bull", trade_type="income",
    structure="bull_put_spread", legs=credit_legs,
    entry_option_mark=0.85, entry_underlying=405.0,
    pricing_mode="credit_spread_debit_to_close",
)
# For credits the app.py wiring constructs custom companion legs:
#   bull_put → long_call at short-leg strike
short_strike = 400
comp_legs = [{"right": "call", "strike": short_strike,
              "expiry": "2026-05-15", "action": "buy"}]
chain_call_400 = [{"strike": 400, "side": "call", "bid": 6.10, "ask": 6.30}]
companion = companion_for_spread(spread_result, "SPY", "bull", "income",
                                  comp_legs, chain_call_400, store,
                                  enabled=True, orig_source="income_scanner")
assert companion is not None, "FAIL: credit spread companion should record"
crec = companion["record"]
print(f"  Spread cid    = {spread_result['campaign_id']}")
print(f"  Companion cid = {companion['campaign_id']}")
print(f"  structure     = {crec['structure']}")
print(f"  legs          = {crec['legs']}")
print(f"  entry_mark    = ${crec['entry_option_mark']:.2f}")
assert crec["structure"] == "long_call"
assert crec["legs"][0]["strike"] == 400
assert crec["legs"][0]["right"] == "call"
assert abs(crec["entry_option_mark"] - 6.20) < 0.001
print("  ✓ Credit-spread companion is directionally-correct long_call at short-leg strike")

# ────────────────────────────────────────────────────────────────────
# Scenario D — chain row missing for the companion strike → skip cleanly
# ────────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("Scenario D — no chain row for companion strike → no crash, no record")
print("=" * 60)
store = fresh_store()
spread_result = rt.record_recommendation(
    store=store, source="check_ticker", ticker="NVDA",
    direction="bull", trade_type="immediate",
    structure="bull_call_spread", legs=spread_legs,
    entry_option_mark=2.10, entry_underlying=199.5,
    pricing_mode="debit_spread_net",
)
empty_chain = []
companion = companion_for_spread(spread_result, "NVDA", "bull", "immediate",
                                  spread_legs, empty_chain, store,
                                  enabled=True, orig_source="check_ticker")
assert companion is None, "FAIL: missing chain should skip (returns None)"
print("  ✓ No chain → no companion (no crash)")

# ────────────────────────────────────────────────────────────────────
# Scenario E — duplicate spread → no orphan companion
# ────────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("Scenario E — duplicate spread record → no companion (gated on is_new_campaign)")
print("=" * 60)
store = fresh_store()
# First record: new campaign
first = rt.record_recommendation(
    store=store, source="check_ticker", ticker="NVDA",
    direction="bull", trade_type="immediate",
    structure="bull_call_spread", legs=spread_legs,
    entry_option_mark=2.10, entry_underlying=199.5,
    pricing_mode="debit_spread_net",
)
companion_for_spread(first, "NVDA", "bull", "immediate",
                     spread_legs, chain_rows, store,
                     enabled=True, orig_source="check_ticker")
# Second record: duplicate of the same spread
second = rt.record_recommendation(
    store=store, source="check_ticker", ticker="NVDA",
    direction="bull", trade_type="immediate",
    structure="bull_call_spread", legs=spread_legs,
    entry_option_mark=2.10, entry_underlying=199.5,
    pricing_mode="debit_spread_net",
)
assert second["is_new_campaign"] is False, "Sanity check: should be a duplicate"
dup_companion = companion_for_spread(second, "NVDA", "bull", "immediate",
                                      spread_legs, chain_rows, store,
                                      enabled=True, orig_source="check_ticker")
assert dup_companion is None, "FAIL: duplicate spread should NOT spawn a new companion"
print("  ✓ Duplicate spread → no new companion record")

print("\n" + "=" * 60)
print("ALL COMPANION SCENARIOS PASSED")
print("=" * 60)
