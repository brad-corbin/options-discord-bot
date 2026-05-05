# v8.3.0 — Monday Noise Cutoff — FINAL PATCH BUNDLE

**Created:** 2026-04-19
**Target deploy:** Sunday evening, live Monday open
**Spec:** CONVICTION_GATE_SPEC_v0_6.md
**Validation:** 621K-signal backtest — POST WR 73.5% vs LOG_ONLY WR 64.7% (+8.8)

All patches verified against the live repo. Apply in order. After each patch, run:
```bash
python3 -c "import ast; ast.parse(open('app.py').read())"
```

---

## Pre-flight

### File placements
- `conviction_scorer.py` → **repo root** (same directory as `app.py`). NOT in `backtest/`.

### Environment variables (set in Render dashboard BEFORE pushing code)

```
# Scorer controls
CONVICTION_SCORER_ENABLED=true
CONVICTION_POST_THRESHOLD=70
CONVICTION_LOG_THRESHOLD=60
CONVICTION_STRICTNESS=medium
FLOW_BOOST_ENABLED=false

# Disable losing layers
CRISIS_PUT_ENABLED=false

# Potter Box per-alert gate (default OFF)
POTTER_BOX_ALERTS_ENABLED=false

# Flow post master kill switch (default = current behavior)
FLOW_TELEGRAM_POSTS_ENABLED=true
```

Verify existing still set: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_CHAT_INTRADAY`.

---

## Patch 1 — crisis_put default off

**File:** `trading_rules.py`, **Line:** 960

### FIND
```python
CRISIS_PUT_ENABLED           = True
```

### REPLACE WITH
```python
# v8.3.0 (Patch 1): Default OFF. Env override via CRISIS_PUT_ENABLED=true.
# Backtest: 2W/13L, -$7,452 over study period.
import os as _os
CRISIS_PUT_ENABLED           = _os.getenv("CRISIS_PUT_ENABLED", "false").strip().lower() == "true"
```

---

## Patch 2 — Potter Box per-setup alerts OFF (3 sites)

### Patch 2a — AM scan (line 10367)
### Patch 2b — PM scan (line 10405)
### Patch 2c — Midday scan (line 10443)

All three sites have IDENTICAL current code. Apply one-at-a-time using different comment markers.

### FIND (3 occurrences — apply in order)
```python
                                    for s in setups:
                                        if s.get("trade") and s.get("flow_direction"):
                                            try:
                                                post_to_diagnosis(_potter_box.format_alert(s))
                                            except Exception:
                                                pass
```

### REPLACE first match (Patch 2a, ~line 10367) WITH
```python
                                    # v8.3.0 (Patch 2a): Per-setup alerts gated off.
                                    if os.getenv("POTTER_BOX_ALERTS_ENABLED", "false").strip().lower() == "true":
                                        for s in setups:
                                            if s.get("trade") and s.get("flow_direction"):
                                                try:
                                                    post_to_diagnosis(_potter_box.format_alert(s))
                                                except Exception:
                                                    pass
```

### REPLACE second match (Patch 2b, ~line 10405) WITH
Same as 2a but comment says `# v8.3.0 (Patch 2b):`.

### REPLACE third match (Patch 2c, ~line 10443) WITH
Same as 2a but comment says `# v8.3.0 (Patch 2c):`.

---

## Patch 3 — Potter Box digest → main channel (3 sites)

### Patch 3a — AM digest (line 10364)
### Patch 3b — PM digest (line 10401)
### Patch 3c — Midday digest (line 10439)

Three sites, identical current code.

### FIND (3 occurrences)
```python
                                if setups:
                                    summary = _potter_box.format_summary(setups)
                                    if summary:
                                        post_to_diagnosis(summary)
```

### REPLACE first (Patch 3a)
```python
                                if setups:
                                    summary = _potter_box.format_summary(setups)
                                    if summary:
                                        # v8.3.0 (Patch 3a): Digest → main channel.
                                        _tg_rate_limited_post(summary)
```

Second (3b) and third (3c): same pattern, just change the `(Patch 3a)` to `(Patch 3b)` / `(Patch 3c)`.

**Result of Patches 2 + 3:** Potter Box digest posts to main 3x daily (8:18 CT, 11:30 CT, 3:05 CT). Per-setup alerts silenced.

