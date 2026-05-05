# Phase 2 Deployment — Conviction Quality Gates + Backlog Bug Fixes

## Summary

Single-PR deploy spec covering:
- **Sweep pipeline fix** — one-line TypeError that has been silently dropping every real-time sweep
- **Conviction Gate A** — min-OI floor (ratio noise guard)
- **Conviction Gate B** — notional floor $10M tier-1 / $5M tier-2
- **Conviction Gate C** — immediate route requires sweep OR sustained flow
- **Conviction Gate D** — reactive + immediate → drop
- **Bug #1** — `LATE_DAY_HEURISTIC` no longer fires on 11/18 DTE
- **Bug #4** — exit signals no longer re-fire within 20 min

Phase 1 (thesis_monitor alert-key cooldown) is a **separate PR** — do not ship in this batch. Phase 2 only touches conviction-path code.

## Files touched

- `oi_flow.py` — constants, `handle_sweep`, `detect_conviction_plays`
- `app.py` — exit-cooldown gate at the post site

Total change: ~50 LOC, 2 files.

---

## Changes to `oi_flow.py`

### 1. New constants block

**Location:** after line 82 (immediately after `CONVICTION_EXIT_MIN_HOLD_SEC = 15 * 60`)

**Insert:**
```python
# ── v8.5 Conviction quality gates ──
CONVICTION_NOTIONAL_TIER1 = 10_000_000   # index + mega_cap: $10M premium floor
CONVICTION_NOTIONAL_TIER2 =  5_000_000   # everything else:  $5M floor
CONVICTION_MIN_OI = {                     # min OI before vol/OI ratio is trusted
    "index":     2000,
    "mega_cap":  1000,
    "large_cap":  500,
    "mid_cap":    250,
}
TIER1_NOTIONAL_TICKERS = (VOLUME_TIERS["index"]["tickers"]
                          | VOLUME_TIERS["mega_cap"]["tickers"])
CONVICTION_EXIT_COOLDOWN = 20 * 60       # Bug #4: suppress exit re-fires for 20 min
```

---

### 2. Sweep burst-field fix (unblocks real-time sweep pipeline)

**Location:** `handle_sweep()` at line 858

**Find:**
```python
            "burst": f"SWEEP ${sweep.get('notional', 0):,.0f}",
```

**Replace with:**
```python
            "burst": sweep.get("volume_delta", 0),  # numeric — consistent with chain alerts
```

**Why:** The string value crashes `detect_conviction_plays` at the `burst >= CONVICTION_MIN_BURST` comparison (str vs int TypeError), caught silently by the outer handler in `app.py:11788`. Every sweep since this path was added has been silently dropped. Display isn't affected — `format_conviction_play` uses `is_streaming_sweep` + `sweep_notional` for the SWEEP label.

---

### 3. Bug #1 — `LATE_DAY_HEURISTIC` DTE bound

**Location:** `detect_conviction_plays()` at line 2102

**Find:**
```python
            LATE_DAY_CUTOFF_MIN = 14 * 60 + 45  # 2:45 PM CT
            if (ct_minutes_total >= LATE_DAY_CUTOFF_MIN
                    and 3 <= alert_dte <= 30):
```

**Replace with:**
```python
            LATE_DAY_CUTOFF_MIN = 14 * 60 + 45  # 2:45 PM CT
            if (ct_minutes_total >= LATE_DAY_CUTOFF_MIN
                    and 3 <= alert_dte <= 7):   # was <=30 — 11/18 DTE shouldn't trip
```

**Why:** EOD retail-close concern only applies to short-dated contracts. 11 and 18 DTE options don't expire soon enough for EOD de-risking to be plausible. Log-only change; does not block any plays.

---

### 4. Gate A — Min-OI floor

**Location:** `detect_conviction_plays()`, insert **before** the existing vol/OI gate (currently at line 2112–2114)

**Find:**
```python
            # Gate: Vol/OI ratio
            if vol_oi < CONVICTION_MIN_VOL_OI:
                continue
```

