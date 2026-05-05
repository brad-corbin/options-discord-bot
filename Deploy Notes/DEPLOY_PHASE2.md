# Phase 2 Deploy — v8.5 Conviction Quality Gates + Backlog Fixes

Built on **v8.4.7**. Two-file change, ~55 LOC. Ships independently; Phase 3 layers on top later.

## What's in this deploy

| # | File | Change | Reason |
|---|---|---|---|
| 1 | oi_flow.py | Constants block | Tier-1/2 notional floors, min-OI map, exit cooldown |
| 2 | oi_flow.py | Sweep `burst` numeric | Unblocks real-time sweep pipeline (str-vs-int TypeError was silently killing every sweep) |
| 3 | oi_flow.py | `LATE_DAY_HEURISTIC` 3-7 DTE | 11/18 DTE don't expire soon enough for EOD de-risking; log-only |
| 4 | oi_flow.py | Gate A — min OI floor | Ratio noise guard before vol/OI is trusted |
| 5 | oi_flow.py | Gate B — notional floor | $10M tier-1 / $5M tier-2 premium. Biggest lever on false-positive rate |
| 6 | oi_flow.py | Gate C — immediate req sweep or sustained | 0-2 DTE single-scan spikes are where most false positives live |
| 7 | oi_flow.py | Gate D — reactive + immediate drops | 2%+ recent move → 0DTE flow is hedging, not directional |
| 8 | app.py | Bug #4 exit cooldown at 4 sites | 20-min dedup suppresses whipsaw exit spam |

## Deviation from handoff — flagged

Handoff said "all three" call sites for Bug #4. Real count in v8.4.7 is **four** (main `5950` / 0dte-chain `7708` / chain-iv `7963` / sweep `11832` after patch insertion drift). Site 3 (`7963`) is code-identical to Site 2; leaving it unpatched would defeat the cooldown on that pathway and reproduce the TSLA-4/13-style exit spam. User confirmed: patch all four. Site 3 uses 7690's template verbatim.

## Deploy order

Single commit, single push. All changes are within existing functions — no signature changes, no new imports except the inline `from oi_flow import CONVICTION_EXIT_COOLDOWN` (no-op if `oi_flow.py` hasn't been updated, so app.py can deploy first safely — but don't, deploy together).

1. Replace `oi_flow.py` and `app.py`.
2. Deploy to Render (not during market hours).
3. Watch startup logs for `oi_flow` import errors — if any, bail to the rollback.

## Pre-deploy verification (full-day grep on prior logs)

```bash
grep -c "SWEEP DETECTED"         render.log   # SweepDetector emits
grep -c "SWEEP: "                render.log   # handler receives
grep -c "Sweep handler error"    render.log   # TypeError crashes
```

Today's expectation: all three numbers roughly match (every sweep crashes). After deploy: third goes to 0.

If `SWEEP DETECTED` is 0, the streaming subscription isn't producing sweeps — that's a different problem, doesn't affect the other Phase 2 gates.

## Post-deploy verification (first full session)

```bash
grep -c "NOTIONAL FLOOR"         render.log   # Gate B drops
grep -c "IMMEDIATE DROP"         render.log   # Gate C drops
grep -c "REACTIVE DROP"          render.log   # Gate D drops
grep -c "EXIT CD"                render.log   # Bug #4 drops (all sites)
grep -c "EXIT CD (sweep)"        render.log   # Bug #4 drops, sweep-path only
grep -c "LATE_DAY_HEURISTIC"     render.log   # Bug #1 — only 3-7 DTE now
grep -cE "FLOW CONVICTION|CONVICTION PLAY|EXIT SIGNAL" render.log  # total fires
```

Expected on a typical day:
- Total conviction fires: was ~19–25, target ~5–10
- Exit signals: was 6/day, target 2–3/day
- `NOTIONAL FLOOR` drops: dozens (largest lever)
- `IMMEDIATE DROP`: handful
- `REACTIVE DROP`: 1–3 on volatile sessions
- `EXIT CD`: 1–2 per day (NVDA-style whipsaw)

**Red flags:**
- Zero conviction fires → Gate B too tight. Drop `CONVICTION_NOTIONAL_TIER1` to 7.5M.
- Fires unchanged and `NOTIONAL FLOOR` is 0 → Gate B didn't wire. Check `alert.get("mid", 0)` for zero-mid rows.
- `Sweep handler error` still nonzero → Patch 2 didn't apply cleanly.

## Rollback

All changes are inside existing functions. No state migrations.

1. `oi_flow.py`: revert to prior. Fastest.
2. `app.py`: revert to prior. Fastest.
3. Unset any env vars if you added any — none in this deploy, skip.

Alternative surgical revert (keeps constants + sweep fix, removes gates):
- Delete the 4 gate blocks (Gate A/B/C/D, all marked `# v8.5 (Phase 2, Gate X)`).
- Delete the 4 `_exit_within_cd` blocks in app.py (all marked `# v8.5 (Phase 2, Bug #4)`).
- Leave constants and the LATE_DAY DTE narrowing — both are low-risk and additive.

## Not included (bookkeeping)

- **Phase 1** (thesis_monitor alert_key cooldown) — separate PR. If Phase 3 ships, Phase 1 is deprecated.
- **Bug #2** (per-snapshot dedup) — holding until Phase 1/2/3 land to see if existing dedup layers already solve it.
- **Bug #3** (dual-header verification) — no code change; verify empirically.