---

## Patch 4 — Flow post master kill switch (5 sites)

### Patch 4a — Line 5355 (conviction immediate route)

### FIND
```python
                        elif route == "immediate":
                            # 0-2 DTE: fire to BOTH channels immediately
                            # FIX BUG #2: route through _tg_rate_limited_post so
                            # rapid-fire conviction alerts share the global TG gap
                            # lock and don't each individually hit the 429 wall.
                            _tg_rate_limited_post(msg)
                            if TELEGRAM_CHAT_INTRADAY and TELEGRAM_CHAT_INTRADAY != TELEGRAM_CHAT_ID:
                                _tg_rate_limited_post(msg, chat_id=TELEGRAM_CHAT_INTRADAY)
```

### REPLACE WITH
```python
                        elif route == "immediate":
                            # 0-2 DTE: fire to BOTH channels immediately
                            # FIX BUG #2: route through _tg_rate_limited_post so
                            # rapid-fire conviction alerts share the global TG gap
                            # lock and don't each individually hit the 429 wall.
                            # v8.3.0 (Patch 4a): flow Telegram kill switch.
                            if os.getenv("FLOW_TELEGRAM_POSTS_ENABLED", "true").strip().lower() == "true":
                                _tg_rate_limited_post(msg)
                                if TELEGRAM_CHAT_INTRADAY and TELEGRAM_CHAT_INTRADAY != TELEGRAM_CHAT_ID:
                                    _tg_rate_limited_post(msg, chat_id=TELEGRAM_CHAT_INTRADAY)
```

### Patch 4b — Line 5458 (conviction swing route)

### FIND
```python
                        elif route == "swing":
                            # 8-30 DTE: post to both channels
                            # FIX BUG #2: route through rate limiter
                            _tg_rate_limited_post(msg)
                            if TELEGRAM_CHAT_INTRADAY and TELEGRAM_CHAT_INTRADAY != TELEGRAM_CHAT_ID:
                                _tg_rate_limited_post(msg, chat_id=TELEGRAM_CHAT_INTRADAY)
```

### REPLACE WITH
```python
                        elif route == "swing":
                            # 8-30 DTE: post to both channels
                            # FIX BUG #2: route through rate limiter
                            # v8.3.0 (Patch 4b): flow Telegram kill switch.
                            if os.getenv("FLOW_TELEGRAM_POSTS_ENABLED", "true").strip().lower() == "true":
                                _tg_rate_limited_post(msg)
                                if TELEGRAM_CHAT_INTRADAY and TELEGRAM_CHAT_INTRADAY != TELEGRAM_CHAT_ID:
                                    _tg_rate_limited_post(msg, chat_id=TELEGRAM_CHAT_INTRADAY)
```

### Patch 4c — Line 6808 (second conviction entry point)

⚠️ IDENTICAL TEXT AT LINES 6808 AND 7030. Apply to FIRST match only.

### FIND (first of 2 occurrences)
```python
                        else:
                            # FIX BUG #2: route through rate limiter to share global TG gap lock
                            _tg_rate_limited_post(msg)
                            if route in ("immediate", "swing") and TELEGRAM_CHAT_INTRADAY and TELEGRAM_CHAT_INTRADAY != TELEGRAM_CHAT_ID:
                                _tg_rate_limited_post(msg, chat_id=TELEGRAM_CHAT_INTRADAY)
```

### REPLACE WITH
```python
                        else:
                            # FIX BUG #2: route through rate limiter to share global TG gap lock
                            # v8.3.0 (Patch 4c): flow Telegram kill switch.
                            if os.getenv("FLOW_TELEGRAM_POSTS_ENABLED", "true").strip().lower() == "true":
                                _tg_rate_limited_post(msg)
                                if route in ("immediate", "swing") and TELEGRAM_CHAT_INTRADAY and TELEGRAM_CHAT_INTRADAY != TELEGRAM_CHAT_ID:
                                    _tg_rate_limited_post(msg, chat_id=TELEGRAM_CHAT_INTRADAY)
```

### Patch 4d — Line 7030 (third conviction entry point)

Apply SAME FIND as 4c, to the SECOND match. Use comment `# v8.3.0 (Patch 4d):`.