**Replace with:**
```python
            # ── v8.5 Gate A: MIN OI FLOOR ──
            # vol/OI ratio is meaningless on tiny OI bases (Mon open, deep OTM,
            # Friday-post-expiry resets). Require real OI before trusting the ratio.
            oi_val = alert.get("oi", 0)
            _tier_name = next(
                (n for n, cfg in VOLUME_TIERS.items() if ticker in cfg["tickers"]),
                "mid_cap"
            )
            _min_oi = CONVICTION_MIN_OI.get(_tier_name, 500)
            if oi_val < _min_oi:
                continue  # insufficient OI base — ratio is noise

            # Gate: Vol/OI ratio
            if vol_oi < CONVICTION_MIN_VOL_OI:
                continue
```

---

### 5. Gate B — Notional floor

**Location:** `detect_conviction_plays()`, insert **after** the burst/overwhelming/sustained gate (currently ending at line 2129)

**Find:**
```python
            has_sustained = alert.get("sustained_flow", False)
            if not has_burst and not has_overwhelming and not has_sustained:
                continue

            # Gate: Clear direction
```

**Replace with:**
```python
            has_sustained = alert.get("sustained_flow", False)
            if not has_burst and not has_overwhelming and not has_sustained:
                continue

            # ── v8.5 Gate B: NOTIONAL DOLLAR FLOOR ──
            # Real institutional conviction is ≥$5-10M in premium, not N contracts.
            # 30k NVDA contracts × $0.15 = $450k is lottery flow, not smart money.
            _mid_early = alert.get("mid", 0) or 0
            if _mid_early <= 0:
                _mid_early = (alert.get("bid", 0) + alert.get("ask", 0)) / 2
            _notional_early = volume * _mid_early * 100
            _notional_floor = (CONVICTION_NOTIONAL_TIER1
                               if ticker in TIER1_NOTIONAL_TICKERS
                               else CONVICTION_NOTIONAL_TIER2)
            if _notional_early < _notional_floor:
                log.debug(f"💎 NOTIONAL FLOOR: {ticker} "
                          f"${_notional_early/1e6:.2f}M < ${_notional_floor/1e6:.0f}M")
                continue

            # Gate: Clear direction
```

---

### 6. Gate C — Immediate route requires sweep OR sustained

**Location:** `detect_conviction_plays()`, insert **after** the DTE→route assignment (currently line 2216–2223)

**Find:**
```python
            # Route by DTE
            if alert_dte <= 2:
                route = "immediate"
            elif alert_dte <= 7:
                route = "income"
            elif alert_dte <= 30:
                route = "swing"
            else:
                route = "stalk"

            # ── 0DTE NEVER ROUTES TO INCOME ──
```

**Replace with:**
```python
            # Route by DTE
            if alert_dte <= 2:
                route = "immediate"
            elif alert_dte <= 7:
                route = "income"
            elif alert_dte <= 30:
                route = "swing"
            else:
                route = "stalk"

            # ── v8.5 Gate C: IMMEDIATE AGGRESSIVENESS GATE ──
            # 0-2 DTE "CONVICTION PLAY" posts are highest-urgency alerts.
            # Require either a streaming sweep (urgency signal: filled across exchanges)
            # OR sustained flow (3+ consecutive 60s scans same direction).
            # "One scan, one big number" is where most false positives live.
            if route == "immediate":
                _has_sweep     = alert.get("is_streaming_sweep", False)
                _has_sustained = alert.get("sustained_flow", False)
                if not _has_sweep and not _has_sustained:
                    log.info(f"💎 IMMEDIATE DROP: {ticker} {alert_dte}DTE "
                             f"— no sweep, no sustained flow (single-scan spike)")
                    continue

            # ── 0DTE NEVER ROUTES TO INCOME ──
```

---

### 7. Gate D — Reactive + immediate → drop

**Location:** `detect_conviction_plays()`, at the reactive detection (currently line 2359–2360)

**Find:**
```python
            # ── PRE-MOVE FILTER ──
            # If stock moved >2% in last 30 min, flow is likely reactive
            # (hedging, profit-taking, closing losers) not predictive.
            recent_move_pct = self._get_recent_move_pct(ticker, lookback_min=30)
            is_reactive = recent_move_pct >= 2.0

            # Dollar estimate
```

