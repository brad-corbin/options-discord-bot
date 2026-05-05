# CONVICTION_GATE_SPEC v0.6 — MONDAY NOISE CUTOFF

**Release:** v8.3.0 (Sunday evening deploy, live Monday open)
**Author:** Brad + Claude, 2026-04-19
**Replaces:** v0.5 (which was a 3-stage cautious rollout; Brad requires single-shot Monday deploy)

---

## 1. The actual goal

**Make the bot usable again by Monday market open.**

"Usable" means filtered, intelligent alerts — not the firehose that made Brad stop checking Telegram. Also means core digests that work today continue working but in the right channels.

Not a cautious rollout. One-shot deploy Sunday evening. Live Monday open. Rollback = env var flips. No shadow mode (shadow mode has never worked for Brad; killed as a concept).

---

## 2. What's shipping Monday

Eight items. Ordered by blast radius (low → high):

| # | Item | Blast radius | Rollback |
|---|------|--------------|----------|
| 1 | OCC writer bug fix | Low (Sheets log only) | Revert patch |
| 2 | Sheets logging audit | Nil (verify-only) | N/A |
| 3 | crisis_put disable | Nil (pure subtraction) | Env var flip |
| 4 | Potter Box alerts off | Low (alert gating only) | Env var flip |
| 5 | Potter Box digest channel move | Low | Env var flip |
| 6 | Active scanner alert channel routing | Low | Env var check / code revert |
| 7 | Flow post master kill switch | Low (env-gated, default on = current behavior) | Env var flip |
| 8 | conviction_scorer.py live | Medium (gates all scanner posts) | Env var flip |

Everything else deferred. In particular:

- **EM calibration change (§2c from v0.5)** — Tuesday earliest. Material behavior change, deserves its own watch window.
- **GEX accuracy upgrade** — Tuesday earliest. 50 lines of math changes inside `estimate_gex_from_chain`; risks a silent regime-classification bug if the new gamma weighting produces unexpected values.
- **GEX far-chain snapshot + visualizer** — v8.3.2+. Separate project, ~500-600 lines.
- **Scanner internal-filter audit (§2f from v0.5)** — Tuesday. The v0.5 concern that scorer rules overlap with scanner's internal filter stands. Right Monday answer is "loosen scanner to pure signal generator; scorer is authoritative gate." Preliminary reading of `active_scanner.py:_analyze_ticker` and `_scan_ticker` suggests scanner already does hash-based dedup (`_is_deduped`, `_mark_signaled`) and applies `TICKER_RULES` + `is_signal_valid` filters. Scorer layers on TOP of these. For Monday: leave scanner filters as-is; scorer adds new gate; monitor for unexpected silence and relax Tuesday if needed.

---

## 3. Item-by-item with exact repo anchors

All paths relative to repo root (`/options-discord-bot-main/` in the zip Brad uploaded).

### 3.1 — OCC writer bug (§2a from v0.5)

**Claim from project instructions:**
> "All 17 rows in position_tracking_2025-04 write 'P' (put) regardless of actual contract."

**Finding on 2026-04-19:** Hunted through app.py, position_monitor.py, and recommendation_tracker.py looking for hardcoded `'P'` or equivalent. Every occurrence I found derives `option_type` correctly from `trade_side`, `_v7_side`, or `_inc_side`. The bug may have been fixed in a prior patch; the "17 rows all P" symptom may be old data not a current bug.

**Action for Monday:** Before shipping this patch, Brad runs:

```bash
cd /opt/render/project/src
grep -n "option_type" app.py position_monitor.py recommendation_tracker.py | \
  grep -v "option_type=" | head -20
```

If every match is an assignment FROM a derived variable (`_v7_side`, `trade_side`), the bug is already fixed — skip this patch, note in commit message.

If there's a genuine hardcoded `'P'`, patch it to derive from the leg's `option_type` attribute. Commit marker: `# v8.3.0 (Patch 1): OCC writer option_type derived from leg`.

