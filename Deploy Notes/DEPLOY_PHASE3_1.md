# Phase 3.1 Deploy — Close the ContinuousFlowScanner Sub-Gate Gap

Built on **Phase 3 + Phase 2 + v8.4.7**. Two-file change, ~190 LOC total diff. One new env var.

## What this fixes

Phase 3 wrapped 4 conviction post sites with the /daytrade + /conviction
subscription gate: `app.py:6029` (main), `7829` (0dte-chain), `8120` (chain-iv),
`11990` (sweep). The handoff said "4 conviction post sites" — but there's a
**5th one** the handoff didn't name:

```
schwab_stream.py ~line 1081  ContinuousFlowScanner._scan_expiration
                                 self._post(msg)
                                 self._post(msg, chat_id=self._intraday_chat_id)
```

Every `💎 CONVICTION [IMMEDIATE] (continuous):` log line — 18 of them today
— came through this un-gated path. Full cards posted to both main and
intraday regardless of subscription state. User expected Phase 3 silence;
saw Phase 3 noise instead.

## What's in this deploy

| File | Change |
|---|---|
| `schwab_stream.py` | New `post_gate_fn` kwarg on `ContinuousFlowScanner.__init__` and `start_continuous_flow(...)`. `_scan_expiration` calls it before `self._post(msg)`. Callback contract: returns `"full"` / `"compact_posted"` / `"silent"`. Default `None` → scanner behaves exactly as pre-3.1. |
| `app.py` | New inline helper `_continuous_post_gate(cp, msg)` defined just above the `start_continuous_flow(...)` call. Mirrors the exact branch logic at the 4 existing sites. Passed into `start_continuous_flow(...)` only when `CONTINUOUS_FLOW_SUB_GATE_ENABLED=true`. |
| Header block in `app.py` | Adds the v8.5 Phase 3.1 entry above the Phase 3 entry. |

Diff sizes: `app.py` +133 lines, `schwab_stream.py` +58 lines. Both files pass `ast.parse`.

## Env gate

```
CONTINUOUS_FLOW_SUB_GATE_ENABLED=false   # default (per project convention)
CONTINUOUS_FLOW_SUB_GATE_ENABLED=true    # enable the gate
```

With `false` (or unset), `start_continuous_flow(...)` is called with
`post_gate_fn=None` and the scanner posts full cards exactly like today.
No behavior change.

With `true`, the gate runs on every continuous-scanner conviction and
applies the same rules as the 4 existing Phase 3 sites.

## Behavior matrix (once enabled)

| Event | No sub | `/conviction` sub | `/daytrade` sub |
|---|---|---|---|
| Continuous new conviction | Compact prompt (intraday only) | Full card (main + intraday) | Full card (main + intraday) |
| Continuous re-fire | Compact prompt | Full card | Full card |
| Continuous EXIT SIGNAL | Silent | Posts + **auto-closes** sub | Posts (sub stays open) |
| Exit CD hit (within 20 min) | Silent | Silent | Silent |

Exit cooldown uses the **same Redis key** as the 4 existing post sites
(`conviction_exit:{ticker}`), so a worker-side exit and a continuous-side
exit dedupe against each other — not against themselves.

## Deviation from handoff — flagged

1. **Env flag defaults to off, not on.** Project convention: every new
   feature needs an on/off env var defaulting to off. This matches that.
   Consequence: you'll need to flip it to `true` explicitly to get the
   silence you were expecting. One-line rollback in either direction.

2. **Side effects are skipped when the gate returns `compact_posted`.** The
   scanner's post-loop also runs `confirm_conviction_posted`,
   `save_conviction_boost`, the income-scan trigger on `route == "income"`,
   and `log_conviction`. Those all attach to a *posted full card* — they
   shouldn't fire on a compact prompt. Implementation skips them via
   `continue` after the gate returns `compact_posted`, except the stats
   counter which still increments so `continuous_flow.convictions` remains
   an honest count of "we detected a real play."

3. **`"silent"` skips literally everything including stats.** An exit
   cooldown hit or an un-subbed exit signal is not a conviction we're
   choosing to route differently — it's a non-event from the user's
   perspective. Stats don't count it.

4. **Gate exceptions fail open to `"full"`.** A bug in the gate callback
   cannot silence real signals — worst case is one noisy card that
   should've been a compact prompt. This matches the fail-open posture
   of the Phase 3 sub-check blocks at the other 4 sites.

## Deploy order

