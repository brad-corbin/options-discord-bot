# Phase 3.1 REMEDIATION — finish what Phase 3.1 was supposed to do

## What happened

When I delivered Phase 3.2, I built it from the pristine pre-Phase-3.1 `app.py` source to make it "independently deployable," then labeled it "deploys cleanly on top of Phase 3 or Phase 3.1." That was wrong. Applying Phase 3.2's `app.py` on top of an already-Phase-3.1'd `app.py` reverted every Phase 3.1 change in that file. That's exactly the "don't ship partial files labeled as finals" trap from the project instructions. My miss.

What's currently on your Render deploy (audit-verified from the zip you uploaded):

| File | Phase 3 | Phase 3.1 | Phase 3.2 |
|---|---|---|---|
| `app.py` | ✅ yes | **❌ clobbered** | ✅ yes |
| `schwab_stream.py` | ✅ yes | ✅ yes (inert — no callback to consume it) | n/a |
| `active_scanner.py` | n/a | n/a | ✅ yes (Fix A) |

Symptom: flipping `CONTINUOUS_FLOW_SUB_GATE_ENABLED=true` does nothing, because the env var isn't read and `post_gate_fn` isn't passed to `start_continuous_flow(...)`. The flow conviction noise you wanted silenced is still firing in full.

## What this remediation ships

**Single file: `app.py`.** It is the uploaded Render `app.py` with the missing Phase 3.1 app.py changes merged back in. It keeps all Phase 3.2 work that's already in your file (Fix A reference, B, C, D).

Specifically added:

1. Phase 3.1 header block (24 lines) above the Phase 3.2 header.
2. `_continuous_post_gate(cp, msg)` helper defined inline above the `start_continuous_flow(...)` call — mirrors the 4 worker-side post sites exactly.
3. `CONTINUOUS_FLOW_SUB_GATE_ENABLED` env read, default `"false"`.
4. `post_gate_fn=` kwarg passed into `start_continuous_flow(...)` gated on the env var.
5. Startup log line confirming ENABLED/disabled state.
6. **Bonus fix** — one-char kwarg typo at `app.py:5992`. Was `get_intraday_bars(cp["ticker"], count=80)`, should be `countback=80`. Was silently `TypeError`-ing into a bare `except: pass` on every immediate-flow conviction, so VPOC never computed via this path. Fixed since I was touching the file.

Total diff vs your current `app.py`: **~147 lines added, 2 lines modified**.

## What this remediation does NOT touch

- `schwab_stream.py` — already has Phase 3.1 correctly applied and is fine. **Do not re-upload.**
- `active_scanner.py` — already has Phase 3.2 Fix A. **Do not re-upload.**
- Phase 3.2 Fix B/C/D in `app.py` — already present, preserved in the merged file.

If you only replace `app.py` from this deliverable and leave the other two alone, you'll be in the correct end state.

## Deploy sequence (Thursday evening)

1. Replace `app.py` on Render with `/mnt/user-data/outputs/phase31_remediate/app.py`.
2. **Leave `CONTINUOUS_FLOW_SUB_GATE_ENABLED` unset or `"false"` for the first deploy.**
3. Deploy.
4. Watch startup logs for:
   ```
   Phase 3.1: continuous flow sub-gate disabled (set CONTINUOUS_FLOW_SUB_GATE_ENABLED=true to enable)
   ```
   That line absent → the helper didn't wire (shouldn't happen, but this is the canary).
5. Confirm no behavior change over ~30 minutes of market hours (or the next morning).

## Enabling the gate (Friday shakedown)

1. On Render: set `CONTINUOUS_FLOW_SUB_GATE_ENABLED=true`.
2. Redeploy (env var change requires process restart).
3. Startup log should now show:
   ```
   Phase 3.1: continuous flow sub-gate ENABLED
   ```
4. During market hours, these new log markers should fire when un-subbed continuous convictions happen:
   ```
   💎 COMPACT PROMPT (continuous): <TICKER> call|put (<N>DTE, $<X.X>M)
   🔇 EXIT SIGNAL (un-subbed, continuous): <TICKER> — user not subscribed, suppressing
   🔇 EXIT CD (continuous): <TICKER> — exit already posted within 20min, suppressed
   🔄 AUTO-CLOSED conviction sub on exit (continuous): <TICKER> <dir>
   ```
5. The existing `💎 CONVICTION [...] (continuous):` log line still fires because it's downstream of the post path — its presence no longer implies a full card posted. Check the preceding log marker to see what actually went out.

## Post-deploy verification

```bash
# Gate is live and firing silences:
grep -c "💎 COMPACT PROMPT (continuous)"         render.log   # non-zero on un-subbed tickers
grep -c "🔇 EXIT SIGNAL (un-subbed, continuous)" render.log   # replaces prior exit-signal noise
grep -c "🔇 EXIT CD (continuous)"                 render.log   # cooldown hits
grep -c "🔄 AUTO-CLOSED conviction sub"           render.log   # rises as you /conviction + flip

# Noise reduction check:
grep -cE "💎 CONVICTION .* \(continuous\):"     render.log    # total detections — unchanged
# Full-post rate should DROP; compact-prompt rate should RISE.

# Gate exception health (should all be 0):
grep "Phase 3.1 gate outer exception"           render.log
grep "Compact prompt failed (continuous)"       render.log

# VPOC bonus fix verification:
grep "vpoc:" render.log | head -5   # before: zero hits; after: non-zero vpoc keys being written
```

## Red flags

- **`Phase 3.1: continuous flow sub-gate ENABLED` missing at startup** after setting the env var → Render env not applied, restart the service.
- **Flag enabled but zero compact prompts after a full session** → either every continuously-detected ticker already has a `/daytrade` or `/conviction` sub (check `/positions`), or the gate is failing open on every call (grep for `Phase 3.1 gate outer exception`).
- **Full cards still posting on un-subbed tickers** → grep for `Phase 3.1 gate outer exception` first; then confirm the `schwab_stream.py` already on Render has the Phase 3.1 `post_gate_fn` plumbing (it should — audit confirmed).

## Rollback

1. **Env-flip only** (recommended first-line rollback): set `CONTINUOUS_FLOW_SUB_GATE_ENABLED=false` or unset it, redeploy. The scanner reverts to always-full-post behavior. No code change.
2. **Full revert**: restore the prior `app.py`. Phase 3.2 work is preserved in the merged file so you'd need to re-apply it; easier to just keep this `app.py` and rely on the env flag.

## No new state, no schema change

- No new Redis keys beyond what Phase 3 + Phase 2 + Phase 3.2 already use.
- The `conviction_exit:{ticker}` cooldown key is shared with the 4 worker-side post sites by design — worker-path and continuous-path exits dedupe against each other, not themselves.
- No Sheet tab changes beyond what Phase 3.2 already did.

## Acknowledgment

Your project instructions say: "Don't ship partial files labeled as finals." I did exactly that when I delivered Phase 3.2 from a pristine base without merging Phase 3.1 forward. The deploy doc for Phase 3.2 saying "deploys cleanly on top of Phase 3 or Phase 3.1" was false. This remediation is the correction. I'll build any further `app.py` patches from the file currently on Render, not from pristine originals.