**Verification:** Next CALL position logged shows `option_type = "call"` in `position_tracking_active` tab.

### 3.2 — Sheets logging audit (§2b from v0.5)

**Claim from project instructions:**
> "Already patched in v8.2.3; verify patch present with grep `# v8.2.3 (Patch 2)` app.py."

**Finding:** The grep pattern does not exist in app.py head comments. What DOES exist: `_append_google_sheet_row` (app.py:565-630) is already wrapped in try/except with `log.warning` on failure. This is the de-facto retry guard — not a retry per se, but the silent drop fix. The Apr 7-10 gap was likely a separate issue (network blip? token expiry?) not a missing try/except.

**Action for Monday:** Verify-only. Grep for `_append_google_sheet_row` in app.py, confirm try/except still wraps the Sheets API call. No patch needed. Document in commit message.

**Additionally:** Add `flow_events` as a new CSV/tab mapping at app.py:399-415 (the `_tab_for_filename` function). This is where the `flow_events` tab writer registers.

```python
# app.py, function _tab_for_filename, mapping dict — ADD:
"flow_events.csv": "flow_events",
```

Commit marker on added line: `# v8.3.0 (Patch 2a): flow_events tab mapping`

Then add a writer call. Where? Brad's flow handler fires in three places (app.py:5257, 6791, 7013 in the conviction play detection loop, and at 10885 in the legacy conviction recon path). Each of those calls `_flow_detector.detect_conviction_plays`. After detection, BEFORE the post, add:

```python
# v8.3.0 (Patch 2b): log every flow event to flow_events.csv / Sheets
try:
    from time import time as _t
    _flow_event_row = {
        "timestamp_utc": _t(),
        "ticker": cp.get("ticker"),
        "direction": cp.get("trade_direction") or cp.get("direction"),
        "flow_type": cp.get("flow_type") or "conviction_play",
        "premium": cp.get("premium"),
        "open_interest": cp.get("oi"),
        "volume": cp.get("volume"),
        "option_symbol": cp.get("contract"),
        "score": cp.get("score", 0),
        "route": cp.get("route"),
        "scanner_fire_within_10m": False,  # filled by correlator later
        "scanner_fire_within_30m": False,
        "campaign_id_matched": cp.get("campaign_id"),
    }
    _fe_fields = list(_flow_event_row.keys())
    _append_csv_row("flow_events.csv", _fe_fields, _flow_event_row)
except Exception as _fe_err:
    log.debug(f"flow_events log failed: {_fe_err}")
```

Place this AFTER `_flow_detector.detect_conviction_plays(...)` returns and BEFORE any Telegram post. 4 places. Each gets its own `# v8.3.0 (Patch 2b)` marker.

**Syntax check:** `python3 -c "import ast; ast.parse(open('app.py').read())"` after each insertion.

**Verification:** After Monday deploy, Sheets tab `flow_events` should receive rows on every flow-detected conviction play. Daily row count should be > 10 on active-flow days.

### 3.3 — crisis_put disable (§2d from v0.5)

**Claim:** "2W/13L, −$7,452 over study period."

**Action:** Set environment variable default.

```bash
# Render dashboard — Environment tab
CRISIS_PUT_ENABLED=false
```

Repo change: find where `CRISIS_PUT_ENABLED` is read and ensure default is `false`.

```bash
cd /opt/render/project/src
grep -n "CRISIS_PUT_ENABLED" app.py
```

Expected current code: `os.getenv("CRISIS_PUT_ENABLED", "true")` or similar. Change to `"false"`.

Commit marker: `# v8.3.0 (Patch 3): crisis_put default off`.

**Verification:** On next CRISIS condition trigger, no Telegram post. Log should show "crisis_put disabled via env".

### 3.4 — Potter Box alerts off (NEW in v0.6)

**Rationale:** Brad hasn't validated any Potter Box alert type has edge (the `analyze_alert_edges_v1.py` analyzer was built and parked). The TSLA 23-alert-a-day bug was a symptom. Correct Monday answer: alerts off entirely. Digest stays on (validated via use).

