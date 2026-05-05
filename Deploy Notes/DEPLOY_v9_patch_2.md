# DEPLOY — v9 (Patch 2 rev1) — disable lightweight GEX writer + thesis fallback + web dashboard freshness

**Scope:** three file replacements, three surgical edits, no new env vars, no Redis schema migration.

| File | Change | Marker |
|---|---|---|
| `oi_flow.py` | Comment out the lightweight GEX writer block (lines ~966-973) | `# v9 (Patch 2a):` |
| `persistent_state.py` | `get_gex_data` now reads thesis fallback chain (rev1: tries both thesis keys, float-coerces all numerics) | `# v9 (Patch 2b rev1):` |
| `omega_dashboard/data.py` | `_read_em_log` honors `:manual` force-runs; `_read_gex` routes through `get_gex_data` fallback chain | `# v9 (Patch 2c):` |

**Built on:** Walk 1 audit + Walk 1B pre-deploy review + Walk 1B Bug 5/6 discovered during deploy prep. Direct fix for the 2026-05-04 AAPL screen showing $260 gamma flip + put-wall-above-call-wall, plus the discovered "force-runs invisible to web card levels panel" bug.

**rev1 history:** Original Patch 2b called `self.get_thesis(ticker)` which reads from key `thesis:{ticker}`. Production verification (`redis EXISTS`) confirmed that key is empty — the live writer is `thesis_monitor._persist_thesis()` which writes to `thesis_monitor:{ticker}`. Original Patch 2b would have been a silent no-op (returning `{}` for every ticker after deploy). rev1 corrects this by reading both possible thesis keys and float-coercing every numeric before comparison.

**Patch 2c addition:** during deploy prep on 2026-05-05, discovered that `omega_dashboard/data.py:_read_em_log` reads only `:silent` keys (morning silent thesis) and `_read_gex` reads `gex:{ticker}` directly with no fallback chain. Both bypass thesis-derived data. Force-runs were invisible to the web dashboard's levels panel. Patch 2c adds `:manual` fallback to em_log lookup and routes gex lookup through `get_gex_data` (Patch 2b's fallback chain). See Walk 1B Bugs 5 and 6.

---

## What this fixes

The web dashboard (`omega_dashboard/data.py:_build_levels`) was deliberately preferring "GEX live" over "EM log" for `gamma_flip`, `call_wall`, `put_wall`, and `max_pain`. The intent was reasonable ("fresh beats stale"), but the lightweight writer at `oi_flow.py:971` was producing values that don't match the institutional engine:

| Field | Lightweight writer (estimate_gex_from_chain) | Institutional engine (thesis) |
|---|---|---|
| `gamma_flip` | OI-difference crossover proxy | Greeks-weighted gamma exposure |
| `call_wall` | Strike with highest call OI (raw) | Gamma-weighted wall |
| `put_wall` | Strike with highest put OI (raw) | Gamma-weighted wall |
| `max_pain` | Strike with highest combined OI (**wrong by definition**) | Min ITM-dollar-value strike |
| `gex_sign` | Above-flip → "positive" (regime) | Conflicting conventions across writers |

After this patch, the lightweight writer stops producing data, the existing 2-hour TTL ages out the bad keys, and `_build_levels` falls through to em_log automatically. **No edits to `omega_dashboard/data.py` are needed for the immediate symptom to resolve.**

---

## What changes per file

### Patch 2a — `oi_flow.py:966-973`

The block:
```python
# Compute and store lightweight GEX for this ticker
# Enables GEX convergence for conviction plays on ALL tickers
try:
    gex = estimate_gex_from_chain(chain_data, spot)
    if gex and gex.get("gamma_flip", 0) > 0:
        self._state._json_set(f"gex:{ticker}", gex, ttl=7200)
except Exception:
    pass
```
…is replaced with a comment block explaining why it was disabled. The function `estimate_gex_from_chain` itself stays in the module (still importable, still callable inline if anything ever needs it). Only the Redis write is gone.

### Patch 2b rev1 — `persistent_state.py:get_gex_data`

The original 7-line direct-read of `gex:{ticker}` is replaced with a three-source fallback chain. Critical correction in rev1: the **second** source path was added after pre-deploy review confirmed that the original Source 1 (via `get_thesis()`) reads from `thesis:{ticker}`, which is empty in production. The actually-live key is `thesis_monitor:{ticker}`, written by `thesis_monitor._persist_thesis()`.

