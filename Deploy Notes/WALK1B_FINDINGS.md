# Walk 1B — gex_sign field audit

**Audit conducted:** 2026-05-04 → 2026-05-05
**Scope:** every site that reads or writes `gex_sign` across the codebase
**Triggered by:** AAPL dashboard card showing $260 gamma flip + put-wall-above-call-wall, traced to multi-writer disagreement
**Audit method:** grep + scope verification (lessons from Patch 2b rev1 — never assume what a getter reads, never assume what a function-scoped variable shares)

**Patch 3 status (added 2026-05-05):** built, AST-clean, 27/27 tests passed, approved for deploy. Scope: `thesis_monitor.py` only. Risk: low. Behavior change: none intended. Main validation: `dealer_regime` mirrors the existing `gex_sign` override on every production ticker. See §6.9 for as-built notes and the one design-vs-implementation deviation (denominator choice).

## Executive summary

The framing "`gex_sign` is overloaded by three writer conventions" is **partially right and partially misleading**. The truly persistent `thesis.gex_sign` field on `ThesisContext` is internally coherent: it is written by exactly one canonical code path (the wrapper override at `thesis_monitor.py:4373-4378`) and read by ~30 sites, all of which expect regime convention. That entire surface is consistent.

What looked like "multi-writer drift" in the initial grep is mostly **local variables named `gex_sign` in different functions across different files**. Different scope, different purpose, different value. They don't share data, so there's no drift between them. They're a refactor trap (rename one and the others still resolve), but they're not producing wrong trade decisions today.

The **real** structural problems are smaller and more targeted than the multi-writer framing suggested:

| # | Problem | Severity | Locations |
|---|---|---|---|
| 1 | Cross-store inconsistency: `em_predictions` sheet writes literal-sign under the same field name where `thesis_monitor:{ticker}` stores regime | High (journaling/QC) | `app.py:11568`, `app.py:14040` |
| 2 | Three "default to positive" silent fallbacks that violate the project's "never silently default" policy | Medium | `thesis_monitor.py:477`, `:952`, `options_engine_v3.py:2586`, `:2641` |
| 3 | Variable-name reuse traps (different functions, same name, different conventions) — no bug today, refactor risk | Low | `app.py:12030` vs `:12741`, `options_engine_v3.py:1340` vs `:1432` |
| 4 | Override log message at `thesis_monitor.py:4377` says "below flip" when condition is "above flip" | Cosmetic | `thesis_monitor.py:4377` |
| 5 | Web dashboard `_read_em_log` ignores `:manual` force-runs — only reads `:silent`, force-runs invisible to levels panel | High (visibility) | `omega_dashboard/data.py:1648` |
| 6 | Web dashboard `_read_gex` reads `gex:{ticker}` directly, bypasses `get_gex_data` fallback chain | Medium (visibility) | `omega_dashboard/data.py:1658` |

**Recommended Patch 3** is far smaller than originally scoped. Roughly **12 line changes total**, no field rename, no consumer migration. Details in §6. **Status as of 2026-05-05:** built, AST-clean, 27/27 tests passed, approved for deploy. As-built notes in §6.9.

**Patch 2c (added 2026-05-05)** addresses Bugs 5 and 6 — both newly discovered during deploy prep for Patch 2. Three files in the Patch 2 bundle now: `oi_flow.py`, `persistent_state.py`, `omega_dashboard/data.py`.

## §1. Methodology and corrections from previous Walk 1

The earlier audit framed `gex_sign` as having "5 writer paths, 3 conventions, 30+ readers." Verifying scope on each writer path corrected the picture significantly:

| Earlier framing | Verified scope | Correction |
|---|---|---|
| "5 literal-sign writers" | Of the 5 sites identified, **2 write to persistent stores** (`app.py:11568` writes em_predictions dict, `app.py:14040` passes to regime_detector.update). The other 3 are **function-local computations** in `_post_em_card`, `_should_use_long_options`, and `should_use_long_option`. | Only 2 cross-scope writers, not 5. |
| "Wrapper override and lightweight oi_flow regime writer" | Wrapper override at `thesis_monitor.py:4373-4378` is the canonical writer for `thesis.gex_sign`. Lightweight regime writer at `oi_flow.py:412` is **inside `estimate_gex_from_chain` which Patch 2a disables** — no longer a writer post-deploy. | One regime writer, not two. |
| "3 default-to-positive fallback writers" | Confirmed. All three are silent-default antipatterns. | Unchanged. |
| "30+ readers in thesis_monitor.py" | Confirmed. All read `thesis.gex_sign` and **all branch on regime semantics** (squeeze/fade, spread vs naked, pin zone, gap context). | Reader convention is uniformly regime. |

The single biggest correction: **the apparent literal-sign-vs-regime conflict between writer paths within `options_engine_v3.py` was scope-isolated.** Lines 1340 (literal-sign assignment) and 1432/1445 (regime-style branch) are in two different functions: `_should_use_long_options` (private) and `should_use_long_option` (public, no underscore). They don't share variables. Each takes its `gex_sign` either from a parameter or from local computation; they don't pass values to each other.

Same correction for `app.py`: the dollar-format-string writer at `:12030` is in `_post_em_card`, the regime-style readers at `:12741` and `:12749` are in `_app_spread_width`. Two different functions. The card renderer's local display variable is not the same `gex_sign` that determines spread width.

This is the **scope verification lesson** that should generalize to all future audits: variable name match is a necessary but not sufficient condition for "these are the same thing." Always verify the function boundary before assuming data flow.