**Affected alert types** (from `potter_box.py:198-320`):
- `detect_cb_gap_signals` (cb_gap_bullish, cb_gap_bearish)
- `detect_gap_outside` (gap_above_box, gap_below_box)
- `detect_punchback` (punchback_bullish, punchback_bearish)
- `detect_supply_demand_zones` (zone_demand_*, zone_supply_*)
- Break-confirmed alerts (if separate post path exists)

**Approach:** Add four env var gates. Each default off:

```bash
POTTER_BOX_BREAKOUT_ALERTS_ENABLED=false
POTTER_BOX_GAP_ALERTS_ENABLED=false
POTTER_BOX_PUNCHBACK_ALERTS_ENABLED=false
POTTER_BOX_ZONE_ALERTS_ENABLED=false
```

In app.py, find every Telegram post path that originates from a Potter Box alert type. Wrap each with an env check:

```python
# v8.3.0 (Patch 4a): gate Potter Box breakout alerts
if os.getenv("POTTER_BOX_BREAKOUT_ALERTS_ENABLED", "false").lower() != "true":
    log.debug(f"Potter Box breakout alert suppressed: {ticker}")
    return  # or 'continue' depending on context
```

**Before patching:** Brad runs to find the Telegram post calls for each alert type:

```bash
cd /opt/render/project/src
grep -n "cb_gap\|gap_outside\|punchback\|supply_demand\|breakdown_confirmed\|BREAKOUT_CONFIRMED" app.py | head -30
```

If fewer than 4 call sites exist, merge the gates. If more, add a gate per site.

Commit markers: `# v8.3.0 (Patch 4a-d): gate <alert_type> alerts`.

**Verification:** No Potter Box alert posts in Telegram on Monday. Digest still fires pre-market.

### 3.5 — Potter Box digest channel move

**Current state (per Brad):** Digest posts to diagnosis channel. Should post to main.

**Finding:** Without grepping the exact digest post call, the pattern is some variant of:

```python
_tg_rate_limited_post(digest_msg, chat_id=DIAGNOSTIC_CHAT_ID)
```

**Before patching:** Brad runs:

```bash
cd /opt/render/project/src
grep -n "potter.*digest\|digest.*potter\|PotterBox digest\|Potter Box digest\|_potter_digest" app.py | head -10
```

At the post call identified, change `chat_id=DIAGNOSTIC_CHAT_ID` (or equivalent) to `chat_id=TELEGRAM_CHAT_ID`.

Commit marker: `# v8.3.0 (Patch 5): Potter Box digest → main channel`.

**Verification:** First pre-market digest Monday posts to main channel, not diagnosis.

### 3.6 — Active scanner channel routing

**Current state (from repo reading):** Active scanner alerts fire via `_enqueue_signal` (app.py:3327) which pushes to Redis. A consumer worker processes the queue, does pre-chain gate validation, then posts via `_tg_rate_limited_post`. The post call is somewhere in the consumer.

Current routing at app.py:5361, 5462, 6811, 7033, 10905 shows the pattern:

```python
if TELEGRAM_CHAT_INTRADAY and TELEGRAM_CHAT_INTRADAY != TELEGRAM_CHAT_ID:
    _tg_rate_limited_post(msg, chat_id=TELEGRAM_CHAT_INTRADAY)
```

**Interpretation:** This already cross-posts to intraday (Alpha SPY Omega) channel for conviction plays. Active scanner T1/T2 alerts may or may not follow the same pattern.

**Before patching:** Brad runs:

```bash
cd /opt/render/project/src
grep -n "tier.*T1\|tier.*T2\|scanner.*alert.*post\|active_scanner.*tg_rate" app.py | head -10
```

Identify the scanner alert post site. Confirm it already posts to main. If it does NOT also cross-post to intraday, add the pattern:

```python
# v8.3.0 (Patch 6): active scanner → main + Alpha SPY Omega
_tg_rate_limited_post(scanner_msg, chat_id=TELEGRAM_CHAT_ID)
if TELEGRAM_CHAT_INTRADAY and TELEGRAM_CHAT_INTRADAY != TELEGRAM_CHAT_ID:
    _tg_rate_limited_post(scanner_msg, chat_id=TELEGRAM_CHAT_INTRADAY)
```

Commit marker: `# v8.3.0 (Patch 6): active scanner to both main and intraday`.

**Env check:** `TELEGRAM_CHAT_INTRADAY` already set per current env. Verify with `/env` command or Render dashboard.

**Verification:** First Monday scanner T1/T2 alert fires to both main and Alpha SPY Omega.

### 3.7 — Flow post master kill switch

**Rationale:** Brad wants the option to turn off ALL flow Telegram posts instantly. Not deciding Sunday or Monday — wants the env var ready.

**Action:** Add env var check to every flow post site:

```python
# v8.3.0 (Patch 7): flow post master kill switch
if os.getenv("FLOW_TELEGRAM_POSTS_ENABLED", "true").lower() != "true":
    log.debug(f"Flow post suppressed by FLOW_TELEGRAM_POSTS_ENABLED=false: {ticker}")
    # Still log to flow_events (Patch 2b). Only suppresses Telegram.
    return  # or 'continue' depending on context
```

Default: **true** (current behavior). Brad flips to `false` at any time, Render redeploy, flow posts stop. Sheets `flow_events` log keeps populating either way.

**Before patching:** Brad runs:

```bash
cd /opt/render/project/src
grep -n "_flow_detector.detect_conviction_plays\|format_conviction_play\|FLOW CONVICTION" app.py | head -10
```

The 4 firing points at app.py:5257, 6791, 7013, 10885 each need the gate.

Commit markers: `# v8.3.0 (Patch 7a-d): flow post kill switch`.

**Verification:** With `FLOW_TELEGRAM_POSTS_ENABLED=true` (default), flow posts continue. Flipping to `false` → next flow event logs to Sheets but no Telegram.

### 3.8 — conviction_scorer.py live

**This is the big one.** Full module, ~360 lines. Spec lives in a separate doc `CONVICTION_SCORER_MODULE.md` that I'll write next after this spec is locked.

**Integration point:** Scanner handler's `_enqueue_signal` at app.py:3327. This is where EVERY scanner alert lands before being pushed to Redis for the worker to post. The scorer call happens BEFORE the enqueue:

```python
# v8.3.0 (Patch 8): conviction_scorer gate
if os.getenv("CONVICTION_SCORER_ENABLED", "false").lower() == "true":
    try:
        from conviction_scorer import score_signal, ConvictionResult
        result = score_signal(
            scanner_event={"job_type": job_type, "ticker": ticker, "bias": bias,
                           "webhook_data": webhook_data, "signal_msg": signal_msg},
            context_snapshot=_build_context_snapshot(ticker, bias, webhook_data),
        )
        # Log every decision
        _log_conviction_decision(ticker, bias, result)
        if result.decision == "discard":
            log.info(f"Scorer DISCARD: {ticker} {bias} score={result.score} "
                     f"gate={result.hard_gate_triggered}")
            return  # signal dies here, no Redis push, no Telegram
        if result.decision == "log_only":
            log.info(f"Scorer LOG_ONLY: {ticker} {bias} score={result.score}")
            return  # signal logged to conviction_decisions, not posted
        # decision == "post" → fall through to existing enqueue
    except Exception as _se:
        log.error(f"Scorer failed for {ticker}: {_se}, failing open (will post)")
        # fail-open: any scorer exception → post as before
```

Placement: at the TOP of `_enqueue_signal`, immediately after the existing `job = {...}` construction at app.py:3329-3333.