Sources, in priority order:
1. **Source 1a** — `thesis:{ticker}` via `self.get_thesis()`. Forward-compatibility path. Currently dead in production (no caller of `save_thesis()` was found in the audited files), but harmless and serves as a hook if the PersistentState API ever becomes the canonical thesis store.
2. **Source 1b** — `thesis_monitor:{ticker}` read directly via `self._json_get()`. **This is the live key in current production.**
3. **Source 2** — `gex:{ticker}`, the lightweight blob deprecated by Patch 2a. Still readable during the 2-hour TTL transition window after deploy.

All numeric reads (`gamma_flip`, `call_wall`, `put_wall`, `max_pain`) flow through a local `_pos_float(v)` helper that coerces to float ≥ 0 with a `try/except (TypeError, ValueError)` guard, so stringy-number values from JSON edge cases don't TypeError-then-silently-fall-through.

---

## Behavior preservation — what stays the same

- **Tickers with a fresh thesis (all 35 FLOW_TICKERS each morning at 8:30 AM CT):** read the same data they always read. New `get_gex_data` returns thesis-derived values, which is what `omega_dashboard/_build_levels` was trying to get to all along.
- **Tickers with no thesis but a lightweight gex blob (transition window only):** still serve the lightweight blob. After ~2 hours post-deploy, all such blobs expire and this code path returns `{}`.
- **`oi_flow.py:2604`** — only inline reader of `gex:{ticker}` other than `_persistent_state` and `omega_dashboard`. After deploy, this lookup will return `None` once the TTL expires. The two consumers of that read produce gex_context strings for conviction-play narration; missing data → empty narration, no crash.
- **Conviction-play gating** — reads `_persistent_state.get_gex_sign(ticker)`, which has its own thesis fallback at `persistent_state.py:650`. Unaffected.
- **Function `estimate_gex_from_chain`** — still defined, still imported, still callable. Just no longer writes to Redis from `oi_flow.py:971`.

---

## Verification post-deploy

After Friday Patch 1+1.1 has shaken out, deploy Patch 2 in the next window. Then:

```bash
# 0. IMMEDIATELY after deploy — manually expire all gex:* keys to force
#    consumers off the stale lightweight blobs. Without this step, the web
#    dashboard / card UI keeps showing the bad values for up to 2 hours
#    until the existing TTL expires naturally. The bot dashboard and
#    Telegram action guide are not affected (they go through the
#    fallback chain in persistent_state.py); only the web card surface
#    benefits from this manual flush.
#
# ⚠️  IMPORTANT — DO NOT RUN THIS COMMAND BEFORE PATCH 2 IS DEPLOYED.
#    The flush requires Patch 2a (writer disabled) and Patch 2b (fallback
#    chain) to be live. Running this before deploy will:
#      - empty the web card's levels panel ("No level data captured")
#        because _read_gex() in omega_dashboard/data.py:1658 reads gex:{ticker}
#        directly with no fallback.
#      - work fine for a few minutes only — until the next chain pull repopulates
#        gex:{ticker} via the still-active oi_flow.py:971 writer.
#    The flush is a POST-DEPLOY step, not a pre-deploy step.
redis-cli -u "$REDIS_URL" --scan --pattern 'gex:*' | xargs -r redis-cli -u "$REDIS_URL" DEL

# Confirm they're gone:
redis-cli -u "$REDIS_URL" --scan --pattern 'gex:*' | head -5
# Should produce no output.

# 1. Confirm no fresh writes to gex:{ticker} are occurring
redis-cli -u "$REDIS_URL" --scan --pattern 'gex:*' | head -5
# Wait 5 minutes. Run again. Should still produce no output (the writer
# at oi_flow.py:971 is disabled, so nothing should be repopulating these keys).

# 2. Confirm AAPL card now shows institutional values
# Pull a fresh card via Telegram (/scan AAPL or whatever your trigger is).
# Expected after gex:AAPL has expired:
#   gamma flip: $273.14 (was $260)
#   call wall:  $285.00 (was $282.50)
#   put wall:   $275.00 (was $285.00 — fixes the impossible layout)
#   max pain:   $277.50 (was $285.00)

# 3. Confirm get_gex_data() returns thesis-sourced values
redis-cli -u "$REDIS_URL" --raw GET 'thesis_monitor:AAPL' | python3 -m json.tool | grep -E 'gamma_flip|call_wall|put_wall|max_pain' | head -10
# These four values should now match what the dashboard card displays.
```

