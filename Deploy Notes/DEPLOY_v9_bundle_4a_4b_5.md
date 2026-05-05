# DEPLOY — v9 Patches 4a + 4b + 5 (bundle)

**Status:** 4a env-gated kill of silent defaults. 4b adds three new explicit
fields to em_predictions journaling. 5 migrates six ternary readers from
`gex_sign` to `dealer_regime`.
**Files touched:** `thesis_monitor.py`, `options_engine_v3.py`, `app.py`
**Tests:** 45 / 45 passing across the four suites
(`test_patch4c.py`, `test_patch4a.py`, `test_patch4b.py`, `test_patch5.py`).
**Markers:** `# v9 (Patch 4a):`, `# v9 (Patch 4b):`, `# v9 (Patch 5):`
**Prerequisite:** Patch 4c must already be deployed (it is — confirmed live).

## What ships

| Patch | What | Risk | Default |
|---|---|---|---|
| 4a | Replace silent `gex_sign="positive"` defaults at 4 sites with env-gated `_gex_sign_default()` helper | Zero behavior change unless `STRICT_GEX_SIGN=1` | OFF |
| 4b | Add `gex_value_sign`, `flip_location`, `dealer_regime` columns to em_predictions Sheet/CSV | Additive; Sheet auto-syncs headers | always on |
| 5 | Rewrite 6 `if gex_sign == "positive" else` ternaries to read `dealer_regime` via `_gex_branch` helper | Behavior change on unknown dealer_regime; safer than 4a alone | always on |

Patch 5 is what makes 4a actually correct. Without 5, strict mode would just
shift the silent-default bug from "fabricated positive" to "fabricated negative"
on the six ternaries. With 5 in place, unknown regimes get explicit
"unknown" handling — conservative messages for action lines, debit spreads
for instrument labels.

## Why bundle

Each patch is small individually, but they form a coherent unit:

- **4b** adds the columns we need to journal what each thesis actually
  decided (so we can audit `dealer_regime` over time).
- **5** ensures readers handle the new "unknown" state correctly. Without 5,
  flipping `STRICT_GEX_SIGN=1` would degrade six display strings; with 5,
  flipping it is safe and meaningful.
- **4a** then becomes a clean toggle once 5 has the readers covered.

Shipping in one commit keeps the deployment clean and avoids a window where
4a is on but 5 hasn't landed yet.

## Pre-deploy verification

```bash
# All four files should parse:
python3 -c "import ast; ast.parse(open('thesis_monitor.py').read())"
python3 -c "import ast; ast.parse(open('options_engine_v3.py').read())"
python3 -c "import ast; ast.parse(open('app.py').read())"

# All four test suites should pass:
python3 test_patch4c.py    # 6/6
python3 test_patch4a.py    # 12/12
python3 test_patch4b.py    # 13/13
python3 test_patch5.py     # 14/14
```

## Deploy steps

1. Pull the four files from `/mnt/user-data/outputs/` into the repo:
   `thesis_monitor.py`, `options_engine_v3.py`, `app.py`.
2. Run pre-deploy verification (above).
3. Confirm markers grep cleanly:
   ```bash
   grep -c "v9 (Patch 4a)" thesis_monitor.py options_engine_v3.py
   grep -c "v9 (Patch 4b)" app.py
   grep -c "v9 (Patch 5)" thesis_monitor.py
   ```
   Expected: 4a → 4 + 3, 4b → 2, 5 → 8.
4. Commit + push to `main`. Render auto-deploys.
5. Watch logs for the first thesis-build cycle after deploy. Expected:
   - No new errors on import.
   - Existing `Thesis GEX overridden: ... → negative` log lines continue.
   - The next em_predictions Sheet write should add three new columns to the
     header row automatically; old rows stay blank in those columns.
6. Optional: after a session or two of clean operation, set
   `STRICT_GEX_SIGN=1` in Render env vars to activate strict mode. The new
   readers (Patch 5) will continue working correctly.

## What changes when `STRICT_GEX_SIGN=1` is set

- `_load_thesis_from_store` on a blob missing `gex_sign` produces `""`
  instead of `"positive"`.
- `_contract_suggestion` called with `thesis=None` uses `""`.
- Webhook unpack sites in `options_engine_v3.py` use `""` when both
  `webhook_data` and `v4_flow` lack `gex_sign`.

The Patch 5 readers in `thesis_monitor.py` are independent of this — they
read `dealer_regime` either way. Strict mode only affects the legacy
`gex_sign` field.

## Rollback

- **Full revert:** revert the commit. Markers make sites greppable.
- **Strict-mode revert (no redeploy):** unset `STRICT_GEX_SIGN` (or set to
  `0`) in Render env. Behavior reverts to legacy on next service restart.
- **Patch 5 cannot be partially reverted via env var** — it's hard-wired.
  But its failure mode is graceful: readers see "unknown" → conservative
  messages.

## Tests

- **Patch 4c (already deployed)** — `test_patch4c.py`: 6 tests covering
  symmetric override log lines.
- **Patch 4a** — `test_patch4a.py`: 12 tests covering env-gated helper,
  Redis load path, contract-suggestion fallback, static audit. Sanity-checked
  by running against unpatched code: 11 of 12 fail (genuine bug-catcher).
- **Patch 4b** — `test_patch4b.py`: 13 tests covering the three new field
  formulas and fieldnames audit. Sanity-checked: T12 (fieldnames) fails on
  unpatched code.
- **Patch 5** — `test_patch5.py`: 14 tests covering `_gex_branch` helper,
  `dealer_regime` defaults, all 6 site migrations, and a strip-comments-then-
  grep audit ensuring no `gex_sign == "positive" else` ternaries remain in
  executable code. Sanity-checked: 12 of 14 fail on unpatched code.

Total: **45 / 45** tests pass on the bundle.

## Known limitations

- Patch 5's `unknown` branch fires whenever `dealer_regime` isn't `pin_range`
  or `trend_expansion`. Today this only happens when flip data is missing
  (rare in production). After more `→ positive` override events accumulate,
  a follow-up audit can confirm `dealer_regime` is reliably populated.
- The `gex_sign` field stays as a legacy alias. Deprecating it is multiple
  patches out — only after every reader is migrated. Patch 5 covered the
  six ternary readers; full audit of remaining `gex_sign` consumers is
  deferred.

## Diffs

| File | Lines changed |
|---|---|
| `thesis_monitor.py` | ~71 (4a helper + 4a edits + Patch 5 helper + 6 site migrations) |
| `options_engine_v3.py` | ~22 (4a helper + 2 webhook site edits) |
| `app.py` | ~33 (4b entry dict + fieldnames extension) |
| **Total** | ~126 |