Dependencies:
- `_build_context_snapshot(ticker, bias, webhook_data)` — NEW helper function that pulls CB side, PB state, wave label, etc. from existing `webhook_data` + a fresh read from `thesis_monitor` / `potter_box`. ~50 lines. Lives in app.py.
- `_log_conviction_decision(ticker, bias, result)` — NEW helper, writes to Sheets tab `conviction_decisions`. ~30 lines. Lives in app.py.
- `conviction_scorer.py` module — ~360 lines, separate file, top-level in repo.

**Env vars (default):**
```bash
CONVICTION_SCORER_ENABLED=true   # master switch ON for Monday
CONVICTION_POST_THRESHOLD=75     # overridable via /confidence
CONVICTION_LOG_THRESHOLD=60      # fixed floor
CONVICTION_STRICTNESS=medium     # medium|loose|tight per §6 in v0.5
FLOW_BOOST_ENABLED=false         # B12 inactive (no flow data yet)
```

**Rollback:** `CONVICTION_SCORER_ENABLED=false` → redeploy → scorer bypassed, scanner posts flow through unchanged. <5 min.

Commit markers: `# v8.3.0 (Patch 8a): scorer gate at enqueue` / `(Patch 8b): context snapshot` / `(Patch 8c): decision logger`.

**Verification Monday:**
- First scanner signal generates a `conviction_decisions` row in Sheets with `score`, `decision`, `breakdown` fields.
- Telegram volume drops by 50%+ compared to last week (Brad eyeballs).
- If Telegram goes silent entirely for >1 hour during market hours, rollback (scorer too strict or buggy).

---

## 4. Deploy runbook

### Sunday evening (target: 6pm-10pm ET)

#### Pre-flight checklist (30 min)

- [ ] Confirm NOT deploying Friday (project rule). Sunday deploy → Monday open is fine.
- [ ] Branch: `git checkout -b v8.3.0-monday-noise-cutoff`
- [ ] Pull latest main, rebase.
- [ ] Backtest cache intact on Render `/var/backtest/` (verify with `ls /var/backtest/` — should show `trades.csv`, `summary_*.csv`).
- [ ] Position tracker state saved (`persistent_state.py` flushes on SIGTERM; Render handles this during redeploy).

#### Patch sequence (estimated 1-2 hours)

Apply patches in order. After EACH patch, run:

```bash
python3 -c "import ast; ast.parse(open('app.py').read())"
python3 -c "import ast; ast.parse(open('conviction_scorer.py').read())"  # after Patch 8
```

**If any syntax check fails, STOP and diagnose before continuing.** Do not ship a file that hasn't parsed clean.

1. **Patch 1** — OCC writer (audit + patch if needed)
2. **Patch 2a** — flow_events tab registration
3. **Patch 2b** — flow_events writers (4 sites)
4. **Patch 3** — crisis_put env default
5. **Patch 4a-d** — Potter Box alert gates
6. **Patch 5** — Potter Box digest channel
7. **Patch 6** — active scanner cross-post to intraday
8. **Patch 7a-d** — flow post kill switch
9. **Patch 8a** — scorer integration at `_enqueue_signal`
10. **Patch 8b** — `_build_context_snapshot` helper
11. **Patch 8c** — `_log_conviction_decision` helper
12. **conviction_scorer.py** — NEW file, complete

#### Render environment variables to set BEFORE push

```bash
# Set these first in Render dashboard Environment tab
CRISIS_PUT_ENABLED=false
POTTER_BOX_BREAKOUT_ALERTS_ENABLED=false
POTTER_BOX_GAP_ALERTS_ENABLED=false
POTTER_BOX_PUNCHBACK_ALERTS_ENABLED=false
POTTER_BOX_ZONE_ALERTS_ENABLED=false
FLOW_TELEGRAM_POSTS_ENABLED=true
CONVICTION_SCORER_ENABLED=true
CONVICTION_POST_THRESHOLD=75
CONVICTION_LOG_THRESHOLD=60
CONVICTION_STRICTNESS=medium
FLOW_BOOST_ENABLED=false

# Verify existing vars still set:
TELEGRAM_BOT_TOKEN     # must be present
TELEGRAM_CHAT_ID       # main channel
TELEGRAM_CHAT_INTRADAY # Alpha SPY Omega channel
```

