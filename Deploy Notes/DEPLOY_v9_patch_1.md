# DEPLOY — v9 (Patch 1 + Patch 1.1) — app_bias.py hardening

**Scope:** single file replacement — `app_bias.py` only. No `app.py` touch, no thesis_monitor.py touch, no env vars added, no Redis schema change.

**Built on:** Walk 1 audit + Reviewer cross-check + pre-deploy review of Patch 1.

Patch 1 contained six hardening fixes. Pre-deploy review caught three blockers + one cleanup, all about `as_float(x, 0.0)` falling back to 0.0 on non-numeric strings and that 0 then triggering bullish/bearish branches. Patch 1.1 closes those gaps. The two patches ship together.

---

## What changes (per signal)

### Patch 1 — six hardening fixes
| # | Fix | Behavior before | Behavior after |
|---|---|---|---|
| 1 | Score clamp | Could return ±15 in extremes | Clamped to ±14; raw value returned in new `raw_score` key |
| 2 | S1 at-flip neutral | spot==flip → score -2 with text "0.0% BELOW" | \|dist\|<0.05% → score 0, neutral text |
| 3 | S6 zero-OI handling | `cw_oi==0` silently treated as ratio=1.0 (balanced) | Explicit ±1 score for one-sided OI; n/a when both zero |
| 4 | S7 zero-midpoint guard | `mid==0` → ZeroDivisionError | n/a placeholder, no crash |
| 5 | S13/S14 TERM placeholder | Silently dropped when `vix9d` missing or `term` not in {normal,inverted,flat} | Always emits a TERM signal (n/a or scored) |
| 6 | S14 term-string normalize | Only matched `"normal"`/`"inverted"`/`"flat"` | Also matches `CONTANGO` → normal, `BACKWARDATION`/`SEVERE_BACKWARDATION` → inverted, `FLAT` → flat |
| 7 | `as_float` coercion | String inputs from Redis could TypeError mid-calc | All numeric reads pass through `as_float(x, 0.0)` |

### Patch 1.1 — close the post-coerce gaps
| # | Fix | Behavior before (P1) | Behavior after (P1.1) |
|---|---|---|---|
| 8 | S13 VIX guard | `vix={"vix": "abc"}` → coerced to 0 → scored `[VIX +2] VIX 0.0` | `v <= 0` after coercion → `[VIX n/a]` + `[TERM n/a]` |
| 9 | S10 Skew guard | `call_iv="abc"` → coerced to 0 → `diff = put_iv` → false `[SKEW -1]` | `call_iv <= 0 or put_iv <= 0` → `[SKEW n/a]` |
| 10 | S5 regime guard | `regime="MODERATE TREND"` (string) → AttributeError on `.get()` | `isinstance(regime, dict)` check, fallback to `{}` |
| 11 | S1 GEX fallback (no flip) | `tgex == 0` → scored `[GEX -1] -$0.0M` | `tgex == 0` → emits `[GEX 0]`, no score |

**Not in this patch:**
- `gex_sign` override (waiting on Walk 1B audit)
- DEX/VANNA/CHARM threshold recalibration (calibration sprint)
- `em` parameter removal (deferred)
- VIX-as-overlay restructure (model design question)

---

## Verification — what you should see post-deploy

**Friday 8:30 AM CT silent EM run** should produce identical scores to today's run for every ticker that wasn't hitting an edge case. The only tickers whose scores will change are:

- Any ticker where `cw_oi==0` or `pw_oi==0` was hiding behind ratio=1.0 (rare; illiquid expiries only)
- Any ticker that happened to print spot exactly at flip_price (very rare)
- Any ticker where the upstream sent `term` as `CONTANGO`/`BACKWARDATION` instead of lowercase (live trace shows this isn't currently happening — defensive only)
- Any ticker where VIX, skew IVs, or regime were arriving as malformed values (would have produced bad signals before Patch 1.1; now produces n/a)

**AAPL today reproduced exactly:** the live AAPL inputs that produced `bias_score=7` against the unpatched function still produce `score=7, direction=STRONG BULLISH` against v9 (Patch 1 + 1.1). Verified by acceptance test.

**New diagnostic field:** the return dict now carries `raw_score` (pre-clamp). Dashboard consumers don't need to read it, but if you want to know whether a clamp happened, `raw_score != score`.

## Smoke check after deploy

```bash
# Re-pull AAPL thesis after the next silent EM run; bias_score should still match the hand-trace.
redis-cli -u "$REDIS_URL" --raw GET 'thesis_monitor:AAPL' | python3 -m json.tool | grep bias_score
```

Then for one ticker, grep your logs for the new TERM-n/a placeholder text so you can confirm the patch is actually live:
```bash
grep "TERM n/a\|term classification\|VIX data unavailable or invalid\|SKEW n/a" logs/*.log | tail -5
```

---

## Deploy steps

1. Replace `app_bias.py` in your Render repo with the v9 file.
2. Push to main. Render auto-redeploys.
3. Confirm no exception in startup logs.
4. Wait until 8:30 AM CT next trading day for the silent EM run.
5. Run the smoke check above.

## Rollback

```bash
git revert <commit-sha-of-v9-patch-1>
git push origin main
```

That's it. No env vars to flip, no Redis keys to clean up, no schema migration. The `raw_score` field returned in the dict is additive — any consumer that doesn't know about it just ignores the key.

---

## Acceptance tests run

**43/43 pass** against the patched file:

- 25 P1 regression tests still pass (AAPL trace, score clamp, at-flip, TERM behavior, zero-OI, zero-midpoint, string-input survival)
- 18 new P1.1 tests pass:
  - VIX = "abc"/0.0/-5.0 → n/a + TERM n/a, contributes 0 to score
  - Skew with bad/zero IVs → n/a, contributes 0
  - Regime as string → no crash, S5 still emits
  - Regime as None → handled
  - GEX fallback (no flip) with tgex == 0 → neutral, no score change
  - GEX fallback with tgex = +1.5 / -1.5 still scores correctly (no regression)
  - Combined worst-case (VIX bad, skew bad, regime string, no flip) → only valid signals contribute, no crash

