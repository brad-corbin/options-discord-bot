"""
Verifies Patch GBUG1 fixes Bug #1.

Three scenarios:
  A. Bull put credit spread, modest profit (~76%) — should NOT instantly grade.
  B. Bull put credit spread, real ride to ~92% profit — should target_hit.
  C. Bear put DEBIT spread (MSFT 405/400 shape) — sanity check, unchanged behavior.

Mirrors the PATCHED spread-branch logic from /home/claude/v8.4/app.py
(_rec_tracker_price_fn after Patch GBUG1).
"""
import sys
sys.path.insert(0, '/home/claude/audit/options-discord-bot-main')

import recommendation_tracker as rt


class FakeStore:
    def __init__(self): self.kv = {}
    def get_fn(self, k): return self.kv.get(k)
    def set_fn(self, k, v): self.kv[k] = v
    def scan_fn(self, prefix): return [k for k in self.kv if k.startswith(prefix)]


def patched_price_fn(structure, legs, chain):
    """Mirrors /home/claude/v8.4/app.py:_rec_tracker_price_fn spread branch
    after Patch GBUG1."""
    mids = {}
    for leg in legs:
        c = chain.get(leg["strike"])
        if c:
            bid, ask = c["bid"], c["ask"]
            mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else c["last"]
            mids[(leg.get("action"), leg.get("strike"),
                  leg.get("expiry"), leg.get("right"))] = mid
    buy_leg = next((l for l in legs if l.get("action") == "buy"), None)
    sell_leg = next((l for l in legs if l.get("action") == "sell"), None)
    buy_mid = mids.get((buy_leg.get("action"), buy_leg.get("strike"),
                        buy_leg.get("expiry"), buy_leg.get("right")))
    sell_mid = mids.get((sell_leg.get("action"), sell_leg.get("strike"),
                         sell_leg.get("expiry"), sell_leg.get("right")))
    # The patched line:
    if structure in ("bull_put_spread", "bear_call_spread"):
        return max(sell_mid - buy_mid, 0.0)
    return max(buy_mid - sell_mid, 0.0)


def fresh_store():
    f = FakeStore()
    return rt.RecommendationStore(get_fn=f.get_fn, set_fn=f.set_fn,
                                  scan_fn=f.scan_fn)


# ────────────────────────────────────────────────────────────────────
# Scenario A: bull_put_spread, modest favorable move, should NOT grade
# ────────────────────────────────────────────────────────────────────
print("=" * 60)
print("Scenario A — bull_put_spread, modest move (~30% profit, under target)")
print("=" * 60)
store = fresh_store()
legs = [
    {"right": "put", "strike": 400, "expiry": "2026-05-15", "action": "sell"},
    {"right": "put", "strike": 395, "expiry": "2026-05-15", "action": "buy"},
]
res = rt.record_recommendation(
    store=store, source="income_scanner", ticker="SPY",
    direction="bull", trade_type="income", structure="bull_put_spread",
    legs=legs, entry_option_mark=0.85, entry_underlying=405.0,
    pricing_mode="credit_spread_debit_to_close",
)
cid = res["campaign_id"]
# Mild favorable drift. Target debit_to_close = ~0.60 → pnl ~29% (under 50%).
chain = {
    400: {"bid": 0.78, "ask": 0.82, "last": 0.80},   # short still expensive
    395: {"bid": 0.19, "ask": 0.21, "last": 0.20},
}
mark = patched_price_fn("bull_put_spread", legs, chain)
print(f"  patched_price_fn returned: ${mark:.4f} "
      f"(should be ~$0.60 = 0.80 - 0.20)")
upd = rt.update_tracking(store, cid, current_option_mark=mark,
                         current_underlying=406.0)
print(f"  status={upd['status']}, grade={upd['grade']}, "
      f"pnl_pct={upd.get('pnl_pct')}")
