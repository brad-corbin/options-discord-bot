# Phase 3.3 REMEDIATION — restore Phase 3.1 remediation that Phase 3.3 clobbered

## What happened

Same class of error as the Phase 3.1 → 3.2 regression two sessions ago. I staged the Phase 3.3 `app.py` from the older options-discord-bot-main__2_.zip I had in the working directory, not the options-discord-bot-main__3_.zip you uploaded that had Phase 3.1 remediation applied. When you deployed Phase 3.3, it overwrote the 3.1rem work.

Audit of the current Render state (v4 zip you just uploaded):

| Item | Status |
|---|---|
| Phase 3.1 remediation header block in app.py | ❌ GONE |
| `_continuous_post_gate` helper | ❌ GONE |
| `CONTINUOUS_FLOW_SUB_GATE_ENABLED` env var read | ❌ GONE |
| Startup log "Phase 3.1: continuous flow sub-gate..." | ❌ GONE |
| `post_gate_fn=` kwarg at `start_continuous_flow(...)` | ❌ GONE |
| VPOC `count=80` → `countback=80` kwarg fix | ❌ GONE (back to `count=80`) |
| Phase 3.2 Fixes A/B/C/D | ✅ Intact |
| Phase 3.3 fixes (dashboard.py, oi_flow.py) | ✅ Intact |
| Phase 3.3 header in app.py | ✅ Intact |

Observed behavior: flipping `CONTINUOUS_FLOW_SUB_GATE_ENABLED=true` on Render does nothing again — same symptom as pre-remediation. ContinuousFlowScanner full-posts every conviction regardless of subscription. VPOC silently TypeErrors on every immediate-flow conviction.

## What this remediation ships

**Single file: `app.py`.** Built from your v3 zip (which had Phase 3.1 remediation + Phase 3.2 correctly applied), with only the Phase 3.3 header block added. No other changes.

Net result: all of Phase 3.1rem + Phase 3.2 + Phase 3.3 in one file.

Verification of the file I'm delivering:

```
Header order (top of file):        3.3 → 3.1rem → 3.2 → 3     ✅
VPOC fix:                          countback=80 at line 6050   ✅
_continuous_post_gate helper:      defined at line 12076       ✅
CONTINUOUS_FLOW_SUB_GATE_ENABLED:  read at line 12073          ✅
post_gate_fn= kwarg:               passed at line 12185        ✅
Startup log:                       present at lines 12189-12193 ✅
Phase 3.2 Fix B (countbacks):      intact                      ✅
Phase 3.2 Fix C (leading space):   intact                      ✅
Phase 3.2 Fix D (spot_at_callout): intact (5 occurrences)      ✅
Phase 3.3 header block:            at lines 4-40               ✅
All 3.2 markers (8 total):         intact                      ✅
```

File parses clean. Diff vs current Render v4: 147 lines restored (matches the ~110 lines of helper code + VPOC fix + header block shift).

## What this remediation does NOT touch

**Do not re-upload these files** — they're correct on Render right now:
- `schwab_stream.py` — Phase 3.1 plumbing intact
- `active_scanner.py` — Phase 3.2 Fix A intact
- `dashboard.py` — Phase 3.3 fixes intact
- `oi_flow.py` — Phase 3.3 fixes intact

Re-uploading invites another clobber cycle. Only replace `app.py`.

## Deploy sequence (Thursday evening or off-hours)

1. Replace `app.py` on Render with `/mnt/user-data/outputs/phase33_remediate/app.py`.

2. **Leave `CONTINUOUS_FLOW_SUB_GATE_ENABLED` unset or `"false"` for first deploy.**

3. Deploy.

4. Watch startup logs for both of these lines:
   ```
   Phase 3.1: continuous flow sub-gate disabled (set CONTINUOUS_FLOW_SUB_GATE_ENABLED=true to enable)
   Trade journal store initialized
   dashboard: loop entering — first write in 60s
   ```
   First line confirms 3.1rem is back. Other two confirm nothing else broke.

5. Wait out ~30 minutes of market hours (or overnight) — scanner should behave identically to the clobbered version because the env flag is off.

## Enabling the continuous gate (Friday shakedown)

1. Set `CONTINUOUS_FLOW_SUB_GATE_ENABLED=true` on Render.
2. Redeploy (env var change requires restart).
3. Startup log should now show:
   ```
   Phase 3.1: continuous flow sub-gate ENABLED
   ```
4. Expected market-hours log markers:
   ```
   💎 COMPACT PROMPT (continuous): <TICKER> call|put (<N>DTE, $<X.X>M)
   🔇 EXIT SIGNAL (un-subbed, continuous): <TICKER>
   🔇 EXIT CD (continuous): <TICKER>
   🔄 AUTO-CLOSED conviction sub on exit (continuous): <TICKER>
   ```

## Post-deploy verification

```bash
# VPOC fix is actually computing now (was silently failing pre-remediation):
grep "vpoc:" render.log | head -5    # should have hits within first 30 min of market

# 3.1rem gate healthy:
grep "Phase 3.1 gate outer exception"   render.log   # should be 0

# Nothing else regressed:
grep "[Phase3.3 diag] AS"               render.log   # 3.3 diag should still fire
```

## Rollback

1. **Env-flip only**: set `CONTINUOUS_FLOW_SUB_GATE_ENABLED=false` — reverts continuous gate behavior without touching code.
2. **Full revert to v4 state**: restore previous `app.py`. Phase 3.3 fixes in other files (dashboard, oi_flow) stay untouched. You'd be back in the current broken state, but at least stable.

## Process change (this is the third time)

Going forward I will grep-verify the staging source file contains all expected prior-phase markers BEFORE editing it, not after. Adding this as a pre-flight check for any future `app.py` work:

```
Pre-flight on staging source:
  grep -cE "Phase 3"  must match expected phases (3, 3.1rem, 3.2, etc.)
  grep "_continuous_post_gate"  must be present if 3.1rem deployed
  grep "countback=80"  must be present if 3.1rem deployed
  grep "spot_at_callout"  must be present if 3.2 deployed
  If any expected marker is missing from source, STOP — wrong file.
```

Apologies for the repeat. The Phase 3.3 delivery did what it was supposed to do in `dashboard.py` and `oi_flow.py`, but `app.py` source selection is where I keep getting caught. This check gets Phase 3.4 clean.

## Ordering

No dependencies. Can deploy immediately. Env var stays off until you're ready for the Friday shakedown.