## §2. Writer inventory (verified)

Sorted by what data store they affect, not just where the assignment happens.

### 2.1 Writers of `thesis.gex_sign` (the persisted ThesisContext attribute)

This is the field that drives ~30 trade-decision branches in `thesis_monitor.py`. There is exactly **one canonical writer**, plus one fallback default that fires only on load-from-Redis.

| Site | What it does | Convention |
|---|---|---|
| `thesis_monitor.py:4373` | Initial assignment: `gex_sign = "positive" if gex_val >= 0 else "negative"` | Literal sign of net GEX |
| `thesis_monitor.py:4377-4378` | Override based on flip distance — replaces line 4373's value if spot is far from flip | **Regime** (above-flip = "positive", below-flip = "negative") |
| `thesis_monitor.py:4385` | Constructs the `ThesisContext(...)` dataclass with `gex_sign=gex_sign` (the local variable just computed) | Pass-through of override result |
| `thesis_monitor.py:920` | `_persist_thesis` writes `data["gex_sign"] = thesis.gex_sign` to Redis key `thesis_monitor:{ticker}` | Pass-through (whatever was on the dataclass) |
| `thesis_monitor.py:952` | Load-from-Redis: `gex_sign=d.get("gex_sign", "positive")` — **silent default to "positive" if missing** | **Default antipattern** |

The effective convention written to `thesis_monitor:{ticker}.gex_sign` is **regime** in 100% of cases except when the override at 4377-4378 doesn't fire (small flip-distance window where `-1.5% < d < 1.5%`). In that small window, the value reverts to literal sign (line 4373's initial assignment). Inside that ±1.5% window, literal sign and regime usually coincide (because spot near flip means GEX value is near zero, and the literal-sign distinction is meaningless), so this is rarely visible as a bug.

**The one issue here is the silent default at line 952.** When loading a stale or partial blob from Redis, missing `gex_sign` becomes `"positive"`. That's a silent fabrication. Project notes explicitly warn against this pattern.

### 2.2 Writers of `em_predictions` row's `gex_sign` column

The `em_predictions` Google Sheet is your reconciliation/journaling surface — it's where you go to QA past trades and see how the bot's bias evolved. Two writers feed it:

| Site | Function | What it does | Convention |
|---|---|---|---|
| `app.py:11568` | (within an em_predictions row builder, function unknown without further grep) | `"gex_sign": "positive" if eng.get("gex", 0) >= 0 else "negative"` | **Literal sign** |
| `app.py:14040-14045` | (in a regime-detector update path) | `_gex_sign = "negative" if _gex_data.get("gex", 0) < 0 else "positive"` then passed to `_regime_detector.update(...)` | **Literal sign** |

**This is the cross-store inconsistency.** For AAPL morning 2026-05-04:
- `thesis_monitor:AAPL.gex_sign` = `"positive"` (regime, from the wrapper override — spot above flip)
- `em_predictions` AAPL row `gex_sign` = `"negative"` (literal, from `app.py:11568` — gex value -94.54M)

Same conceptual field name, two stores, opposite values. **Both are individually correct under their convention, but together they're confusing for QC.** When you look at em_predictions in two weeks and try to back-test "did spread setups work in `gex_sign=negative` regimes?", you'll get noise from rows where "negative" actually means "GEX value was negative" rather than "expansion regime."

### 2.3 Writers of `gex:{ticker}` Redis key

After Patch 2a deploys, this list goes to zero. Pre-patch:

| Site | What it does | Convention |
|---|---|---|
| `oi_flow.py:412` | Inside `estimate_gex_from_chain`: `gex_sign = "positive" if spot >= gamma_flip else "negative"` | Regime (above/below flip) |
| `oi_flow.py:971` | The `_json_set("gex:{ticker}", gex, ttl=7200)` write that consumes line 412's output | Persists regime |

**Status post-Patch 2a:** disabled. Function `estimate_gex_from_chain` is still defined for inline calls but no longer writes to Redis. Closes this writer category entirely.

### 2.4 Writers of webhook/v4_flow `gex_sign` field

This is the noisiest area, but it doesn't touch trade decisions on production paths.

| Site | What it does | Convention |
|---|---|---|
| `options_engine_v3.py:2586` | `gex_sign=webhook_data.get("gex_sign", v4_flow.get("gex_sign", "positive") if v4_flow else "positive")` | **Default to "positive"** (silent) |
| `options_engine_v3.py:2641` | Same pattern | **Default to "positive"** (silent) |

These read `gex_sign` from incoming webhook data with a default. The default is "positive" — meaning if a webhook arrives without `gex_sign`, the engine assumes positive regime and continues. **Silent fabrication.** Same project-notes violation as line 952.

### 2.5 Function-local `gex_sign` variables (NOT writers of any persistent state)

These were the misleading hits in the original grep. Listing them here for completeness, but they don't affect the structural audit because they don't cross scopes.

| Site | Function | Local convention | Cross-scope risk |
|---|---|---|---|
| `app.py:12030` | `_post_em_card` (line 11923) | `"+$1.5M"` / `"-$2.0M"` (display string) | None — variable doesn't escape function |
| `options_engine_v3.py:1340` | `_should_use_long_options` (line 1311) | Literal sign, computed from `v4_flow.get("gex")` | None — used locally for `LONG_PUT_GEX_REQUIRED` config check at line ~1352 |

**No cross-scope contamination.** These are refactor traps (if someone moves code around without renaming, they could collide), but no current bug.

