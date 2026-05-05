# WALK 1D — `iv_skew:{ticker}` writer/reader audit

**Verdict:** This is **not** a clean Patch 2a parallel. Do **not** apply the
disable-writer pattern. Schema divergence is real but latent (no live
override pulling bad data over good). Recommendation is a threshold
recalibration, not a deletion. Final disposition is operator's call —
four options outlined at the end.

## Sites

Anchors greppable. Line numbers will drift; values below are at the
file's current state (3,928 lines).

| Role | File:line | Code |
|---|---|---|
| Producer | `oi_flow.py:430` | `def compute_iv_skew(chain_data, spot)` |
| Writer | `oi_flow.py:996–998` | `_json_set(f"iv_skew:{ticker}", skew, ttl=7200)` |
| Reader | `oi_flow.py:2621–2623` | `iv_skew = self._state._json_get(f"iv_skew:{ticker}") or {}` |
| Embed | `oi_flow.py:2681` | `play["iv_skew"] = iv_skew` |
| Consumer | `oi_flow.py:2969–2975` | `format_conviction_play` — emits "STEEP PUT SKEW" line |

The writer is the only producer of the `iv_skew:{ticker}` key. The
reader is the only consumer. There is no other code in the repo that
touches that Redis key.

## Why this differs from the gex case

Patch 2a shut down the lightweight gex writer because it was producing
values that were **selected over** the institutional-engine values via an
explicit "GEX live takes precedence" rule in `_build_levels`. The
visible bug was put-wall above call-wall — structurally impossible — and
came directly from that override.

For `iv_skew:{ticker}`:

- **No override rule.** Nothing reads `iv_skew:{ticker}` except the
  conviction-play formatter. There is no `_build_levels` equivalent
  picking between sources.
- **No structurally-broken visible bug.** No analogue of put-wall-above-
  call-wall has been observed. `compute_iv_skew()`'s arithmetic is
  internally coherent (decimal IV in, decimal IV out, formatter
  multiplies by 100 for display).
- **No alternate per-ticker writer to fall through to.** Disabling
  the writer breaks the conviction "STEEP PUT SKEW" line outright with
  no replacement source.

## What is real: schema divergence between two parallel skew systems

There **are** two skew systems in the codebase. They never cross today,
but they compute the same underlying metric with incompatible
thresholds.

### System A — `oi_flow` rail

```
compute_iv_skew(chain_data, spot)
  ↓ writes iv_skew:{ticker}
  ↓ read by detect_conviction_plays
  ↓ embedded in play["iv_skew"]
  ↓ consumed by format_conviction_play → "STEEP PUT SKEW" alert line
```

Output fields: `atm_iv`, `put_25d_iv`, `call_25d_iv`, `skew_ratio`,
`skew_direction` ("put_heavy" / "call_heavy" / "neutral"),
`skew_extreme` (bool, true when `skew_ratio > 1.5`).

### System B — `options_skew` rail

```
options_skew.compute_skew(contracts, spot, ticker)
  ↓ called only from shadow_signals.py
  ↓ persisted to omega:shadow_signals:{spread_id} (per-trade key)
  ↓ used by score_skew_for_trade for shadow-delta scoring
```

Output fields: `skew_pp` (put_iv − call_iv, percentage points),
`skew_ratio`, `skew_label` ("EXTREME_STEEP" / "STEEP" / "NORMAL" /
"FLAT" / "VERY_FLAT" / "INVERTED"), `skew_emoji`, `daily_roc_pp`,
`roc_label`, `put_strike`, `call_strike`, `put_iv`, `call_iv`,
`put_delta`, `call_delta`, `description`. Plus history via
`SkewHistory`.

### Where they disagree

The two systems flag "extreme" using **different math**:

- System A: `skew_extreme = (skew_ratio > 1.5)` — geometric
- System B: `EXTREME_STEEP = (skew_pp > 6.0)` — arithmetic, in IV pp

These agree at typical IV levels but diverge at low absolute IV.
Worked examples (put IV / call IV → ratio, pp):

| put_iv | call_iv | ratio | pp | A says | B says |
|--------|---------|-------|----|----|----|
| 0.30 | 0.20 | 1.50 | 10.0 | extreme | EXTREME_STEEP |
| 0.18 | 0.12 | 1.50 | 6.0  | extreme | EXTREME_STEEP (boundary) |
| 0.15 | 0.10 | 1.50 | 5.0  | **extreme** | STEEP |
| 0.12 | 0.08 | 1.50 | 4.0  | **extreme** | STEEP |
| 0.09 | 0.06 | 1.50 | 3.0  | **extreme** | NORMAL |

In a low-VIX regime where absolute IV is compressed, System A
flashes "STEEP PUT SKEW" in conviction alerts at thresholds System B
would call routine. False-positive risk on the conviction alert text.

## What is also real, but lower-priority

`compute_iv_skew()` is a strict subset of `compute_skew()`'s
capabilities. Missing from System A: daily ROC tracking, history
recording, trade-direction scoring, percentage-point output. The
conviction formatter cannot say "skew is rapidly expanding" because
that signal isn't computed there.

