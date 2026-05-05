# DEPLOY — v9 Patch 4c (symmetric override log)

**File touched:** `thesis_monitor.py` (1 line changed)
**Risk:** Zero — observability only, no behavior change.
**Tests:** 6/6 passing (`test_patch4c.py`).
**Marker:** `# v9 (Patch 4c): symmetric log`

## What this patch does

The thesis-build override block at `thesis_monitor.py:4388–4389` had an
asymmetric log: the `d > 1.5` branch (spot below flip → forced `gex_sign =
"negative"`) emitted a `log.info`, but the `d < -1.5` branch (spot above flip →
forced `gex_sign = "positive"`) silently flipped the sign without logging.

After Patch 4c, both branches log identically:

```
Thesis GEX overridden: raw +12.3M but spot 2.1% below flip → negative
Thesis GEX overridden: raw -45.6M but spot 1.8% above flip → positive
```

This is the AAPL-morning case from the original audit — that branch had been
firing in production with no log evidence. Now we will see it.

## The diff

```diff
@@ -4386,7 +4386,7 @@
     if flip is not None and spot > 0:
         d = (flip - spot) / spot * 100
         if d > 1.5: gex_sign = "negative"; log.info(f"...{d:.1f}% below flip → negative")
-        elif d < -1.5: gex_sign = "positive"
+        elif d < -1.5: gex_sign = "positive"; log.info(f"...{-d:.1f}% above flip → positive")  # v9 (Patch 4c): symmetric log
```

`-d` is used because `d` is negative in this branch, and the message reads
more naturally as a positive percentage above flip.

## Why no env var

Project rule says new features get an env-var on/off. This is not a feature —
it is the missing half of an existing logging hook. Adding env-var gating only
to the negative-`d` branch would create the same asymmetry the patch fixes.
The matching `d > 1.5` log line is not env-gated either.

## Deploy steps

1. Pull `thesis_monitor.py` from `/mnt/user-data/outputs/` into the repo.
2. Confirm the patch marker is present: `grep "v9 (Patch 4c)" thesis_monitor.py`
   (should return 1 line).
3. AST sanity: `python3 -c "import ast; ast.parse(open('thesis_monitor.py').read())"`.
4. Commit + push to `main`. Render auto-deploys.
5. Watch logs for the next thesis-build cycle on a ticker that triggers the
   override. Either branch firing is fine — confirm both log lines appear in
   Render output across a few hours of trading.

## Acceptance gate (the reason this patch exists)

Track A's next piece (Patch 4a — kill silent `gex_sign="positive"` defaults)
should not ship until **both override branches have fired in production logs at
least once**. Without that witness evidence, removing the silent defaults would
leave us unable to tell whether `dealer_regime` is staying in lockstep with
`gex_sign` for the negative-`d` case.

Tail Render logs and grep for `Thesis GEX overridden`. When you see at least
one `→ positive` line in addition to `→ negative`, the gate is cleared.

## Rollback

Single line, marker-tagged. To revert: `grep -n "v9 (Patch 4c)" thesis_monitor.py`,
strip the trailing `; log.info(...)` portion of that line, redeploy. Or just
revert the commit.

## Tests

`test_patch4c.py` is included in the bundle. It exercises:

1. `d > 1.5` → existing log still fires (regression).
2. `d < -1.5` → new log fires with `→ positive` (the patch).
3. `|d| < 1.5` → neither log fires.
4. `flip=None` → neither log fires.
5. `spot=0` → neither log fires (guard intact).
6. Log-format symmetry across both branches.

Run: `python3 test_patch4c.py`. Expected: `6/6 passed`.

Sanity-checked by also running the same tests against the pre-patch file —
tests 2 and 6 fail there, confirming the suite catches the bug rather than
trivially passing.

## What's next

Once 4c has been live for a session or two and at least one `→ positive` log
line has been observed in Render, move to Patch 4a (kill silent defaults at
the three sites: `thesis_monitor.py:477`, `thesis_monitor.py:952`,
`options_engine_v3.py:2586`/`:2641`). Then 4b (em_predictions schema columns).