### 2.6 Backtest-only writer

| Site | Function | Convention |
|---|---|---|
| `backtest/backtest_replay.py:368` | `estimate_gex_sign(prior_day) -> str` | **Historical-pattern proxy** — returns "negative" if prior day was a strong trend day, "positive" if quiet/ranging. Different from both literal sign and regime. |

This is a heuristic that GUESSES today's regime from yesterday's price action. Used only by `backtest_replay.py:423` for replaying historical scenarios. Not a production trade-decision input. Worth knowing about if anyone ever re-uses `estimate_gex_sign` outside the backtest, but not a structural issue.

## §3. Reader inventory (verified)

Categorized by what semantic convention the reader expects.

### 3.1 Readers that branch on `thesis.gex_sign` for trade decisions (ALL EXPECT REGIME)

This is the consequential category. Spread-vs-naked picks, exit recalibration, scoring boosts, retest narration — all read `thesis.gex_sign` and all interpret it as regime.

| File:line | What it does | Convention expected |
|---|---|---|
| `entry_validator.py:276,279,282,285` | Score breakouts/failures by `gex_sign × setup_type` matrix | Regime |
| `entry_validator.py:369-390` | Confidence boosts and adjustments | Regime |
| `exit_policy.py:189,205,211` | Detect regime flip during open trade, recalibrate | Regime (specifically tracks regime CHANGES) |
| `exit_policy.py:232,249,266,283` | Adjust exit thresholds by `gex_sign × setup_type` | Regime |
| `thesis_monitor.py:1117-1123` | Gap context × regime decision matrix (4 branches) | Regime |
| `thesis_monitor.py:1661` | `direction == "LONG" and thesis.gex_sign == "negative"` → naked calls path | Regime (negative = expansion = use nakeds) |
| `thesis_monitor.py:2080-2084` | Recalibrate trade if `thesis.gex_sign != trade.policy_config.get("gex_at_entry")` | Regime change detection |
| `thesis_monitor.py:2579` | Crisis snapback check requires `thesis.gex_sign == "negative"` | Regime |
| `thesis_monitor.py:2915` | Pin-zone logic only fires in `gex_sign == "positive"` | Regime |
| `thesis_monitor.py:2927-2930` | Action narration: "Failed break = squeeze" vs "Break with momentum = short" | Regime |
| `thesis_monitor.py:2958, 3176, 3187, 3395` | Various regime-conditional branches | Regime |
| `thesis_monitor.py:3288-3296` | Squeeze/fade probability narration | Regime |
| `thesis_monitor.py:3322-3331` | **Spread vs naked instrument selection** (the most consequential reader) | Regime |
| `oi_flow.py:2587` | GEX context narration for conviction plays | Regime (read via `_state.get_gex_sign()` which has thesis fallback) |
| `options_engine_v3.py:1432, 1445` | Long-option GEX note: "GEX- (accelerant)" vs "GEX+ (mean reversion)" | Regime |

**All 30+ readers want regime.** No reader I found explicitly needs literal sign. The wrapper override at `thesis_monitor.py:4377-4378` is doing exactly the right thing for these consumers.

### 3.2 Readers that branch on a function-parameter `gex_sign`

These take `gex_sign` as input and don't care about provenance — whatever the caller passes in, the function branches on it. The convention they expect depends entirely on the caller.

| File:line | Function | Caller's responsibility |
|---|---|---|
| `app.py:12741, 12749` | `_app_spread_width` | Caller passes `gex_sign_str` (regime, sourced from thesis) |
| `options_engine_v3.py:1340-1352` | `_should_use_long_options` | Caller passes `v4_flow` dict; function computes `gex_sign` LOCALLY from `v4_flow.get("gex")` |
| `options_engine_v3.py:1432, 1445` | `should_use_long_option` | Caller passes `gex_sign` parameter; function expects regime |

These are well-defined function APIs and not bugs in themselves. The risk is what the caller actually passes. For `_should_use_long_options`, the function side-steps the issue by computing locally; for `should_use_long_option`, it trusts the caller.

### 3.3 Display-only readers (don't make decisions)

| File:line | What it shows |
|---|---|
| `app.py:1470` | `f"GEX: {thesis.gex_sign} \| Regime: {thesis.regime}"` |
| `app.py:12491` | Log line in some debug path |
| `regime_detector.py:117` | `f"GEX: {self.gex_sign}"` |
| `dashboard.py:1667` | Dashboard pill cell |
| `omega_dashboard/data.py:1514` | Dashboard sign normalization for display |
| `thesis_monitor.py:914, 958, 2910, 3517, 4027` | Log lines, monitor message headers, status display |
| `backtest_replay.py:954` | Backtest log |

Display readers don't care about convention as long as the value is one of `"positive"` / `"negative"` / `""`. They render whatever they get. No structural concern.

### 3.4 Pass-through (kwarg propagation)

Many sites in `thesis_monitor.py` (1469, 1496, 1564, 1580, 1590, 1631, 1939, 3160, 3269) pass `thesis.gex_sign` as a kwarg to sub-functions. These don't read or branch on the value — they just propagate it. Convention is whatever the original writer set; no structural concern.

`position_monitor.py:367` is similar: `pos.thesis_gex_sign = thesis.gex_sign or ""` — stores the value at trade entry for later regime-flip detection in `exit_policy.py:189`. Pass-through.

## §4. Compatibility analysis

The matrix that matters: where do writers and readers from different convention families cross paths?