**Replace with:**
```python
            # ── PRE-MOVE FILTER ──
            # If stock moved >2% in last 30 min, flow is likely reactive
            # (hedging, profit-taking, closing losers) not predictive.
            recent_move_pct = self._get_recent_move_pct(ticker, lookback_min=30)
            is_reactive = recent_move_pct >= 2.0

            # ── v8.5 Gate D: REACTIVE + IMMEDIATE = DROP ──
            # Reactive flow on 0-2 DTE is almost certainly hedging reaction to
            # the underlying move, not directional conviction. Promote the flag
            # to a gate for the immediate route only. Swing/income/stalk still
            # tolerate reactive flow (real institutions position into moves).
            if is_reactive and route == "immediate":
                log.info(f"💎 REACTIVE DROP: {ticker} moved {recent_move_pct:.1f}% "
                         f"in 30min — 0DTE flow is hedging, not directional")
                continue

            # Dollar estimate
```

---

## Changes to `app.py`

### 8. Bug #4 — Exit-signal cooldown at post site

**Location:** inside the main scanner's conviction post block. Apply to **all three** call sites that post conviction plays:

- **Scanner path** — around line 5945–5959
- **Secondary scanner path** — around line 7690 (same structure)
- **Sweep path** — around line 11767–11773

The pattern is identical in all three. Example using the first site:

**Find (line 5945 area):**
```python
                        # v7.2: Exit signals for positions never posted to user → log only.
                        # The direction flip is still tracked internally and logged to CSV,
                        # but the user shouldn't be told to "close" a position they never entered.
                        _is_phantom_exit = (cp.get("is_exit_signal") and
                                           not cp.get("exit_prior_was_posted", True))

                        if _is_shadow:
                            log.info(f"🔇 SHADOW: {cp['ticker']} {cp['trade_side']} ${cp['strike']:.0f} "
                                     f"— flow fights EM card ({cp.get('em_detail','')})")
                        elif _is_phantom_exit:
                            log.info(f"🔇 EXIT SIGNAL SUPPRESSED: {cp['ticker']} "
                                     f"— prior entry was never posted to user")
                        elif route == "immediate":
```

**Replace with:**
```python
                        # v7.2: Exit signals for positions never posted to user → log only.
                        # The direction flip is still tracked internally and logged to CSV,
                        # but the user shouldn't be told to "close" a position they never entered.
                        _is_phantom_exit = (cp.get("is_exit_signal") and
                                           not cp.get("exit_prior_was_posted", True))

                        # Bug #4: suppress exit re-fires within 20 min of last exit post.
                        # Real whipsaw on volatile tickers (NVDA flipped bull↔bear 3 times in
                        # 40 min on 4/20) was producing exit spam. One exit per session window.
                        _exit_within_cd = False
                        if cp.get("is_exit_signal") and not _is_phantom_exit:
                            _exit_cd_key = f"conviction_exit:{cp['ticker']}"
                            try:
                                from oi_flow import CONVICTION_EXIT_COOLDOWN
                                if not _flow_detector._state.check_and_set_cooldown(
                                        _exit_cd_key, CONVICTION_EXIT_COOLDOWN):
                                    _exit_within_cd = True
                            except Exception:
                                pass

                        if _is_shadow:
                            log.info(f"🔇 SHADOW: {cp['ticker']} {cp['trade_side']} ${cp['strike']:.0f} "
                                     f"— flow fights EM card ({cp.get('em_detail','')})")
                        elif _is_phantom_exit:
                            log.info(f"🔇 EXIT SIGNAL SUPPRESSED: {cp['ticker']} "
                                     f"— prior entry was never posted to user")
                        elif _exit_within_cd:
                            log.info(f"🔇 EXIT CD: {cp['ticker']} — exit already posted "
                                     f"within {CONVICTION_EXIT_COOLDOWN/60:.0f}min, suppressed")
                        elif route == "immediate":
```

Apply the **same insertion** (the `_exit_within_cd` block + the new `elif _exit_within_cd:` branch) to the parallel blocks at ~line 7690 and ~line 11767. Note the sweep path at 11767 has slightly different surrounding structure — it goes straight from `_is_phantom_exit` to the post, without route branches, so insert like:

```python
                            _is_phantom_exit = (cp.get("is_exit_signal") and
                                               not cp.get("exit_prior_was_posted", True))

                            # Bug #4: exit cooldown (sweep path)
                            _exit_within_cd = False
                            if cp.get("is_exit_signal") and not _is_phantom_exit:
                                _exit_cd_key = f"conviction_exit:{cp['ticker']}"
                                try:
                                    from oi_flow import CONVICTION_EXIT_COOLDOWN
                                    if not _flow_detector._state.check_and_set_cooldown(
                                            _exit_cd_key, CONVICTION_EXIT_COOLDOWN):
                                        _exit_within_cd = True
                                except Exception:
                                    pass

                            if _is_phantom_exit:
                                log.info(f"🔇 EXIT SIGNAL SUPPRESSED (sweep): {cp['ticker']} "
                                         f"— prior entry was never posted to user")
                            elif _exit_within_cd:
                                log.info(f"🔇 EXIT CD (sweep): {cp['ticker']} — "
                                         f"within {CONVICTION_EXIT_COOLDOWN/60:.0f}min, suppressed")
                            elif not cp.get("is_shadow_only"):
                                # ... existing post logic ...
```

---

## Pre-deploy verification

Run these on Render logs for a full trading day **before** deploying:

```bash
grep -c "SWEEP DETECTED"         render.log   # SweepDetector emits
grep -c "SWEEP: "                render.log   # handler receives
grep -c "Sweep handler error"    render.log   # TypeError crashes
```

Expected with current code:
- `SWEEP DETECTED` ≈ `SWEEP: ` ≈ `Sweep handler error` (all three match, confirming every sweep crashes)

Expected after this PR:
- `SWEEP DETECTED` ≈ `SWEEP: `, and `Sweep handler error` → 0

If `SWEEP DETECTED` is 0, streaming subscription isn't producing sweeps — separate issue, investigate `schwab_stream.py:472` `SweepDetector.check()` wiring. Does not affect the rest of Phase 2.

---

## Post-deploy verification

First full session after deploy, grep for:

```bash
grep -c "NOTIONAL FLOOR"         render.log   # Gate B drops
grep -c "IMMEDIATE DROP"         render.log   # Gate C drops
grep -c "REACTIVE DROP"          render.log   # Gate D drops
grep -c "EXIT CD"                render.log   # Bug #4 drops
grep -c "LATE_DAY_HEURISTIC"     render.log   # Bug #1 — should only fire on 3-7 DTE now
grep -c "FLOW CONVICTION\|CONVICTION PLAY\|EXIT SIGNAL" render.log  # total fires
```

**Expected behavior on a typical day:**
- Total conviction fires: was ~19–25, target ~5–10
- Exit signals: was 6/day, target 2–3/day
- `NOTIONAL FLOOR` drops: dozens per day (this is the biggest lever)
- `IMMEDIATE DROP` drops: handful per day
- `REACTIVE DROP` drops: 1–3 per day on volatile sessions
- `EXIT CD` drops: 1–2 per day (NVDA-style whipsaw)
- `LATE_DAY_HEURISTIC` lines: should only appear for 3-7 DTE fires after 2:45 CT

**Red flags:**
- Zero conviction fires all day → thresholds too tight, consider reducing tier-1 from $10M to $7.5M
- `NOTIONAL FLOOR` = 0 but fires continue high → Gate B didn't wire (check `volume * _mid_early * 100` math for zero-mid alerts)
- `Sweep handler error` still nonzero → sweep fix didn't apply cleanly

---

## Rollback

All changes are additive except three modifications:
1. `oi_flow.py:858` — single-line revert (change back to string)
2. `oi_flow.py:2102` — change `7` back to `30`
3. `oi_flow.py:2112` + `:2130` + `:2223` + `:2360` — remove four inserted blocks

The new constants and `app.py` changes are pure additions; reverting is deletion of the inserted blocks. No migration of state or cached data.

Git-safe: all changes are within existing functions, no signature changes, no new imports outside of the inline `from oi_flow import CONVICTION_EXIT_COOLDOWN` (which is a no-op if `oi_flow.py` hasn't been updated).

---

## NOT included in this PR

- **Phase 1 — thesis_monitor alert-key cooldown.** Separate PR. That one attacks 252 of 546 daily messages (the real whipsaw). This PR attacks 19–25. Deploy Phase 1 first or second — order doesn't matter since they touch disjoint code paths.
- **Bug #2 — per-flow-snapshot dedup.** Holding until Phase 1 + Phase 2 are in place for a week, to see whether `CONVICTION_COOLDOWN` + session dedup + notional floor already solve it without a third layer.
- **Bug #3 — dual-header verification.** No code change needed; verify empirically post-deploy that no same-minute dual-header pairs appear.