Not during market hours. Thursday evening preferred.

1. Replace `schwab_stream.py` and `app.py` on the Render repo.
2. Leave `CONTINUOUS_FLOW_SUB_GATE_ENABLED` unset (or explicitly `false`).
3. Deploy.
4. Watch startup logs for:
   ```
   Phase 3.1: continuous flow sub-gate disabled (set CONTINUOUS_FLOW_SUB_GATE_ENABLED=true to enable)
   ```
   Confirms patch loaded and the scanner is unchanged from pre-3.1.
5. If anything about that deploy looks off, revert both files. No state to undo.

## Friday shakedown (enabling the gate)

1. On Render, set `CONTINUOUS_FLOW_SUB_GATE_ENABLED=true`.
2. Redeploy (or trigger a restart — env change needs a process restart).
3. Startup logs should now show:
   ```
   Phase 3.1: continuous flow sub-gate ENABLED
   ```
4. During market hours, these new markers should appear in the logs:
   ```
   💎 COMPACT PROMPT (continuous): <TICKER> call|put (<N>DTE, $<X.X>M)
   🔇 EXIT SIGNAL (un-subbed, continuous): <TICKER> — user not subscribed, suppressing
   🔇 EXIT CD (continuous): <TICKER> — exit already posted within 20min, suppressed
   🔄 AUTO-CLOSED conviction sub on exit (continuous): <TICKER> <dir>
   ```
5. The existing `💎 CONVICTION [...] (continuous):` log line still fires
   — that's logged AFTER any post decision and is just "we detected a
   conviction play." Presence of that line doesn't mean a full card went
   out anymore; check the preceding log markers to see what actually posted.

## Post-deploy verification greps

```bash
# Gate is live and firing silences:
grep -c "💎 COMPACT PROMPT (continuous)"         render.log   # non-zero on un-subbed tickers
grep -c "🔇 EXIT SIGNAL (un-subbed, continuous)" render.log   # exit noise replacement
grep -c "🔇 EXIT CD (continuous)"                 render.log   # cooldown hits
grep -c "🔄 AUTO-CLOSED conviction sub"           render.log   # should rise as you /conviction + flip

# Noise reduction check (same metric as Phase 2):
grep -cE "💎 CONVICTION .* \(continuous\):"     render.log    # total detections — unchanged
# vs the worker-post rate: how many actually became full cards vs compact?
# Before 3.1: conviction count ≈ full-card count
# After 3.1:  conviction count >> full-card count for un-subbed tickers

# Gate exception health:
grep "Phase 3.1 gate"                           render.log    # should be 0. Non-zero = investigate.
grep "Phase 3.1 gate outer exception"           render.log    # should be 0.
grep "Compact prompt failed (continuous)"       render.log    # should be 0. Non-zero = compact builder issue.
```

## Red flags

- **`Phase 3.1: continuous flow sub-gate ENABLED` missing at startup** after
  setting the env var → Render env not applied, restart the service.
- **Flag enabled but zero compact prompts after a full session** → either
  you've got /daytrade subs on everything that's firing (check `/positions`),
  or the gate is failing open on every call (grep for
  `Phase 3.1 gate outer exception`).
- **`AUTO-CLOSED conviction sub` firing on tickers you didn't sub to** —
  shouldn't happen, but would indicate stale keys in Redis under
  `subscriptions:*`. `redis-cli --pattern 'subscriptions:*'` to inspect;
  `DEL` any ghost keys.
- **Full cards still posting on un-subbed tickers after flag enabled** →
  grep for `Phase 3.1 gate outer exception` first (fail-open path).

## Rollback

Two routes, both low-effort:

1. **Just flip the env var**: set `CONTINUOUS_FLOW_SUB_GATE_ENABLED=false`
   (or unset it), redeploy. Scanner gets `post_gate_fn=None` and behavior
   reverts to pre-3.1 full-posting. Zero code change.

2. **Full revert**: restore both files to their pre-3.1 versions. No state
   migrations. Redis subscription keys from Phase 3 are unaffected.

## Ordering

- **Phase 3 must be live first.** Phase 3.1 depends on `subscriptions.py`,
  `_build_compact_conviction_prompt`, and the exit cooldown key convention
  that Phase 3 established.
- **Phase 2 must be live first** (it is). `conviction_exit:{ticker}` key
  and `CONVICTION_EXIT_COOLDOWN` are Phase 2 deliverables.
- **No schema or state migrations.** Can deploy and revert freely.