| Writer convention | Writer site | Reader convention | Reader site | Match? |
|---|---|---|---|---|
| Regime (canonical) | `thesis_monitor.py:4373-4378` → `thesis_monitor:{ticker}` | Regime | All ~30 thesis_monitor.py readers | ✅ correct |
| Regime (canonical) | same | Regime | `oi_flow.py:2587` (via `_state.get_gex_sign`) | ✅ correct |
| Default-to-positive | `thesis_monitor.py:952` (load fallback) | Regime | All ~30 readers | ⚠️ silent fabrication when fires |
| Default-to-positive | `thesis_monitor.py:477` | Regime | Same readers | ⚠️ silent fabrication when fires |
| Default-to-positive | `options_engine_v3.py:2586, 2641` | Regime (function param) | `should_use_long_option` callers | ⚠️ silent fabrication when fires |
| Literal sign | `app.py:11568` → em_predictions dict | (no decision reader — em_predictions is journaled, not consumed for trades) | n/a | ⚠️ correct at write time but mislabels the column |
| Literal sign | `app.py:14040` → `regime_detector.update(gex_sign=...)` | Need to verify regime_detector.update's expectation | TBD | **needs further audit** |
| Local literal sign (function-local) | `options_engine_v3.py:1340` | Same function | `options_engine_v3.py:1352` | ✅ self-consistent |
| Local display string | `app.py:12030` | Same function | Same function (display only) | ✅ self-consistent |

**Two things stand out:**

1. **`app.py:14040 → _regime_detector.update(gex_sign=_gex_sign)`** — passes a literal-sign value into `regime_detector.update`. Need to grep for what convention `regime_detector` expects internally. If it expects regime, this is a real bug. If it expects literal sign, fine.

2. **The default-to-positive antipattern** fires silently in three places. When it fires, downstream readers get `"positive"` regardless of the actual GEX state. Project notes explicitly warn against this. **Should be killed in Patch 3.**

## §5. Real bugs found, ranked

### Bug 1 (high): Cross-store inconsistency in `em_predictions`

`em_predictions` Google Sheet rows have a `gex_sign` column. The two writers (`app.py:11568` and `:14040`) write **literal sign** of the GEX value. But when you look at the same row via `thesis_monitor:{ticker}` (e.g., to cross-reference what the trade decision saw), you'll see **regime**. Same field name, different values, depending on which store you read.

**Impact for QC:** If you query "show me all rows where `gex_sign == 'negative'`" expecting to find expansion-regime trades, you'll get a mix of (a) actually-expansion regime trades and (b) literally-negative-GEX-value trades (which are usually but not always expansion regime).

**Fix:** Either rename the em_predictions column to `gex_value_sign` to clearly distinguish, OR change the writers to compute regime instead of literal sign. The first is safer (no behavior change); the second is more aligned with the rest of the codebase.

### Bug 2 (medium): Three silent-default-to-positive sites

| Site | Trigger | Effect |
|---|---|---|
| `thesis_monitor.py:477` | `getattr(thesis, "gex_sign", "positive") if thesis else "positive"` | Silent "positive" when thesis is None or has no gex_sign attribute |
| `thesis_monitor.py:952` | `gex_sign=d.get("gex_sign", "positive")` on Redis load | Silent "positive" when loaded blob is missing the field |
| `options_engine_v3.py:2586, 2641` | `webhook_data.get("gex_sign", v4_flow.get("gex_sign", "positive")...)` | Silent "positive" when webhook missing field and no v4_flow data |

**Impact:** Project notes explicitly say: "Silent `except: pass` is the enemy; at minimum log.debug with context." Silent default to "positive" is the same antipattern in a different form. When the default fires, downstream readers make trade decisions on a fabricated regime label.

**Fix:** Replace each default with explicit `None` or `""`. Consumers should handle missing data deliberately, not be fed a confident-looking but fake value.

### Bug 3 (low): Variable-name reuse refactor traps

| Pair | Status |
|---|---|
| `app.py:12030` (`_post_em_card` local display string) ↔ `app.py:12741` (`_app_spread_width` regime branch) | No bug today; refactor risk |
| `options_engine_v3.py:1340` (`_should_use_long_options` local literal sign) ↔ `options_engine_v3.py:1432` (`should_use_long_option` parameter) | No bug today; refactor risk |

**Fix:** Rename the function-local variables to disambiguate (`gex_sign_display`, `gex_sign_local`), or document the scope boundary in a comment so future refactors don't collapse the variables.

### Bug 4 (cosmetic): Misleading override log message

`thesis_monitor.py:4377`:
```python
if d > 1.5:
    gex_sign = "negative"
    log.info(f"Thesis GEX overridden: raw {gex_val:+.1f}M but spot {d:.1f}% below flip → negative")
```

The variable `d` in this code is `(flip - spot)/flip * 100` (positive when spot is BELOW flip). So `d > 1.5` corresponds to "spot more than 1.5% below flip," and the log message correctly says "below flip." That's actually right.

**However**, the elif branch at line 4378 is the inverse:
```python
elif d < -1.5:
    gex_sign = "positive"
```

There's no log line for this branch, which means when the override fires for "spot above flip," there's no log evidence. **The fix is to add a symmetric log message for the `d < -1.5` branch** so we can see both regime overrides in production logs.

### Bug 5 (high): Web dashboard ignores manual force-runs for level data

**Discovered:** 2026-05-05 during Patch 2 deploy preparation.
**Location:** `omega_dashboard/data.py:1644-1652` (`_read_em_log`).