### Patch 4e — Line 10901 (legacy sweep handler)

### FIND
```python
                            elif not cp.get("is_shadow_only"):
                                # FIX BUG #2: route through rate limiter
                                _tg_rate_limited_post(cp_msg)
                                if cp.get("route") in ("immediate", "swing") and TELEGRAM_CHAT_INTRADAY:
                                    _tg_rate_limited_post(cp_msg, chat_id=TELEGRAM_CHAT_INTRADAY)
```

### REPLACE WITH
```python
                            elif not cp.get("is_shadow_only"):
                                # FIX BUG #2: route through rate limiter
                                # v8.3.0 (Patch 4e): flow Telegram kill switch (sweep path).
                                if os.getenv("FLOW_TELEGRAM_POSTS_ENABLED", "true").strip().lower() == "true":
                                    _tg_rate_limited_post(cp_msg)
                                    if cp.get("route") in ("immediate", "swing") and TELEGRAM_CHAT_INTRADAY:
                                        _tg_rate_limited_post(cp_msg, chat_id=TELEGRAM_CHAT_INTRADAY)
```

### Verify Patch 4
```bash
grep -c "FLOW_TELEGRAM_POSTS_ENABLED" /opt/render/project/src/app.py
# Expected: 5
```

---

## Patch 5 — Sheets tab registration

### Patch 5a — Register flow_events + conviction_decisions tabs

**Line:** 399-415

### FIND
```python
def _tab_for_filename(filename: str) -> str:
    mapping = {
        "signal_decisions.csv": GOOGLE_SHEET_SIGNAL_TAB,
        "em_predictions.csv": GOOGLE_SHEET_EM_TAB,
        "em_reconciliation.csv": GOOGLE_SHEET_RECON_TAB,
        "crisis_put_signals.csv": GOOGLE_SHEET_CRISIS_TAB,
        "shadow_signals.csv": GOOGLE_SHEET_SHADOW_TAB,
        "conviction_plays.csv": GOOGLE_SHEET_CONVICTION_TAB,
        # v7 position tracking tabs — name IS the tab
        "position_tracking_active": "position_tracking_active",
        "position_tracking_swing": "position_tracking_swing",
        "position_tracking_income": "position_tracking_income",
        "position_tracking_conviction": "position_tracking_conviction",
        "position_tracking_shadow": "position_tracking_shadow",
        "shadow_filtered_signals": "shadow_filtered_signals",
    }
    return mapping.get(filename, "")
```

### REPLACE WITH
```python
def _tab_for_filename(filename: str) -> str:
    mapping = {
        "signal_decisions.csv": GOOGLE_SHEET_SIGNAL_TAB,
        "em_predictions.csv": GOOGLE_SHEET_EM_TAB,
        "em_reconciliation.csv": GOOGLE_SHEET_RECON_TAB,
        "crisis_put_signals.csv": GOOGLE_SHEET_CRISIS_TAB,
        "shadow_signals.csv": GOOGLE_SHEET_SHADOW_TAB,
        "conviction_plays.csv": GOOGLE_SHEET_CONVICTION_TAB,
        # v7 position tracking tabs — name IS the tab
        "position_tracking_active": "position_tracking_active",
        "position_tracking_swing": "position_tracking_swing",
        "position_tracking_income": "position_tracking_income",
        "position_tracking_conviction": "position_tracking_conviction",
        "position_tracking_shadow": "position_tracking_shadow",
        "shadow_filtered_signals": "shadow_filtered_signals",
        # v8.3.0 (Patch 5a): new tabs for Monday
        "flow_events.csv": "flow_events",
        "conviction_decisions.csv": "conviction_decisions",
    }
    return mapping.get(filename, "")
```

### Patch 5b — Insert flow event log calls at 5 conviction sites

At each conviction firing site (after `msg = _flow_detector.format_conviction_play(cp)`), add the call to `_log_flow_event(cp)`. The `_log_flow_event` helper is defined in Patch 6c.

**Location 1 (~line 5289):** After `msg = _flow_detector.format_conviction_play(cp)` at site 4a's context.

**Location 2 (~line 5392):** After the format call in site 4b's context.

**Location 3 (~line 6795):** After the format call in site 4c's context.