`compute_iv_skew()` averages across delta buckets `[0.40, 0.60]` for
ATM and `[0.20, 0.30]` for 25d. `compute_skew()` picks the single
contract with delta closest to 0.25 (with a tolerance band). Different
precision properties — averaging is more noise-robust, single-pick is
more sensitive to chain liquidity. Neither is "wrong"; they're just
different methodologies for the same metric.

## What is **not** a bug

- Decimal/percent handling — chain `iv` field is decimal, formatter
  does `*100` for display. Consistent.
- Absolute-delta logic — both calls and puts use `abs(delta)`, correct.
- Writer guard `skew.get("atm_iv", 0) > 0` — handles empty-dict
  return cleanly.
- 7200s TTL — stale data ages out within 2hr. Fine.
- Try/except wrapping — failure is silent and non-blocking. Per
  project rules ("Wrap new background work in try/except at every
  level"). Fine.

## Disposition options

**Option A — Recalibrate thresholds.** Change `compute_iv_skew()` so
`skew_extreme` uses `(put_25d_iv − call_25d_iv) * 100 > 6.0` matching
`options_skew.SKEW_EXTREME_STEEP_PCT`, and `skew_direction` thresholds
mirror `SKEW_STEEP_PCT` / `SKEW_FLAT_PCT`. Smallest change, single file,
single function. Conviction-formatter feature stays working. Brings
both systems into agreement on "extreme."
  Estimated risk: low. Estimated work: ~30 lines changed in one
  function plus tests. Env gate per project rule for behavior changes.

**Option B — Migrate conviction reader to options_skew.** Replace the
`iv_skew:{ticker}` Redis path with a direct call to
`options_skew.compute_skew()` inside `detect_conviction_plays`. Pulls
in `daily_roc_pp` and `skew_label`, formatter gets richer alert lines.
Heavier change — requires plumbing chain contracts to the call site or
adding a per-ticker write in shadow_signals.
  Estimated risk: medium (touches more files). Estimated work: a chat.

**Option C — Document and defer.** No code change. Add a known-issue
entry: "conviction `STEEP PUT SKEW` line uses a geometric threshold
that flags low-VIX skew as extreme more aggressively than the rest of
the system." Trade off: schema duplication remains; same-bug surface
in future patches.
  Estimated risk: zero. Estimated work: zero.

**Option D — Delete the System A rail entirely.** Comment out writer
(Patch 2a style) AND remove the reader at line 2621–2625, the
embedding at 2681, and the formatter block at 2969–2975. Conviction
alerts lose the "STEEP PUT SKEW" line. Cleanest deduplication. No
fallback feature unless paired with Option B work later.
  Estimated risk: low (single file, well-scoped). Estimated work:
  about half of Option A. **Loses a feature.**

## Recommendation

**Option A.** Lowest blast radius, fixes the actual divergence bug
(false-positive extreme at low VIX), keeps the alert feature, brings
the codebase into single-source-of-truth on threshold semantics
without ripping out the simpler System A rail. Option B is a worthy
v8.3+ target if Brad wants the richer skew text in conviction alerts.

If Brad disagrees with the recommendation and wants C or D instead,
that is also defensible — the bug is latent, not screaming.

## What this audit does **not** touch

- `engine_bridge._compute_skew()` at line 369 — separate internal
  helper for the v3 engine, different consumer path. Not in scope.
- `compute_skew()` calculation correctness — verified its delta
  filtering is correct, but did not exhaustively check all edge cases.
- Whether `chain_data["iv"]` is always decimal in production —
  verified consistency within `oi_flow`; did not cross-check against
  the MarketData adapter's contract.

## Out of scope per project rules

- Position-tracker abstraction (v8.3+)
- Architecture shift to long-only

## Next chat — if Brad picks A

1. Verify `oi_flow.py` is the version we audited (top-of-file comment
   block matches; AST clean).
2. Locate `compute_iv_skew()` by anchor (function definition, not
   line number).
3. Add `# v9 (Patch 6):` marker. Recalibrate `skew_extreme` and
   `skew_direction` thresholds to use percentage-point math matching
   `options_skew` constants.
4. Env gate per project rule: `STRICT_SKEW_PP=1` or similar (default
   off) toggles between legacy ratio thresholds and pp thresholds.
   *Or* — make case for skipping the gate per the post-Patch-5
   precedent: cosmetic change, reliably-populated input, rollback via
   commit revert. Document the decision either way.
5. Add unit tests covering the worked-example table above.
6. AST-parse, run tests, deploy doc, ship.

## Next chat — if Brad picks D

1. Same anchors. Add `# v9 (Patch 6):` markers at writer and reader.
2. Comment out lines 994–1000 with a header documenting the
   duplicate-truth finding.
3. Comment out lines 2620–2625 with a matching header.
4. Comment out lines 2968–2975 with a matching header.
5. AST-parse, run tests, deploy doc, ship.

## Next chat — if Brad picks C

No code change. Add a one-line known-issue entry in project
instructions. Move on to Walk 1C (`regime_detector` audit).