The function reads `em_log:{today_utc}:{ticker}:silent` ONLY. Force-run thesis writes go to `em_log:{today_utc}:{ticker}:manual` — a separate Redis key. The dashboard's level panel never reads `:manual`, so force-runs are **silently invisible to the level panel**.

This was discovered when the user force-ran AAPL on 2026-05-04 23:29 CT to refresh data, then saw the dashboard card's level panel still showing the morning silent thesis values instead of the force-run's updated values. The whole purpose of force-running (refresh data because something changed) was being defeated for the levels panel surface.

**Compounds with Bug 1 / Patch 2 work:** when `gex:{ticker}` was manually flushed (during Patch 2 prep) and today's silent thesis hadn't run yet (pre-8:30 AM CT on 2026-05-05), the level panel went **completely empty** — `_build_levels` returned `{"available": False}` because both `gex` and `em_log:silent` were missing, even though `em_log:manual` had today's data.

**Impact:** any time the user force-runs a ticker outside the 8:30 AM CT silent window, the levels panel does not reflect the force-run. This is invisible to the user — the panel just keeps showing whatever it had before. No log warning, no UI indicator that the panel is stale relative to the most recent thesis build.

**Fix (in Patch 2c):** `_read_em_log` reads both `:silent` and `:manual` and returns whichever has the newer `logged_at_utc` timestamp. Three-line change. Tests verify: T2 (manual-only returns manual), T4 (both present, manual newer wins), T5 (both present, silent newer wins).

**Generalizable lesson:** Walk 1B audit protocol should add: **for every Redis key the dashboard reads, enumerate every writer of that key family.** If a `:silent` reader exists, check whether `:manual`, `:overnight`, `:adhoc` etc. variants are also written by other code paths and need fallback logic. The pattern "reader hardcoded to one variant of a multi-variant key family" is a recurring bug class.

### Bug 6 (medium): Web dashboard's `_read_gex` bypasses the persistent_state fallback chain

**Discovered:** 2026-05-05, same audit as Bug 5.
**Location:** `omega_dashboard/data.py:1655-1661` (`_read_gex`).

The function reads `gex:{ticker}` directly via `_json_get`, bypassing `bot_state.get_gex_data()` which has the proper thesis fallback (Patch 2b rev1). This was actually two separate problems:

1. **Pre-Patch-2:** `_read_gex` read the lightweight (and wrong) values that `oi_flow.py:971` was writing. The dashboard preferred these wrong-but-fresh values via the explicit "GEX live takes precedence" priority logic in `_build_levels`.
2. **Post-Patch-2 (writer disabled, no manual flush):** `_read_gex` read the same wrong values until the 2-hour TTL expired. No fallback to thesis.
3. **Post-Patch-2 + manual flush:** `_read_gex` returned None entirely. Levels panel went empty unless `em_log:silent` was also available.

**Fix (in Patch 2c):** `_read_gex` is rerouted through `bot_state.get_gex_data()`, which honors the Source 1a / 1b / 2 fallback chain Patch 2b rev1 set up. The function still returns the same shape `{gamma_flip, gex_sign, call_wall, put_wall, max_pain}`, just sourced via the proper fallback path.

**This decouples the web dashboard from the lightweight `gex:{ticker}` key's lifecycle entirely.** Even if that key never gets written (post-Patch-2a), the web card always has thesis-sourced data.

## §6. Patch 3 design (rev1, post-review) — AS BUILT 2026-05-05

**Status:** This section was the design spec. Patch 3 was built against it on 2026-05-05 and the actual implementation matches §6.1, §6.5, §6.6, and §6.7 verbatim. **One small deviation in §6.2** — the denominator on the `dealer_regime` distance — is documented in §6.9. The deviation makes the implementation mirror the existing wrapper override exactly (which was the stated goal of §6.3) rather than the slightly-off pseudocode here. Test results live in §6.8.

**Original §6 in this doc proposed three options (A/B/C) and recommended a small ~12-line fix. After post-review pushback that finding was wrong.** The reviewer correctly identified that scope-isolation alone doesn't protect against convention drift at function-call boundaries (we don't have full caller-side data on every site that passes `gex_sign` as a parameter). The safer engineering move is **purely additive new fields** — no behavior change, no reader migration, no risk of surfacing latent bugs.

The original §6 (kill silent defaults + add em_predictions column) is **deferred to Patch 4** so the silent-default removal happens after the new explicit fields are available as a fallback.

### §6.1 Three new fields, populated alongside existing `gex_sign`

```python
# Added to ThesisContext dataclass:
gex_value_sign: str = "neutral"     # literal sign of net GEX value
flip_location: str = "unknown"      # spot vs flip, ignoring magnitude
dealer_regime: str = "unknown"      # what the wrapper override decides

# gex_sign stays unchanged — kept as legacy alias for now
```

### §6.2 Build logic in `build_thesis_from_em_card`