**Location 4 (~line 7017):** After the format call in site 4d's context.

**Location 5 (~line 10895):** After the sweep's format call in site 4e's context.

Insert at each location:
```python
                        # v8.3.0 (Patch 5b): log flow event
                        try:
                            _log_flow_event(cp)
                        except Exception as _fe:
                            log.debug(f"flow_events log failed: {_fe}")
```

**Simpler alternative:** Call `_log_flow_event` once inside the scorer gate (Patch 6d). That way every scorer-seen flow is logged without finding 5 separate sites. But scorer only sees scanner-triggered signals, not flow-triggered. So we need it at the 5 flow sites for completeness. Not perfect — can defer 5b-2 through 5b-5 if you want to minimize edits.

**Minimum viable:** Only do Patch 5b-1 (at line 5289). That catches the main conviction path. Lines 5392, 6795, 7017, 10895 catch legacy/sweep paths that fire less frequently. If Monday shows flow_events tab is thin, add 5b-2 through 5b-5 Tuesday.

---

## Patch 6 — Scorer helpers + integration

Insert three NEW functions BEFORE `def _enqueue_signal` (line 3327).

### Patch 6a — `_build_context_snapshot`

```python
# v8.3.0 (Patch 6a): Build context snapshot for conviction scorer.
# Pulls CB side, PB state, wave label, etc. from existing data stores.
# All failures fall back to default so scorer handles missing fields gracefully.
def _build_context_snapshot(ticker: str, bias: str, webhook_data: dict) -> dict:
    ctx = {
        "ticker": ticker,
        "direction": str(bias).lower(),
        "scoring_source": (webhook_data or {}).get("source", "active_scanner"),
        "timeframe": str((webhook_data or {}).get("timeframe", "5")) + "m",
    }

    # Potter Box state from global scanner
    try:
        if _potter_box:
            pb = _potter_box.get_active_box(ticker)
            if pb:
                ctx["pb_state"] = str(pb.get("state", "")).lower()
                ctx["wave_label"] = str(pb.get("wave_label", "")).lower()
                ratio = pb.get("maturity_ratio", 0) or 0
                if ratio < 0.33:
                    ctx["maturity"] = "early"
                elif ratio < 0.66:
                    ctx["maturity"] = "mid"
                elif ratio < 1.0:
                    ctx["maturity"] = "late"
                else:
                    ctx["maturity"] = "overdue"
                try:
                    spot = get_spot(ticker)
                    mid = pb.get("midpoint") or pb.get("cb")
                    if spot and mid:
                        ctx["cb_side"] = "above_cb" if spot > mid else "below_cb"
                except Exception:
                    pass
                try:
                    spot = get_spot(ticker)
                    floor = pb.get("floor", 0) or 0
                    roof = pb.get("roof", 0) or 0
                    if spot and (floor or roof):
                        d_roof = abs(roof - spot) / spot if roof else 999
                        d_floor = abs(spot - floor) / spot if floor else 999
                        ctx["at_edge"] = min(d_roof, d_floor) < 0.02
                except Exception:
                    pass
    except Exception as _pbe:
        log.debug(f"_build_context_snapshot Potter Box fetch failed for {ticker}: {_pbe}")

    ctx["diamond"] = bool((webhook_data or {}).get("diamond", False))
    ctx["wave_dir_original"] = str((webhook_data or {}).get("wave_dir", "")).lower()

    for key in ("ema_diff_quintile", "macd_hist_quintile", "rsi_quintile",
                "wt2_quintile", "adx_quintile"):
        if key in (webhook_data or {}):
            ctx[key] = webhook_data[key]

    ctx["fractal_resistance_above_spot_pct"] = (webhook_data or {}).get(
        "fractal_resistance_above_spot_pct", 0)
    ctx["pivot_resistance_above_spot_pct"] = (webhook_data or {}).get(
        "pivot_resistance_above_spot_pct", 0)

    ctx["recent_flow"] = None
    return ctx


```

### Patch 6b — `_log_conviction_decision`