---

## Rollback

```bash
git revert <commit-sha-of-v9-patch-2>
git push origin main
```

That's it. No env vars to flip. No Redis cleanup needed (the deprecated keys age out either way; if rolled back, they'll be repopulated on the next chain pull).

---

## Acceptance tests run

**38/38 pass** against the patched `persistent_state.py:get_gex_data` (Patch 2b rev1):

**Original Patch 2b regression suite (17 checks):**
- T1 — Source 1a only: returns thesis-derived dict
- T2 — Both sources: 1a takes precedence
- T3 — Source 1a missing → falls through to lightweight gex blob
- T4 — Thesis with `gamma_flip == 0` → falls through (matches `get_gamma_flip_level` pattern)
- T5 — Neither source: returns `{}` cleanly
- T6 — Thesis fields missing/None: coerced to 0.0 (no NoneType crashes)
- T7 — Thesis exists but no `levels` dict: falls through
- T8 — `get_thesis` raises: caught and falls through

**rev1 — Source 1b (thesis_monitor:{ticker}) — live production shape (10 checks):**
- T9 — **Critical:** AAPL with only `thesis_monitor:{ticker}` populated (the actually-live shape) returns institutional values
- T10 — Source 1b takes precedence over lightweight gex blob
- T11 — When both 1a and 1b are populated, 1a takes priority
- T12 — Source 1b with `gamma_flip == 0` → falls through correctly

**rev1 — float-coerce-before-compare safety (11 checks):**
- T13 — `gamma_flip` as numeric string `"273.14"` doesn't TypeError on compare
- T14 — `gamma_flip` as unparseable string `"n/a"` falls through
- T15 — `gamma_flip` as `None` falls through
- T16 — `gamma_flip` as empty string falls through
- T17 — Mixed-quality fields (valid gamma_flip + garbage call_wall): valid fields preserved, garbage coerced to 0
- T18 — Negative `gamma_flip` (impossible but defended) falls through

**17/17 pass** against `omega_dashboard/data.py` Patch 2c:

**`_read_em_log` :manual fallback (7 checks):**
- T1 — `:silent` only present → returns silent
- T2 — `:manual` only present → returns manual (**THE BUG WE'RE FIXING**)
- T3 — Neither key present → returns None
- T4 — Both present, manual newer by `logged_at_utc` → returns manual
- T5 — Both present, silent newer → returns silent
- T6 — Both present without timestamps → falls through to silent (legacy data safety)
- T7 — `_json_get` raises → returns None

**`_read_gex` routed through `get_gex_data` (10 checks):**
- T8 — `get_gex_data` returns full dict → passed through
- T9 — Returns empty dict → returns None (so `_build_levels` falls through to em_log)
- T10 — Returns None → returns None
- T11 — Raises → caught, returns None
- T12 — Live AAPL post-flush: thesis has correct values, lightweight blob deleted → all 5 fields land correctly with regime-convention `gex_sign`

Patch 2a (oi_flow.py) is verified by AST clean parse and textual confirmation that the live `_json_set` line is gone. The only behavioral surface for that patch is "stops writing the Redis key," which is testable post-deploy via the TTL countdown above.

---

## What this patch does NOT do

- **Does not fix the `gex_sign` overload.** That's three writer conventions across five paths (literal sign at `app.py:11568`/`:14040`, regime override at `thesis_monitor.py:4373`, default-to-positive at `:477`/`:952`). After Patch 2a, the regime-override path at `thesis_monitor.py:4373` is the only writer that touches `thesis_monitor:{ticker}.gex_sign`, which is what `get_gex_data` now returns. So the symptom is partially mitigated, but the structural overload remains. A future Walk 1B-driven patch should split `gex_sign` into `gex_value_sign` + `dealer_regime`.
- **Does not touch `omega_dashboard/_build_levels`.** The "GEX live takes precedence" priority logic is now dead code (Source 2 produces nothing), but it's harmless dead code. Cleanup can happen in a future patch after one week of clean data.
- **Does not touch `iv_skew:{ticker}`.** Same writer/reader pattern, but smaller blast radius (writer + single reader both inside `oi_flow.py`). Defer to v9 Patch 3 once we've confirmed Patch 2 didn't regress anything.
- **Does not touch the `_calc_bias` / `app_bias.py` engine.** Patch 1+1.1 already shipped that hardening separately.