```python
gex_val = eng.get("gex", 0)
flip = eng.get("flip_price")

# Field 1 — literal sign of net GEX
if gex_val > 0:
    gex_value_sign = "positive"
elif gex_val < 0:
    gex_value_sign = "negative"
else:
    gex_value_sign = "neutral"

# Field 2 — flip location (independent of sign)
if flip and spot:
    d = (spot - flip) / flip * 100   # positive = spot above flip
    if abs(d) < 0.25:
        flip_location = "at_flip"
    elif d > 0:
        flip_location = "above_flip"
    else:
        flip_location = "below_flip"
else:
    flip_location = "unknown"

# Field 3 — dealer regime: MIRROR existing wrapper override at lines 4377-4378
#   Above flip (>1.5%)   → pin_range
#   Below flip (>1.5%)   → trend_expansion
#   Near flip (±1.5%)    → fall back to literal sign
#   Unknown flip data    → unknown
if flip and spot:
    d_inv = (flip - spot) / flip * 100
    if d_inv < -1.5:
        dealer_regime = "pin_range"
    elif d_inv > 1.5:
        dealer_regime = "trend_expansion"
    else:
        dealer_regime = "pin_range" if gex_val >= 0 else "trend_expansion"
else:
    dealer_regime = "unknown"
```

### §6.3 Critical correctness invariant

**`dealer_regime` MUST agree with `thesis.gex_sign` for every ticker on every run, except when `flip` is missing.** This is enforced by deriving both from the same wrapper-override semantics. If they ever disagree in production, that's evidence the wrapper itself drifted.

For AAPL morning 2026-05-04 verification:
- `gex_value = -94.54` → `gex_value_sign = "negative"`
- `spot 279.57`, `flip 273.14`, `d = +2.35%` → `flip_location = "above_flip"`
- `d_inv = -2.35`, `d_inv < -1.5` → `dealer_regime = "pin_range"`
- Existing wrapper: `d > 1.5` is FALSE, `d < -1.5` is TRUE → `gex_sign = "positive"`
- ✓ `dealer_regime "pin_range"` ↔ `gex_sign "positive"` — agree

For SOXX (from bot log, 2026-05-04 13:37:48):
- `gex_value = +1.9M` → `gex_value_sign = "positive"`
- `spot 7.4% below flip` (per log) → `flip_location = "below_flip"`
- `d_inv > 1.5` → `dealer_regime = "trend_expansion"`
- Existing wrapper logged: `gex_sign = "negative"`
- ✓ `dealer_regime "trend_expansion"` ↔ `gex_sign "negative"` — agree

This invariant is the test condition for the rev1 patch. Run a live diff for one trading day; if any ticker shows `dealer_regime != gex_sign`-equivalent-regime, investigate before deploying further migrations.

### §6.4 Why the auditor's `dealer_regime` formula was wrong

The pre-review version of this doc accepted the auditor's proposed formula:
```python
if gex_value_sign == "positive":
    dealer_regime = "pin_range"
elif gex_value_sign == "negative" and flip_location in ("above_flip", "below_flip"):
    dealer_regime = "trend_expansion"
```

This formula treats literal sign as the regime determinant and ignores flip distance. For AAPL morning, this would produce `dealer_regime = "trend_expansion"` while the wrapper says `pin_range` — opposite regimes. Deploying that would mean `dealer_regime` and `thesis.gex_sign` disagree from day one, which defeats the purpose of "additive, no behavior change."

Corrected formula in §6.2 mirrors the existing wrapper exactly, preserving the dealer-mechanics intuition: above flip with positive-gamma strikes between spot and flip → suppression regime, regardless of literal net GEX sign.

### §6.5 Persist/load changes in `thesis_monitor.py:920` and `:952`

In `_persist_thesis` (line 920), add the three new fields to the data dict:

```python
data = {
    ...existing fields...,
    "gex_value_sign": thesis.gex_value_sign,
    "flip_location":  thesis.flip_location,
    "dealer_regime":  thesis.dealer_regime,
    ...
}
```

In `_load_thesis_from_store` (line ~952 — note: the actual function is named `_load_thesis_from_store`, not `load_thesis`; design doc said `load_thesis` for brevity), load with safe defaults that never silently fabricate:

```python
t = ThesisContext(
    ...,
    gex_sign=d.get("gex_sign", "positive"),  # UNCHANGED — legacy default stays for now
    gex_value_sign=d.get("gex_value_sign", "neutral"),  # NEW
    flip_location=d.get("flip_location", "unknown"),    # NEW
    dealer_regime=d.get("dealer_regime", "unknown"),    # NEW
    ...
)
```

The new fields use `"neutral"` / `"unknown"` as their default — explicit non-decisions, not silent fabrications. Future readers can branch on `"unknown"` to mean "field not yet populated for this thesis," distinct from any decision label.

### §6.6 Total scope of Patch 3

| Change | File | Lines |
|---|---|---|
| Add 3 fields to `ThesisContext` dataclass | `thesis_monitor.py` (dataclass def) | 3 |
| Build logic in `build_thesis_from_em_card` | `thesis_monitor.py:4373-4385` | ~25 |
| Add fields to `_persist_thesis` data dict | `thesis_monitor.py:920` | 3 |
| Add fields to `load_thesis` constructor | `thesis_monitor.py:952` | 3 |
| Symmetric override log line (rev1: defer to optional) | `thesis_monitor.py:4378` | 1 |

**~30 lines, all in `thesis_monitor.py`.** No file outside `thesis_monitor.py` is touched. No reader migrated. No existing behavior changed. New fields are written but unread.

### §6.7 What Patch 3 explicitly does NOT do