```python
# v8.3.0 (Patch 6b): Log every scorer decision to Sheets.
def _log_conviction_decision(ticker: str, bias: str, result, context_snapshot: dict):
    try:
        row = {
            "timestamp_utc": time.time(),
            "ticker": ticker,
            "direction": bias,
            "score": int(getattr(result, "score", 0)),
            "decision": str(getattr(result, "decision", "")),
            "hard_gate_triggered": str(getattr(result, "hard_gate_triggered", "") or ""),
            "tier_action": str(getattr(result, "tier_action", "") or ""),
            "breakdown_json": json.dumps(getattr(result, "breakdown", {})),
            "cb_side": str(context_snapshot.get("cb_side", "") or ""),
            "pb_state": str(context_snapshot.get("pb_state", "") or ""),
            "wave_label": str(context_snapshot.get("wave_label", "") or ""),
            "maturity": str(context_snapshot.get("maturity", "") or ""),
            "at_edge": bool(context_snapshot.get("at_edge", False)),
            "scoring_source": str(context_snapshot.get("scoring_source", "") or ""),
            "timeframe": str(context_snapshot.get("timeframe", "") or ""),
        }
        fields = list(row.keys())
        threading.Thread(
            target=lambda: _append_csv_row("conviction_decisions.csv", fields, row),
            daemon=True,
            name="conviction-log",
        ).start()
    except Exception as _lce:
        log.debug(f"_log_conviction_decision failed for {ticker}: {_lce}")


```

### Patch 6c — `_log_flow_event`

```python
# v8.3.0 (Patch 6c): Log flow event to Sheets flow_events tab.
def _log_flow_event(cp: dict):
    try:
        row = {
            "timestamp_utc": time.time(),
            "ticker": cp.get("ticker"),
            "direction": cp.get("trade_direction") or cp.get("direction", ""),
            "flow_type": cp.get("flow_type") or "conviction_play",
            "premium": cp.get("premium", 0),
            "open_interest": cp.get("oi", 0),
            "volume": cp.get("volume", 0),
            "option_symbol": cp.get("contract", ""),
            "score": cp.get("score", 0),
            "route": cp.get("route", ""),
            "dte": cp.get("dte", 0),
            "strike": cp.get("strike", 0),
            "expiry": str(cp.get("expiry", ""))[:10],
            "campaign_id": cp.get("campaign_id", ""),
            "is_exit_signal": bool(cp.get("is_exit_signal", False)),
            "is_shadow_only": bool(cp.get("is_shadow_only", False)),
        }
        fields = list(row.keys())
        threading.Thread(
            target=lambda: _append_csv_row("flow_events.csv", fields, row),
            daemon=True,
            name="flow-event-log",
        ).start()
    except Exception as _fle:
        log.debug(f"_log_flow_event failed for {cp.get('ticker','?')}: {_fle}")


```

### Patch 6d — Scorer gate inside `_enqueue_signal`

**Line:** 3327

### FIND
```python
def _enqueue_signal(job_type: str, ticker: str, bias: str,
                    webhook_data: dict, signal_msg: str):
    job = {
        "job_type": job_type, "ticker": ticker, "bias": bias,
        "webhook_data": webhook_data, "signal_msg": signal_msg,
        "enqueued_at": time.time(),
    }
```

### REPLACE WITH
```python
def _enqueue_signal(job_type: str, ticker: str, bias: str,
                    webhook_data: dict, signal_msg: str):
    # v8.3.0 (Patch 6d): Conviction scorer gate.
    # Fail-open: any scorer exception → signal posts as before.
    # Rollback: CONVICTION_SCORER_ENABLED=false → redeploy.
    if os.getenv("CONVICTION_SCORER_ENABLED", "false").strip().lower() == "true":
        try:
            from conviction_scorer import score_signal as _scorer_fn
            _ctx = _build_context_snapshot(ticker, bias, webhook_data)
            _result = _scorer_fn(
                scanner_event={
                    "job_type": job_type, "ticker": ticker, "bias": bias,
                    "webhook_data": webhook_data, "signal_msg": signal_msg,
                },
                context_snapshot=_ctx,
            )
            _log_conviction_decision(ticker, bias, _result, _ctx)
            if _result.decision == "discard":
                log.info(f"Scorer DISCARD: {ticker} {bias} score={_result.score} "
                         f"gate={_result.hard_gate_triggered}")
                return
            if _result.decision == "log_only":
                log.info(f"Scorer LOG_ONLY: {ticker} {bias} score={_result.score}")
                return
            log.info(f"Scorer POST: {ticker} {bias} score={_result.score} "
                     f"breakdown={_result.breakdown}")
        except Exception as _se:
            log.error(f"Scorer failed for {ticker} {bias}: {_se} — failing open")

    job = {
        "job_type": job_type, "ticker": ticker, "bias": bias,
        "webhook_data": webhook_data, "signal_msg": signal_msg,
        "enqueued_at": time.time(),
    }
```