#### Commit, push, deploy

```bash
git add -A
git commit -m "v8.3.0: Monday noise cutoff — Potter Box alerts off, scorer live, channel routing

See CONVICTION_GATE_SPEC_v0_6.md for full scope.

Patches applied:
- (1) OCC writer audit
- (2a, 2b) flow_events logging
- (3) crisis_put default off
- (4a-d) Potter Box alert gates
- (5) Potter Box digest to main channel
- (6) active scanner cross-post intraday
- (7a-d) flow post master kill switch
- (8a-c) conviction_scorer integration
- conviction_scorer.py — NEW module"
git push origin v8.3.0-monday-noise-cutoff
```

Open PR, merge to main. Render auto-deploys.

#### Post-deploy verification (30 min)

- [ ] Bot reconnects Telegram (watch Render deploy log for "Telegram bot initialized")
- [ ] `/status` endpoint returns 200
- [ ] `/signals_today` returns reasonable structure (not an error)
- [ ] Sheets `conviction_decisions` tab exists with headers (will populate at first Monday scan)
- [ ] Sheets `flow_events` tab exists with headers
- [ ] No ERROR-level log spike in Render logs
- [ ] Test signal if possible (any synthetic or manual)

### Monday open (9:30 ET)

Watch for 30 min.

- [ ] Active scanner fires on expected tickers during first 15 min
- [ ] Scorer decisions logged to `conviction_decisions` (every scanner signal → 1 row)
- [ ] Telegram volume is ≤ half what it was last week
- [ ] Digests fired to main channel (Potter Box pre-market)
- [ ] EM cards SPY/QQQ posted to main (unchanged)
- [ ] No crash / restart / exception spike

### Rollback matrix

| Symptom | Action | Time to recover |
|---------|--------|----------------|
| Bot not posting anything | `CONVICTION_SCORER_ENABLED=false`, redeploy | 5 min |
| Scorer crashing (ERROR spike) | `CONVICTION_SCORER_ENABLED=false`, redeploy | 5 min |
| Flow firehose resumed | `FLOW_TELEGRAM_POSTS_ENABLED=false`, redeploy | 5 min |
| Potter Box alerts accidentally on | Verify all 4 env vars set to `false`, redeploy | 5 min |
| Scorer too strict (Telegram silent) | `CONVICTION_STRICTNESS=loose`, redeploy | 5 min |
| Digest in wrong channel | Revert Patch 5 | 10 min |
| Scanner not cross-posting intraday | Check `TELEGRAM_CHAT_INTRADAY` env, revert Patch 6 if needed | 10 min |
| Any other crash | `git revert <commit>`, push, redeploy | 15 min |

---

## 5. What's NOT in v0.6

| Item | Why deferred | When |
|------|-------------|------|
| EM calibration PT bands | Material behavior change; needs watch window | v8.3.1 Tuesday |
| GEX accuracy (BS gamma in `estimate_gex_from_chain`) | 50 lines of math, risks silent regime bug | v8.3.1 Tuesday |
| GEX far-chain snapshot + visualizer | 500-600 lines, separate project | v8.3.2+ |
| Alert-type edge analyzer (`analyze_alert_edges_v1.py`) | Parked; Potter Box alerts off anyway | v8.3.3+ |
| Scanner filter audit doc | Concern stands, addressed next week | v8.3.1 Tuesday |
| Ticker tier gating enforcement | Part of scorer (Patch 8), but "deferred flow promotion" framework is future | Included in Monday scorer |
| Credit spread post-formatter (Q5 from v0.5) | Nice-to-have; scorer base must work first | v8.3.2+ |
| Shadow mode | Killed; Brad doesn't use it | Never |

---

## 6. Version notes

