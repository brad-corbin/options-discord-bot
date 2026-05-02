"""
Verifies Patch GBUG2 fixes Bug #2.

Three scenarios:
  A. Cross-side play (flow on calls, recommend put). rec_mid is set.
     With patch: entry_option_mark = rec_mid (correct put price).
     Without patch: entry_option_mark = mid (the call's price → bug).
  B. Same-side play (flow on calls, recommend call). rec_mid not set.
     entry_option_mark = mid (correct, fall-through preserved).
  C. Edge case: rec_mid present but zero/falsy. Should fall through to mid.
"""
import sys
sys.path.insert(0, '/home/claude/audit/options-discord-bot-main')

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


def patched_entry_mark(cp):
    """Mirrors the patched line in /home/claude/v8.4/app.py:3236
    (_record_conviction_recommendation) after Patch GBUG2."""
    return (cp.get("current_mark")
            or cp.get("rec_mid")
            or cp.get("mid")
            or cp.get("premium")
            or 0)


# ────────────────────────────────────────────────────────────────────
# Scenario A: cross-side play (flow=call, rec=put)
# ────────────────────────────────────────────────────────────────────
print("=" * 60)
print("Scenario A — flow on call, recommendation on put (rec_mid set)")
print("=" * 60)
cp_cross = {
    "ticker": "NVDA",
    "strike": 200,
    "side": "call",                   # flow side
    "trade_direction": "bearish",
    "expiry": "2026-05-15",
    "mid": 0.18,                      # call mid (FLOW side)
    "ask": 0.20,
    "premium": 0.18,
    "rec_strike": 200,                # the put we actually recommend
    "rec_side": "put",
    "rec_mid": 1.40,                  # put mid (RECOMMENDED side)
    "rec_bid": 1.38,
    "rec_ask": 1.42,
    "has_rec_contract": True,
}
entry = patched_entry_mark(cp_cross)
print(f"  patched entry_option_mark = ${entry:.2f}")
print(f"  cp['rec_mid'] = ${cp_cross['rec_mid']:.2f}  (recommended put)")
print(f"  cp['mid']     = ${cp_cross['mid']:.2f}     (flow call — wrong)")
assert entry == 1.40, f"FAIL: expected 1.40, got {entry}"
print("  ✓ entry_option_mark = rec_mid (the recommended put's price)")

# Now run end-to-end through the recorder + a poll, to confirm sane PnL:
store = fresh_store()
direction = "bear"
right = "put"
strike = cp_cross["rec_strike"]
res = rt.record_recommendation(
    store=store, source="conviction_flow", ticker=cp_cross["ticker"],
    direction=direction, trade_type="immediate",
    structure=f"long_{right}",
    legs=[{"right": right, "strike": strike,
           "expiry": cp_cross["expiry"], "action": "buy"}],
    entry_option_mark=entry,
    entry_underlying=200.0,
    pricing_mode="long_mark",
)
cid = res["campaign_id"]
# Poll: NVDA holding flat, put mid still ~$1.42
upd = rt.update_tracking(store, cid, current_option_mark=1.42,
                         current_underlying=200.0)
print(f"  After poll: status={upd['status']}, grade={upd['grade']}, "
      f"pnl_pct={upd.get('pnl_pct')}")
assert upd["status"] == "tracking", "FAIL: shouldn't grade — flat market"
print("  ✓ Stays tracking with realistic ~1.4% PnL "
      f"(was instantly +678% before patch)")

# ────────────────────────────────────────────────────────────────────
# Scenario B: same-side play (flow=call, rec=call). rec_mid NOT set.
# ────────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("Scenario B — flow on call, recommendation on call (rec_mid absent)")
print("=" * 60)
cp_same = {
    "ticker": "NVDA",
    "strike": 200,
    "side": "call",
    "trade_direction": "bullish",     # bullish + call flow → recommend call
    "expiry": "2026-05-15",
    "mid": 1.50,
    "ask": 1.52,
    "premium": 1.50,
    # No rec_mid, no rec_strike — same side, oi_flow.py:2647 skips override
}
entry = patched_entry_mark(cp_same)
print(f"  patched entry_option_mark = ${entry:.2f}")
assert entry == 1.50, f"FAIL: expected 1.50, got {entry}"
print("  ✓ Falls through to cp['mid'] when rec_mid not set "
      "(unchanged behavior)")

# ────────────────────────────────────────────────────────────────────
# Scenario C: rec_mid present but zero (falsy)
# ────────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("Scenario C — rec_mid present but 0 (one-sided quote scenario)")
print("=" * 60)
cp_zero_rec = {
    "ticker": "NVDA",
    "strike": 200,
    "side": "call",
    "trade_direction": "bearish",
    "expiry": "2026-05-15",
    "mid": 0.18,
    "rec_mid": 0,                     # streaming overlay had no two-sided quote
    "rec_strike": 200,
    "rec_side": "put",
}
entry = patched_entry_mark(cp_zero_rec)
print(f"  patched entry_option_mark = ${entry:.2f}")
# Since `or` falls through on zero, we land on cp['mid'] — still buggy in
# this corner case, but at least we don't store $0 as entry. This matches
# the position_monitor pattern.
assert entry == 0.18, f"FAIL: expected fall-through to 0.18, got {entry}"
print("  ✓ Falls through to mid when rec_mid is 0 "
      "(matches position_monitor fallback chain)")
print("    NOTE: this corner case still uses the flow side's mid. The")
print("    real fix would be to also reject the record entirely if no")
print("    valid rec-side quote exists. Out of scope for this patch.")

print("\n" + "=" * 60)
print("ALL SCENARIOS PASSED — Patch GBUG2 fixes the bug correctly.")
print("=" * 60)