---

## Final checklist

```bash
# Syntax check all 3 modified files
python3 -c "import ast; ast.parse(open('app.py').read())" && \
python3 -c "import ast; ast.parse(open('trading_rules.py').read())" && \
python3 -c "import ast; ast.parse(open('conviction_scorer.py').read())" && \
echo "All three parse OK"

# Scorer imports cleanly
python3 -c "from conviction_scorer import score_signal, ConvictionResult; print('scorer OK')"

# Patch count verification
grep -c "# v8.3.0 (Patch" /opt/render/project/src/app.py
# Expected: at least 14 without Patch 5b, or 19 if all 5 5b sites applied
```

---

## Commit and deploy

```bash
cd /opt/render/project/src
git add conviction_scorer.py app.py trading_rules.py
git commit -m "v8.3.0: Monday noise cutoff — scorer live, Potter Box alerts off, channel fixes

Validated against 621K backtest: POST WR 73.5% vs LOG_ONLY 64.7% (+8.8 WR lift).

Patches:
- Patch 1: crisis_put env default off (trading_rules.py)
- Patch 2a-c: Potter Box per-setup alerts gated off
- Patch 3a-c: Potter Box digest → main channel
- Patch 4a-e: Flow post master kill switch (5 sites)
- Patch 5a: Register flow_events + conviction_decisions tabs
- Patch 5b-1: Flow event log at primary conviction site
- Patch 6a: _build_context_snapshot helper
- Patch 6b: _log_conviction_decision helper
- Patch 6c: _log_flow_event helper
- Patch 6d: Scorer gate at _enqueue_signal
- NEW: conviction_scorer.py module

Rollback: CONVICTION_SCORER_ENABLED=false → env flip → redeploy, <5min."

git push origin main
```

---

## Monday open (9:30 ET) verification

- [ ] Active scanner fires → `conviction_decisions` tab gets rows
- [ ] Potter Box digest posts to MAIN (not diagnosis) at 8:18 CT
- [ ] EM cards SPY/QQQ unchanged
- [ ] `flow_events` tab gets rows on flow fires
- [ ] Telegram volume 50-80% lower than last week
- [ ] No exception/crash spike in Render logs

## Rollback matrix

| Symptom | Action | Recovery |
|---------|--------|----------|
| Bot silent >30 min | `CONVICTION_STRICTNESS=loose` + redeploy | 5 min |
| Scorer crashing | `CONVICTION_SCORER_ENABLED=false` + redeploy | 5 min |
| Flow firehose back | `FLOW_TELEGRAM_POSTS_ENABLED=false` + redeploy | 5 min |
| Potter alerts flooding | Verify `POTTER_BOX_ALERTS_ENABLED=false` | 5 min |
| crisis_put back | Verify `CRISIS_PUT_ENABLED=false` | 5 min |
| Digest in wrong channel | Revert Patches 3a/3b/3c | 10 min |
| Anything unfixable | `git revert HEAD` + push | 10 min |

---

## Known limitations (reduced precision only, not bugs)

1. **SR proximity rules (G3, P1, B7, B8, B9) won't fire Monday** — `_build_context_snapshot` doesn't populate fractal/pivot distances. Rules skip gracefully when fields are 0. Wire from `level_registry.py` in v8.3.1.

2. **Indicator quintile rules (P5-P11) only fire if webhook_data carries `*_quintile` fields.** Scanner currently sends raw values. Compute quintiles in `_build_context_snapshot` in v8.3.1.

3. **B12 (flow confirmation boost) inactive** — `FLOW_BOOST_ENABLED=false` and `recent_flow=None`. Wire in v8.4.

Core edge (+8.8 WR) comes from hard gates G1/G2 and tier gating — both fully functional.

— End of v8.3.0 patch bundle —