- **No silent-default removal.** Defer to Patch 4. The defaults at `thesis_monitor.py:477`, `:952`, `options_engine_v3.py:2586/2641` stay as-is. Once `dealer_regime` is populated and proven correct, Patch 4 can replace silent defaults with explicit `"unknown"` returns and update consumers to handle them.
- **No em_predictions schema change.** Defer to Patch 4 alongside silent-default removal. Patch 3 doesn't write any new field to the sheet.
- **No reader migration.** The 30+ readers of `thesis.gex_sign` keep reading `gex_sign`. Patch 4+ migrates them one by one to whichever new field they actually need (most will move to `dealer_regime`).
- **No variable-name reuse cleanup** in `app.py` or `options_engine_v3.py`. Document only; refactor risk stays as known.
- **No `gamma_flip` / wall / max_pain audit.** Walk 1C and 1D for those fields. Patch 3 is gex_sign-domain only.

### §6.8 Verification protocol — pre-deploy results + post-deploy check

**Pre-deploy test results (2026-05-05, run against the actual modified `thesis_monitor.py`):** 27/27 checks passed.

| # | Test                                                       | Result |
| - | ---------------------------------------------------------- | ------ |
| 1 | AST clean                                                  | PASS   |
| 2 | AAPL morning case (gex −94.54M, spot above flip)           | 4/4    |
| 3 | SOXX case (gex +1.9M, spot below flip)                     | 4/4    |
| 4 | Edge case: no `flip_price` provided                        | 4/4    |
| 5 | Persist/load roundtrip preserves all three fields          | 4/4    |
| 5b| Legacy Redis blob (no new keys) hydrates to defaults       | 4/4    |
| 6 | `at_flip` band straddle (±0.25 %)                          | 2/2    |
| 7 | `gex_value == 0` → `gex_value_sign = "neutral"`            | 2/2    |
| 8 | Within ±1.5 % band, negative gex → `trend_expansion`       | 2/2    |

The lockstep invariant from §6.3 — `dealer_regime` agreeing with `gex_sign` on every override case — held across Tests 2, 3, 7, and 8. Test 5b additionally confirmed that legacy Redis blobs without the new keys hydrate to safe defaults (`"neutral"` / `"unknown"`) rather than silently fabricating regime labels.

**Post-deploy invariant check.** For one full trading day after Patch 3 ships, compare the new fields against the legacy `gex_sign` for every ticker on every silent-thesis run:

```bash
# Pull every thesis_monitor blob, check the invariant
for ticker in AAPL MSFT NVDA TSLA SPY QQQ IWM ...; do
    redis-cli --raw GET "thesis_monitor:$ticker" | python3 -c '
import json, sys
t = json.load(sys.stdin)
gs = t.get("gex_sign", "")
dr = t.get("dealer_regime", "")
expected = "pin_range" if gs == "positive" else "trend_expansion" if gs == "negative" else "unknown"
status = "OK" if dr == expected else "MISMATCH"
print(f"{sys.argv[1]}: gex_sign={gs} dealer_regime={dr} → {status}")
' "$ticker"
done
```

Any MISMATCH row is a bug. Investigate before approving Patch 4.

### §6.9 As-built notes (2026-05-05)

**One deviation from §6.2 worth recording:** the design pseudocode for `dealer_regime` used `(flip - spot) / flip * 100` as the distance metric. The actual implementation uses `(flip - spot) / spot * 100` instead. This was a deliberate correction during build, not a slip.

**Why:** the existing wrapper override at `thesis_monitor.py:4373-4378` computes its distance as `(flip - spot) / spot * 100` (denominator: `spot`). §6.3 of this doc named "mirror the wrapper exactly" as the correctness invariant for `dealer_regime`. Using `/flip` in the new field while the wrapper uses `/spot` creates a small band of tickers near the ±1.5% boundary where the two could disagree (the percentages differ by a factor of `flip/spot`). Mirroring the wrapper's denominator guarantees agreement on every ticker by construction.

For the verification cases in §6.3:

| Case | `(flip-spot)/flip` (design) | `(flip-spot)/spot` (built) | Same outcome? |
| --- | --- | --- | --- |
| AAPL: spot 279.57, flip 273.14 | -2.355% | -2.300% | Both < -1.5 → `pin_range` ✓ |
| SOXX: spot 465.81, flip 503.0 | +7.40% | +7.98% | Both > +1.5 → `trend_expansion` ✓ |

Production cases unchanged. Only borderline tickers (where the percentage sits between the two formulations of "1.5%") are affected, and those are exactly the cases where the design intent — match the wrapper — demands the wrapper's denominator.

**Self-contained block.** The new build block in `build_thesis_from_em_card` does not depend on the override block's local `d` variable being in scope. It recomputes its own distance value with the same formula. Cost: one extra division per em-card. Benefit: no NameError surprises if the override's guard ever shifts (e.g., the override's `if flip is not None and spot > 0` is approximately but not identically equivalent to the new block's `if flip and spot`).

**Untouched per Patch 3 rule:** the existing silent default `gex_sign=d.get("gex_sign", "positive")` on the load path was left in place. Removal is Patch 4 work. The new fields use `"neutral"` / `"unknown"` defaults so they don't introduce a new silent-fabrication site.

**Untouched per Walk 1B rev1 plan:** the symmetric override log line at `thesis_monitor.py:4378` (the `elif d < -1.5: gex_sign = "positive"` branch with no log statement) was not added. Deferred to Patch 4 per §6.6.

**Grep marker:** `# v9 (Patch 3):` appears at every edit site so the changes can be located later (4 sites in total: dataclass def, builder block, persist dict, load constructor).


## §7. Migration order (revised)

The plan stretches across multiple patches. Each is independently shippable.

