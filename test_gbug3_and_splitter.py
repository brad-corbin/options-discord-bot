"""
Verifies:
  - GBUG3: dashboard._pnl_pct_current branches on pricing_mode
  - Display splitter: _split_records_for_display correctly buckets
    confirmed / review_only / companion records
  - _review_only_display_mode env var precedence
"""
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import recommendation_tracker as rt

# Force a fresh import of the patched dashboard.py
import importlib
import importlib.util
spec = importlib.util.spec_from_file_location(
    "dashboard_patched", str(ROOT / "dashboard.py"))
dashboard_patched = importlib.util.module_from_spec(spec)
# Skip the boot — we just need the helpers
import sys as _s
_s.modules["dashboard_patched"] = dashboard_patched
try:
    spec.loader.exec_module(dashboard_patched)
except Exception as e:
    # Module-level boot may fail on missing deps; we only need the helpers
    pass


# ────────────────────────────────────────────────────────────────────
# GBUG3: _pnl_pct_current should use credit math for credit spreads
# ────────────────────────────────────────────────────────────────────
print("=" * 60)
print("GBUG3 — _pnl_pct_current credit-spread sign")
print("=" * 60)

from dashboard_patched import _pnl_pct_current

# Credit spread: $0.85 credit → debit-to-close $0.30 = +65% profit
credit_record = {
    "entry_option_mark": 0.85,
    "exit_option_mark": 0.30,
    "status": "graded",
    "pricing_mode": "credit_spread_debit_to_close",
}
pnl = _pnl_pct_current(credit_record)
print(f"  credit spread, entry $0.85, exit $0.30")
print(f"  pnl_pct = {pnl:+.4f}")
assert abs(pnl - 0.6471) < 0.001, f"FAIL: expected ~+0.65, got {pnl}"
print("  ✓ Credit spread renders as +64.7% (was -64.7% before patch)")

# Long call (debit instrument): unchanged
long_call_record = {
    "entry_option_mark": 1.50,
    "exit_option_mark": 2.25,
    "status": "graded",
    "pricing_mode": "long_mark",
}
pnl = _pnl_pct_current(long_call_record)
print(f"\n  long call, entry $1.50, exit $2.25 → pnl_pct = {pnl:+.4f}")
assert abs(pnl - 0.50) < 0.001
print("  ✓ Long call unchanged (still +50%)")

# Debit spread: unchanged
debit_spread_record = {
    "entry_option_mark": 2.10,
    "exit_option_mark": 3.00,
    "status": "graded",
    "pricing_mode": "debit_spread_net",
}
pnl = _pnl_pct_current(debit_spread_record)
print(f"\n  debit spread, entry $2.10, exit $3.00 → pnl_pct = {pnl:+.4f}")
assert abs(pnl - 0.4286) < 0.001
print("  ✓ Debit spread unchanged (still +42.9%)")


# ────────────────────────────────────────────────────────────────────
# Display splitter: 3-way bucketing
# ────────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("Display splitter — 3-way bucketing")
print("=" * 60)

records = [
    # confirmed (no extra, default source)
    {"campaign_id": "c1", "first_source": "check_ticker",
     "extra_metadata": {}},
    # review-only by explicit flag
    {"campaign_id": "r1", "first_source": "v84_credit_dual_post",
     "extra_metadata": {"review_only": True}},
    # review-only by source-name fallback (no explicit flag)
    {"campaign_id": "r2", "first_source": "v84_long_call_burst",
     "extra_metadata": {}},
    # companion long
    {"campaign_id": "k1", "first_source": "companion_check_ticker",
     "extra_metadata": {"companion_of": "c1"}},
    # confirmed_entry override (review_only=True but confirmed=True → confirmed)
    {"campaign_id": "c2", "first_source": "v84_credit_dual_post",
     "extra_metadata": {"review_only": True, "confirmed_entry": True}},
]

confirmed, review_only, companion = rt._split_records_for_display(records)
print(f"  confirmed:    {[r['campaign_id'] for r in confirmed]}")
print(f"  review_only:  {[r['campaign_id'] for r in review_only]}")
print(f"  companion:    {[r['campaign_id'] for r in companion]}")

assert {r["campaign_id"] for r in confirmed} == {"c1", "c2"}, \
    "FAIL: confirmed bucket wrong"
assert {r["campaign_id"] for r in review_only} == {"r1", "r2"}, \
    "FAIL: review_only bucket wrong"
assert {r["campaign_id"] for r in companion} == {"k1"}, \
    "FAIL: companion bucket wrong"
print("  ✓ All five records bucketed correctly")


# ────────────────────────────────────────────────────────────────────
# Display mode env var precedence
# ────────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("Display mode env var resolution")
print("=" * 60)

# Default
for k in ("RECTRACKER_FILTER_REVIEW_ONLY_FROM_REPORTS",
          "RECTRACKER_REVIEW_ONLY_DISPLAY"):
    os.environ.pop(k, None)
mode = rt._review_only_display_mode()
print(f"  no env vars → mode = {mode!r}")
assert mode == "inline"

# Legacy var → 'hide' for back-compat
os.environ["RECTRACKER_FILTER_REVIEW_ONLY_FROM_REPORTS"] = "1"
mode = rt._review_only_display_mode()
print(f"  legacy var=1 → mode = {mode!r}")
assert mode == "hide"

# New var overrides
os.environ["RECTRACKER_REVIEW_ONLY_DISPLAY"] = "split"
mode = rt._review_only_display_mode()
print(f"  legacy=1 + new=split → mode = {mode!r}  "
      f"(legacy wins because it's stricter)")
# Per implementation, legacy hide takes priority. Let's verify.
assert mode == "hide"

# Clear legacy, new=split
os.environ.pop("RECTRACKER_FILTER_REVIEW_ONLY_FROM_REPORTS", None)
mode = rt._review_only_display_mode()
print(f"  new=split alone → mode = {mode!r}")
assert mode == "split"

# Bogus value → fallback to inline
os.environ["RECTRACKER_REVIEW_ONLY_DISPLAY"] = "garbage"
mode = rt._review_only_display_mode()
print(f"  new=garbage → mode = {mode!r}")
assert mode == "inline"

# Cleanup
for k in ("RECTRACKER_FILTER_REVIEW_ONLY_FROM_REPORTS",
          "RECTRACKER_REVIEW_ONLY_DISPLAY"):
    os.environ.pop(k, None)
print("  ✓ Env var resolution works")


print("\n" + "=" * 60)
print("ALL GBUG3 + SPLITTER SCENARIOS PASSED")
print("=" * 60)