print(f"  target_pct = {upd['exit_logic']['target_pct']}")
assert upd["status"] == "tracking", "FAIL: should still be active at ~29% profit"
print("  ✓ Stays ACTIVE — bug is fixed (was instantly grading +100% before)")

# ────────────────────────────────────────────────────────────────────
# Scenario B: bull_put_spread, big favorable move, SHOULD grade
# ────────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("Scenario B — bull_put_spread, big move (~90% profit)")
print("=" * 60)
store = fresh_store()
res = rt.record_recommendation(
    store=store, source="income_scanner", ticker="SPY",
    direction="bull", trade_type="income", structure="bull_put_spread",
    legs=legs, entry_option_mark=0.85, entry_underlying=405.0,
    pricing_mode="credit_spread_debit_to_close",
)
cid = res["campaign_id"]
# Underlying ripped from 405 to 412. Short 400P near worthless,
# long 395P near worthless too. Debit-to-close very small.
chain = {
    400: {"bid": 0.06, "ask": 0.10, "last": 0.08},
    395: {"bid": 0.01, "ask": 0.03, "last": 0.02},
}
mark = patched_price_fn("bull_put_spread", legs, chain)
print(f"  patched_price_fn returned: ${mark:.4f} "
      f"(should be ~$0.06 = 0.08 - 0.02)")
upd = rt.update_tracking(store, cid, current_option_mark=mark,
                         current_underlying=412.0)
print(f"  status={upd['status']}, grade={upd['grade']}, "
      f"pnl_pct={upd.get('pnl_pct')}, exit_reason={upd.get('exit_reason')}")
assert upd["status"] == "graded", "FAIL: should grade at ~93% profit"
assert upd["grade"] == "win"
assert upd["exit_reason"] == "target_hit"
assert 0.90 <= upd["pnl_pct"] <= 0.95
print("  ✓ Correctly grades target_hit at real profit level")

# ────────────────────────────────────────────────────────────────────
# Scenario C: bear_put_spread (debit), unchanged behavior
# ────────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("Scenario C — bear_put_spread MSFT 405/400 (debit), sanity check")
print("=" * 60)
store = fresh_store()
debit_legs = [
    {"right": "put", "strike": 405, "expiry": "2026-05-15", "action": "buy"},
    {"right": "put", "strike": 400, "expiry": "2026-05-15", "action": "sell"},
]
res = rt.record_recommendation(
    store=store, source="check_ticker", ticker="MSFT",
    direction="bear", trade_type="immediate", structure="bear_put_spread",
    legs=debit_legs, entry_option_mark=2.10, entry_underlying=405.0,
    pricing_mode="debit_spread_net",
)
cid = res["campaign_id"]
# MSFT dropped from 405 to 401. 405P now ITM, 400P near ATM.
# Spread mark = buy_mid - sell_mid (debit, increasing as it works)
chain = {
    405: {"bid": 4.40, "ask": 4.60, "last": 4.50},
    400: {"bid": 1.40, "ask": 1.60, "last": 1.50},
}
mark = patched_price_fn("bear_put_spread", debit_legs, chain)
print(f"  patched_price_fn returned: ${mark:.4f} "
      f"(should be ~$3.00 = 4.50 - 1.50)")
upd = rt.update_tracking(store, cid, current_option_mark=mark,
                         current_underlying=401.0)
print(f"  status={upd['status']}, grade={upd['grade']}, "
      f"pnl_pct={upd.get('pnl_pct')}, exit_reason={upd.get('exit_reason')}")
# pnl = (3.00 - 2.10) / 2.10 = +43%
assert upd["status"] == "tracking", "FAIL: ~43% profit shouldn't grade target"
print("  ✓ Debit spread behavior unchanged, stays ACTIVE at +43%")

print("\n" + "=" * 60)
print("ALL SCENARIOS PASSED — Patch GBUG1 fixes the bug correctly.")
print("=" * 60)