**Patch 3 (this audit's deliverable — built and approved 2026-05-05):** Additive only. Three new fields populated, persisted, loaded. No reader changes. Test invariant: new fields agree with legacy `gex_sign`. **27/27 pre-deploy tests passed.** Awaiting deploy window. See §6.9 for as-built notes.

**Patch 4 (after Patch 3 has run clean for ~1 week):**
- Kill the 3 silent-default-to-positive sites — replace with explicit `"unknown"` reads of `dealer_regime` where applicable
- Update `em_predictions` row schema — add `gex_value_sign`, `flip_location`, `dealer_regime` columns alongside legacy `gex_sign`
- Add the symmetric override log line at `thesis_monitor.py:4378`

**Patch 5+ (much later, only if needed):**
- Migrate display readers (~10 sites) from `gex_sign` to `dealer_regime`. Display-only, low risk.
- Migrate behavior readers (~30 sites in thesis_monitor.py) from `gex_sign` to `dealer_regime`. **One reader at a time, one patch each.** Verify trade decisions don't change after each migration.

**Patch N (eventual):** Once all readers are migrated, deprecate `gex_sign` as an alias of `dealer_regime`. Optional final cleanup.

Rollback for each piece is straightforward — comment-out the change, redeploy.

## §8. Audit protocol lessons

Two protocol additions, generalized from this audit:

### 8.1 Verify scope before assuming data flow

When grep finds the same name in multiple places, **verify it's the same scope** before assuming they share data:

```bash
# Where does the closest preceding `def` live for each interesting line?
awk 'NR<=N && /^[[:space:]]*def /{name=$0; ln=NR} END{print ln":"name}' file.py
```

Variable name match is a necessary but not sufficient condition for "these are the same data flow." This audit caught two scope-isolation cases (app.py and options_engine_v3.py) that the original framing treated as contradictions but were actually independent locals.

### 8.2 Audit the getter, not just the field

Lesson from Patch 2b rev1: when reading "the value of field X" from a store, never assume what key the getter reads from. Always grep the getter's body. The original Patch 2b assumed `get_thesis()` reads from `thesis_monitor:{ticker}` — actually reads from `thesis:{ticker}` (a different, currently-empty key).

Generalizes for Walk 1B: when categorizing a "writer site," don't stop at the assignment statement. Trace what data store the value ends up in, and confirm the getter that reads it back uses the same convention. The em_predictions cross-store inconsistency in this audit was found this way: writer at `app.py:11568` writes literal sign, getter via `thesis_monitor:{ticker}.gex_sign` returns regime. Two stores, same field name, opposite values.

### 8.3 Check what fields silent-default to

Project notes already warn about silent `except: pass`. Generalize: any `default=...` parameter to `dict.get()` or `getattr()` is a silent-fabrication site if the default value is meaningful (not `None` or empty). For binary string fields like `gex_sign`, defaulting to `"positive"` (or `"negative"`) creates the same problem as `except: pass` — production code makes decisions on fake data.

Future audits should grep for `\.get\("[a-z_]+", "[a-z]+"\)` patterns and `getattr\([^,]+, "[a-z_]+", "[a-z]+"\)` patterns to surface these.

---

## Appendix A — Full grep evidence

Provided in chat history. Key files audited:
- `app.py` (sites: 1338, 1470, 1486-1497, 11568, 11588, 12030, 12491, 12741, 12749, 13205, 14040-14045)
- `thesis_monitor.py` (sites: 477, 914, 920, 952, 1117-1123, 1469, 1496, 1564, 1580, 1590, 1631, 1661, 1939, 2062, 2080-2084, 2579, 2910, 2915, 2927-2930, 2958, 3160, 3176, 3187, 3269, 3288-3296, 3322-3331, 3395, 3517, 3567, 3601, 3646, 3697, 4027, 4373-4385)
- `options_engine_v3.py` (sites: 1340-1352, 1432, 1445, 2586, 2641)
- `entry_validator.py` (sites: 276-285, 369-390)
- `exit_policy.py` (sites: 189, 205, 211, 232, 249, 266, 283)
- `oi_flow.py` (sites: 412, 675, 971, 2580, 2587)
- `persistent_state.py` (sites: 643, 650, 660, 671)
- `dashboard.py` (sites: 948, 1667)
- `omega_dashboard/data.py` (sites: 1514, 1658)
- `regime_detector.py` (sites: 117, 458)
- `position_monitor.py` (site: 367)
- `swing_engine.py` (site: 975)
- `backtest/backtest_replay.py` (sites: 368, 423, 939, 954)

## Appendix B — Open questions for future audits

1. **`regime_detector.update(gex_sign=...)`** — what convention does this expect? `app.py:14040` passes literal sign. If the regime detector internally treats this as regime, it's silently wrong. Worth a follow-up grep on `regime_detector.py`.

2. **`iv_skew:{ticker}` field structure** — same writer/reader pattern as `gex:{ticker}` (writer at `oi_flow.py:979`, reader at `:2604`). Walk 1C should audit. Likely smaller blast radius than gex_sign because both writer and reader are in the same file.

3. **`gamma_flip` writer paths** — earlier work identified `app.py:5965` writes from `_eng.flip_price` and `app.py:8036` writes from `dealer.gamma_flip`. Are these the same value computed by the same engine, or two independent computations? If two computations, they could disagree just like the gex blob did. Walk 1D candidate.

4. **The default-to-positive at `options_engine_v3.py:2586/2641`** comes from webhook data. Where do those webhooks originate, and does the originator know about the gex_sign field? Worth confirming the upstream source actually sets it (and what convention it uses) rather than relying on the silent default.