**v0.6 (this doc):**
- Rewrote entirely around Brad's correction that Monday goal is usability, not cautious rollout
- Killed shadow mode per Brad
- Turned off Potter Box alerts entirely (alert-edge analyzer parked; no validated edge)
- Moved Potter Box digest from diagnosis → main channel
- Added active scanner cross-post to Alpha SPY Omega (intraday)
- Added flow post master kill switch (default on, Brad flips when ready)
- Scorer ships live Monday, not shadow-mode
- Dropped 3-stage rollout; single-shot deploy
- All repo anchors verified against actual `options-discord-bot-main.zip` uploaded 2026-04-19

**v0.5 (replaced):**
- 3-stage cautious rollout (v8.3.0 fixes → v8.3.1 shadow → v8.3.2 live)
- Included §2f scanner filter audit as pre-deploy deliverable (retained as Tuesday work in v0.6)
- Correctly identified all scoring rule calibrations (§4 rules from v0.4 retained)

**v0.4 (replaced):**
- Added B3=+3, P2/P3 direction split, P15, B6 split, B13, ORCL→Tier-3
- All rule calibrations retained in v0.6

**v0.3 (replaced):**
- Demoted flow from trigger to confirming layer (retained in v0.6)

**v0.2, v0.1 (replaced):**
- Flow-first framing, rejected

---

## 7. Reference: scorer rules (carried forward from v0.5)

For `conviction_scorer.py` implementation, the complete rule set is:

**Hard gates (§4a):** G1 CB misalignment, G2 no_box + bear, G3 bear far below resistance

**Heavy penalties (§4b):** P1 (−8), P2a/P2b (bull/bear established pinescript split), P3a/P3b (bull/bear established active_scanner split), P4 (XLE bear active_scanner), P15 (bull + late maturity: −3)

**Moderate penalties (§4c):** P5-P14 per v0.4 table

**Moderate boosts (§4d):** B1 (+3), B2 (+3), B3 (+3), B4 (+2 pinescript-only), B5 (+2 Tier-2 + at_edge), B6a (bear+late+active: +4), B6b (bear+late+pinescript: +2), B7 (+3), B12 (+2, default off), B13 (post_box + bull + active: +3)

**Heavy boosts (§4e):** B8-B11 per v0.4 table

**Cap rules (§4f):** B10+B11 at +5; P5+P6 at −6; P7+P8 at −6

**Base score:** 65. Floor 0, ceiling 100.

**Tiers (§5):**
- Tier-1: SPY, XLF, DIA, GLD, XLV, JPM (bull), QQQ, GOOGL (bull), CAT, XLE, GS, TLT, SOXX, IWM
- Tier-2 (require at_edge OR diamond): MSFT, AMZN, NVDA, NFLX, META, UNH, CRM, LLY
- Tier-3 (never post debit): ARM, BA, COIN, MRNA, MSTR, PLTR, SMCI, SOFI, TSLA, AMD, ORCL
- Bear debit candidates: TLT, DIA, XLV, AAPL, IWM, SPY, MSFT, XLF, UNH, QQQ, XLE. GOOGL/JPM bear: P13 penalty.

Full rule detail in separate `CONVICTION_SCORER_MODULE.md` doc that pairs with `conviction_scorer.py` build.

---

## 8. Brad's sign-off checklist before build begins

- [ ] All eight Monday items are the right items
- [ ] Repo anchors look correct (Brad verifies with quick greps)
- [ ] Env var names don't collide with anything existing (Brad checks Render env)
- [ ] Sunday evening deploy window works
- [ ] Rollback matrix covers Brad's worry scenarios
- [ ] Nothing in §5 "what's NOT in v0.6" should be bumped into v0.6

Once signed off, I build `conviction_scorer.py` + `_build_context_snapshot` + `_log_conviction_decision` + the Sheets tab writers. Target: all code delivered tonight, Brad applies patches Sunday evening, live Monday.

— Not financial advice —
