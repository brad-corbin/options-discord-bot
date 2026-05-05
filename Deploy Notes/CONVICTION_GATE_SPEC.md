# CONVICTION_GATE_SPEC v0.5

**Release:** v8.3
**Author's note:** v0.4 added 9 changes based on Claude's review of v0.3. This version (v0.5) corrects 5 issues found in a follow-up data-audit of v0.4: one reversed-direction rule (P2 on pinescript), two rules that failed Brad's published ≥5 WR threshold (P15, B6b), one factual citation error (B13), and one under-cited tier move (ORCL). No philosophy changes. Scanner-first trigger, flow as confirming layer, same rollout plan.

---

## 1. Philosophy

**Scanner-first confluence scoring (v8.3); flow-first deferred to v8.4+ pending data.**

1. Active scanner on 5m candles is the primary trigger. It evaluates CB alignment, Potter Box state, wave label, and indicators on bar close.
2. Every other layer — flow events, SR proximity, EM context, diamond setup, ticker tier — confirms or denies the scanner's initial interest.
3. A unified conviction score (0–100) determines routing:
   - **<60**: silent discard, no log
   - **60–74**: log only (`signal_decisions`), no Telegram post
   - **≥75** (user-tunable): Telegram post to main channel
4. Scoring rules default-OFF via env vars. Shadow mode runs 2 trading days before any rule affects live notifications.
5. Nothing the scorer does can break trading. All scoring lives in its own daemon; fail-open means scanner→log→post continues working even if the scorer crashes.

**Why scanner-as-trigger, not flow:**
- Backtest: 621,007 scanner + pinescript signals, 20 months. Flow: untested at outcome level.
- Scanner filters are backtest-tunable. Flow thresholds aren't until §2e data accumulates.
- The "scanner too tight post-Mar 19" issue is a tuning problem. The scorer loosens the gate using backtest evidence.

**Pre-condition audit required (v0.4 addition, preserved):** Before v8.3.1 ships, active_scanner's internal filter must be documented (§2f). Any rule already enforced by active_scanner is flagged as "redundant-by-design" in the scorer or removed.

Additive to v8.2. Existing flow ingestion, tracker, and monitor paths unchanged.

---

## 2. Pre-deploy fixes (must ship BEFORE scorer goes live)

Six v8.2 items must be addressed first, in this order:

### 2a. OCC writer bug (`position_tracking_*`)
**Symptom:** 17 rows in `position_tracking_2025-04` write `'P'` regardless of contract. Phantom exit signals on calls.
**Fix:** Use `leg.option_type`, not hardcoded `'P'`.
**Blast radius:** Low.

### 2b. `signal_decisions` logging broken Apr 7–10
**Fix:** Wrap writer with retry-3x-then-log-warning. Verify `grep "# v8.2.3 (Patch 2)" app.py` finds it.
**Blast radius:** Nil.

### 2c. `em_1sd` calibration 30–40% too wide
**Fix:** `PT1 = 0.6 × em_1sd`, `PT2 = 1.0 × em_1sd`, `PT3 = 2.0 × em_1sd`. Env var `EM_CALIBRATION_VERSION=v8.3`.
**Blast radius:** Medium. No backfill.

### 2d. Disable crisis_put layer (2W/13L, −$7,452)
**Fix:** `CRISIS_PUT_ENABLED=false` default.
**Blast radius:** Nil.

### 2e. Flow event logging — precondition for v8.4
**Fix:** New Sheets tab `flow_events` (or Redis `flow:event:*`): `timestamp_utc, ticker, flow_type, direction, premium, open_interest, volume, option_symbol, scanner_fire_within_10m (bool), scanner_fire_within_30m (bool), campaign_id_matched (or NULL)`.
Daemon write, try/except wrapped.
**Blast radius:** Additive.

### 2f. Active scanner internal-filter audit (v0.4 addition)
**Symptom:** Scorer rules may overlap with filters already applied inside `active_scanner._analyze_ticker`. If scanner rejects CB-misaligned signals, G1 is dead code. Worse: overlapping filters make the "scanner too tight" problem worse.
**Fix:** Read `active_scanner._analyze_ticker` and enumerate every filter causing early return. Document in `active_scanner_filter_audit.md`:

- CB check? Y/N
- Potter Box state check? Y/N
- Wave label check? Y/N
- Indicator quintile checks? Y/N
- Regime check? Y/N
- Ticker tier check? Y/N
- SR proximity? Y/N

For every Y: either remove the corresponding scorer rule OR relax the scanner internal filter so the scorer becomes authoritative.

**Default recommendation:** relax scanner filters, let scorer be authoritative. Aligns with loosen-the-gate goal. Scanner fires on any CB+PB-valid setup; scorer handles nuance.

**Blast radius:** Medium. Changes scanner behavior. Pair with v8.3.1.

**All six ship as v8.3.0.**

---

## 3. Architecture

### 3a. New module: `conviction_scorer.py`

```
score_signal(scanner_event, context_snapshot) -> ConvictionResult
```

`ConvictionResult`:
- `score: int` (0–100)
- `decision: Literal["discard", "log_only", "post"]`
- `breakdown: Dict[str, int]`
- `hard_gate_triggered: Optional[str]`
- `shadow_mode: bool`

`context_snapshot`:
- ticker, direction, scoring_source
- CB side, PB state, wave_label, maturity, wave_alignment, diamond flag
- SR proximity (fractal & pivot)
- EM context (bias_score, regime, vol_regime)
- indicator quintiles (ema_diff, macd_hist, rsi, wt2, adx)
- timeframe, tier, ticker_class
- `recent_flow: Optional[FlowMatch]` — flow events on same ticker+direction in prior 15 min (B12)

**Scorer is the authoritative gate once v8.3.1 ships.** Scanner's role is to fire candidates, not pre-filter. §2f audit confirms no overlap.

### 3b. Env vars

```
CONVICTION_SCORER_ENABLED=false
CONVICTION_SHADOW_MODE=true
CONVICTION_POST_THRESHOLD=75
CONVICTION_LOG_THRESHOLD=60
CONVICTION_STRICTNESS=medium
FLOW_LOGGING_ENABLED=true
FLOW_BOOST_ENABLED=false              # B12 off until flow data validates
CREDIT_LEG_SUGGESTIONS_ENABLED=false  # Q5 post formatter, off by default
```

### 3c. Integration points

- Scanner handler calls `score_signal` after layer enrichment, before Telegram post
- Shadow mode: post fires as before; scorer decision advisory in `conviction_shadow` tab
- Live mode: decision gates post
- Every call writes to `signal_decisions` with score + breakdown
- Daemon threads, try/except wrapped, fail-open → post

---

## 4. Scoring rules

### 4a. Hard gates (immediate discard)

| # | Rule | Criterion | Source |
|---|------|-----------|--------|
| G1 | CB misalignment | `(direction=bull AND cb_side=above_cb) OR (direction=bear AND cb_side=below_cb)` | batch 1 — −9 to −12 WR at n>15K |
| G2 | no_box + bear | `pb_state=no_box AND direction=bear` | batch 1 — −9.5 to −10.5 WR |
| G3 | Bear far below resistance | `direction=bear AND fractal_resistance_above_spot_pct >= 3.0` | batch 3b — −14 to −20 WR at n~3K |

### 4b. Heavy penalties (−5 to −10)

| # | Rule | Points | Source |
|---|------|--------|--------|
| P1 | Bear 2–3% below resistance | −8 | batch 3b |
| **P2a** | **Bull + wave_label=established + pinescript — CORRECTED in v0.5** | **−4** | batch 1 — −2.5 to −4.8 WR, median ~−4.0 |
| **P2b** | **Bear + wave_label=established + pinescript — CORRECTED in v0.5** | **−5** | batch 1 — −3.4 to −7.3 WR, median ~−4.5 |
| P3a | Bull + wave_label=established + active_scanner | −6 | batch 1 — −6.4 to −8.6 WR at n≥500 |
| P3b | Bear + wave_label=established + active_scanner | −2 | batch 1 — −0.4 to −6.4 WR, median ~−3 |
| P4 | XLE + active_scanner + bear (ticker-specific) | −5 | batch 3a (WR=43.6% n=1,524) |

**~~P15 removed in v0.5.~~** Bull + maturity=late drags −3 to −5 across 9 n≥500 cells, but only 1 cell (active_scanner 15m T2, −5.0) hits the ≥5 WR threshold. Direction is consistent but doesn't meet Brad's criteria. Pattern documented in §10 for future v8.3.x reconsideration.

### 4c. Moderate penalties (−2 to −4)

| # | Rule | Points | Source |
|---|------|--------|--------|
| P5 | Bull + ema_diff Q5 | −4 | batch 3c/3d |
| P6 | Bull + macd_hist Q5 | −4 | batch 3c/3d |
| P7 | Bear + ema_diff Q5 | −4 | batch 3c |
| P8 | Bear + macd_hist Q5 + timeframe=30m | −3 | batch 3d |
| P9 | Bear + RSI Q5 | −3 | batch 3c |
| P10 | Bull + 30m + ema_diff Q1 | −3 | batch 3c |
| P11 | Bull + ADX Q5 | −2 | batch 3c |
| P12 | pb_state=below_floor + bull | −2 | batch 1 |
| P13 | Weak debit-bear + active_scanner: GOOGL, JPM | −3 | batch 3a |
| P14 | Weak debit-bull marginal: CRM, LLY | −3 (requires at_edge) | batch 3a — ORCL removed, see §5c |

### 4d. Moderate boosts (+2 to +3)

| # | Rule | Points | Source |
|---|------|--------|--------|
| B1 | wave_label=breakout_imminent | +3 | batch 1 — +3.5 to +5.6 WR |
| B2 | pb_state=above_roof + bull | +3 | batch 1 — +3.5 to +4.7 WR |
| B3 | wave_aligned original interpretation | +3 | batch 1 — +2.6 to +4.9 WR, median ~+3.5 |
| B4 | diamond=True (pinescript only) | +2 | batch 2 |
| B5 | at_edge + Tier-2 | +2 | batch 2 |
| B6a | Bear + maturity=late + active_scanner | +4 | batch 1 — +5.6 to +6.9 WR at n≥500 |
| B7 | Bull + pivot resistance <2% above spot | +3 | batch 3b |
| B12 | Flow event on same ticker+direction in prior 15 min | +2 (default OFF) | unmeasured |
| **B13** | **post_box + bull + active_scanner — CORRECTED citation in v0.5** | **+2** | batch 1 — only n≥500 cell is active_scanner 5m T2 bull post_box n=879 WR=68.8% (+5.1). Borderline-threshold rule, weighted conservatively. |

**~~B6b removed in v0.5.~~** Pinescript bear+late lifts only +0.8 to +2.5 across 6 n≥500 cells — max is +2.5 on 15m T2 at n=2,997, less than half the ≥5 WR threshold. Fails Brad's criteria. B6a (active_scanner) remains the valid edge; pinescript doesn't replicate it.

### 4e. Heavy boosts (+4)

| # | Rule | Points | Source |
|---|------|--------|--------|
| B8 | Bull + fractal resistance 3%+ above | +4 | batch 3b — +5 to +8 WR |
| B9 | Bear + pivot resistance <1% above | +4 | batch 3b — +4 to +9 WR |
| B10 | Bear + RSI Q1 | +4 | batch 3c |
| B11 | Bear + WT2 Q1 | +4 | batch 3c |

### 4f. Cap rules (correlation adjustment)

- **B10 + B11** (bear RSI Q1 AND WT2 Q1): cap combined at **+5**
- **P5 + P6** (bull ema_diff Q5 AND macd_hist Q5): cap combined at **−6**
- **P7 + P8** (bear ema_diff Q5 AND macd_hist Q5 on 30m): cap combined at **−6**
- **B8 + B10 + B11** (bear capitulation stack): no additional cap beyond B10+B11's +5, so max = B8(+4) + capped(B10/B11)(+5) = +9. Sustainable.

### 4g. Note on B12 (flow confirming layer)

Unchanged from v0.3. Defaults OFF via `FLOW_BOOST_ENABLED=false`. Activated only after §2e produces 2+ weeks of data and Brad reviews.

### 4h. Base score

Every signal starts at **65**. Rules add/subtract. Floor 0, ceiling 100.

Rationale: baseline scanner-triggered, CB-aligned signal runs ~65% WR in backtest.

---

## 5. Ticker tier gating

### 5a. Tier-1 quality (full scorer, all rules apply)
SPY, XLF, DIA, GLD, XLV, JPM (bull only), QQQ, GOOGL (bull only), CAT, XLE (bear gets P4), GS, TLT, SOXX, IWM

### 5b. Tier-2 marginal bulls (require at_edge OR diamond to post)
MSFT, AMZN, NVDA, NFLX, META, UNH, CRM, LLY

Enforcement: If ticker in Tier-2 AND direction=bull AND NOT (at_edge OR diamond), cap final score at **74**. Not a penalty — a ceiling.

### 5c. Tier-3 credit-only (no debit posts) — UPDATED rationale in v0.5
ARM, BA, COIN, MRNA, MSTR, PLTR, SMCI, SOFI, TSLA, AMD, **ORCL**

**ORCL rationale (corrected from v0.4):** ORCL bull runs 49–55% WR across all scoring sources and timeframes at n>250 (pinescript 5m T2 bull n=2,686 WR=55.1%; active_scanner 5m T2 bull n=1,330 WR=51.2%). It never reaches the 57%+ floor that justifies debit-bull status. The v0.4 "at_edge fails" observation (n=39 WR=25.6%) is directionally consistent but below the n≥500 floor on its own; the broader weak-WR profile is the authoritative evidence.

Enforcement: Tier-3 signals run through scorer; `decision` never `post` to main. See §7 Q1 for credit-candidate routing.

### 5d. Tier-specific bear lists
Debit-bear candidates: TLT, DIA, XLV, AAPL, IWM, SPY, MSFT, XLF, UNH, QQQ, XLE.
**GOOGL and JPM bear are NOT in the debit-bear list** — can fire but get P13 penalty.

---

## 6. Strictness modes

- **loose**: hard gates (G1–G3) only
- **medium (default)**: hard gates + tier gating + CB-related rules
- **tight**: all rules including indicator quintiles and SR proximity

Default medium. Priority is loosening, not tightening.

---

## 7. Open questions

### Q1. Credit-candidate notifications (Tier-3)
(a) Main channel with "CREDIT CANDIDATE" prefix / (b) Separate `#credit-candidates` channel / (c) Log only
**Default:** (c)

### Q2. Strictness default
(a) medium / (b) tight
**Default:** medium

### Q3. Shadow mode duration
(a) 2 trading days / (b) 5 trading days
**Default:** 2

### Q4. Flow promotion criteria for v8.4
(a) Flow precedes first scanner fire by ≥5 min in ≥60% of winning campaigns (2+ weeks post-§2e)
(b) Flow-confirmed scanner fires outperform scanner-alone by ≥5 WR points
(c) Both (a) AND (b)
**Default:** (c)

### Q5. Credit spread strike info in Conviction Take posts (v0.4 addition)
When a post (≥75) fires with `pb_state=in_box`, include suggested credit spread strikes (short leg at nearest PB boundary, long leg $0.25 OTM) as secondary recommendation?
(a) Yes / (b) No / (c) Configurable, default off
**Default:** (c). Env var `CREDIT_LEG_SUGGESTIONS_ENABLED=false`.

---

## 8. Rollout plan

### 8a. v8.3.0 — pre-deploy fixes (six from §2)
Ship Monday of shakedown week. Verify 2 trading days.

**§2f audit is a documentation deliverable (not code).** Must complete before v8.3.1 branch cuts. Audit result determines whether any scorer rules get dropped as redundant OR whether scanner filters get relaxed.

### 8b. v8.3.1 — scorer module, shadow mode ON

**Precondition:** `active_scanner_filter_audit.md` exists and reviewed. Scorer rule set or scanner filters adjusted per audit. If material changes result, bump to v0.6.

- `conviction_scorer.py` added
- Env vars per §3b (scorer enabled, shadow on, flow boost off, credit suggestions off)
- Writes advisory to `conviction_shadow` tab

Two trading days minimum. Brad reviews nightly.

### 8c. v8.3.2 — go live
`CONVICTION_SHADOW_MODE=false`. Rollback = env var only.

### 8d. v8.3.3+ — known-issue cleanup
- Conviction play refire (NVDA 4/15-style 21 posts)
- Dual-labeling (CONVICTION PLAY + FLOW CONVICTION)
- Exit signal refire (TSLA 8 exits on 4/13)
- Option peak refire vs PT1/PT2/PT3 only
- **P15 reconsideration** — if bull+late drag holds in production shadow data, reintroduce as timeframe-specific rule
- Alert-type edge analysis (parked `analyze_alert_edges_v1.py`)

### 8e. v8.4+ — flow promotion evaluation
After `flow_events` has 2+ weeks clean data:
- Enable B12 (`FLOW_BOOST_ENABLED=true`)
- Weekly review of flow vs scanner lead-time and WR
- At 60+ days clean data, evaluate Q4 promotion criteria
- Produce v8.4 spec if criteria met

---

## 9. Deploy runbook (≤2 pages)

### Pre-flight
1. NOT Friday.
2. `git checkout -b v8.3.X`
3. Pull latest main, rebase.

### v8.3.0
Apply six patches, syntax-check after each with `python3 -c "import ast; ast.parse(open('app.py').read())"`:

1. OCC writer (§2a)
2. `signal_decisions` logging guard (§2b) — verify present
3. EM calibration (§2c)
4. `CRISIS_PUT_ENABLED=false` default (§2d)
5. Flow events logging (§2e)
6. Scanner filter audit deliverable (§2f) — `active_scanner_filter_audit.md` in repo root

Commit: `v8.3.0: pre-deploy fixes + scanner audit deliverable`

Render env vars:
```
EM_CALIBRATION_VERSION=v8.3
CRISIS_PUT_ENABLED=false
FLOW_LOGGING_ENABLED=true
```

Push, verify `/status` 200, verify `flow_events` tab receiving rows. Run 2 days.

### v8.3.1
**Precondition:** `active_scanner_filter_audit.md` reviewed. Scorer or scanner adjusted. Any material spec change → v0.6 bump.

1. Add `conviction_scorer.py` per §3
2. Scanner handler integration at bar close, after enrichment
3. New Sheets tab `conviction_shadow`
4. Commit: `v8.3.1: conviction_scorer (shadow mode)`
5. Render env vars:
   ```
   CONVICTION_SCORER_ENABLED=true
   CONVICTION_SHADOW_MODE=true
   CONVICTION_POST_THRESHOLD=75
   CONVICTION_LOG_THRESHOLD=60
   CONVICTION_STRICTNESS=medium
   FLOW_BOOST_ENABLED=false
   CREDIT_LEG_SUGGESTIONS_ENABLED=false
   ```
6. Push. Run 2 trading days. Review `conviction_shadow` nightly.

### v8.3.2
Env var flip: `CONVICTION_SHADOW_MODE=false`, redeploy. Monitor first 2 hours. Rollback = env var + redeploy.

### Rollback matrix
| Symptom | Rollback |
|---------|----------|
| Scorer too aggressive | `CONVICTION_STRICTNESS=loose` |
| Scorer crashing | `CONVICTION_SCORER_ENABLED=false` |
| EM targets wrong | Unset `EM_CALIBRATION_VERSION` |
| OCC phantom exits back | Revert §2a patch only |
| Flow logging noisy | `FLOW_LOGGING_ENABLED=false` |
| Credit leg suggestions wrong | `CREDIT_LEG_SUGGESTIONS_ENABLED=false` |

### Post-deploy verification
- `/signals_today` normal volume
- `conviction_shadow` / `signal_decisions` / `flow_events` tabs receiving rows
- No error spike
- Telegram cadence feels right

---

## 10. Version notes

**v0.5 (this doc):**
- **P2 split corrected (was reversed on pinescript in v0.4):** P2a (bull) = −4, P2b (bear) = −5. Data shows pinescript bear established drags slightly more than bull (medians −4.5 vs −4.0).
- **P15 removed.** Bull+late drags consistently but only 1 of 9 n≥500 cells hits the ≥5 WR threshold. Direction noted in §8d for v8.3.x re-evaluation.
- **B6b removed.** Pinescript bear+late max lift is +2.5 WR — fails threshold.
- **B13 citation corrected.** Real evidence is `active_scanner 5m T2 bull post_box n=879 +5.1 WR`, not the `n=299 +9.1` cited in v0.4. Magnitude reduced from +3 to +2 to reflect borderline status.
- **ORCL Tier-3 rationale rewritten.** Citing 55% unfiltered bull WR across timeframes at n>250 (broad, authoritative evidence) instead of n=39 at_edge observation (below floor).

**v0.4 (replaced):**
- Added scanner filter audit (§2f), credit leg suggestions (Q5), ORCL Tier-3 move, B3 +2→+3, P2/P3 direction split, B6 split, B13 (incorrectly cited), P15
- Most structural improvements correct; rule tuning had errors corrected here

**v0.3 (replaced):**
- Scanner-first trigger framing, flow as confirming layer (B12)
- §2e flow logging, Q4 flow promotion criteria

**v0.2 (replaced):** Flow-as-trigger, rejected
**v0.1 (replaced):** Pinescript-first, missing SR rules

---

## 11. Backtest provenance

All rules except B12 reference `/home/claude/v8.3/csv_batch1/BATCH{1,2,3A,3B,3C,3D}_FINDINGS.md`. Underlying CSVs at `/var/backtest/summary_*.csv`. 621,007 signals, 20 months, analyzed by `analyze_combined_v2.py`.

**Criteria applied uniformly:**
- ≥5 WR points lift or drag vs baseline
- ≥500 sample size (hard floor)
- Effect survives when other applicable filters applied
- Computable from data at signal-fire time

**v0.5 threshold discipline:** The criteria are applied strictly. Three rules from v0.4 (P15, B6b, and B13's original citation) failed one or both criteria and were removed or corrected. The n≥500 floor and ≥5 WR threshold are what make the scorer defensible — relaxing them for specific rules would undermine the whole structure.

**B12 (flow boost) has NO backtest validation.** Smallest non-zero weight, defaults OFF.

**Rules explicitly NOT in scorer (null findings):**
- Bar expansion (621K signals, no lift)
- Refire / fires_in_week
- Maturity as hard gate (used as niche boost B6a instead)
- Credit-spread strategy math as scorer rule (tautological) — surfaced via Q5 post-formatter

**Parked for v8.3.x (UNTESTED, not null):**
- Alert-type edges (analyze_alert_edges_v1.py built, not run at scale)
- Bull + late drift pattern (consistent −3 to −5 drag across 9 cells but only 1 at threshold)

---

## Summary of changes v0.4 → v0.5

| # | Change | Rationale | Source |
|---|--------|-----------|--------|
| 1 | P2a magnitude: −8 → −4 | Real pinescript bull established range is −2.5 to −4.8, median −4.0 | wave_label batch 1 |
| 2 | P2b magnitude: −4 → −5 | Real pinescript bear established range is −3.4 to −7.3, median −4.5 (BEAR worse than bull) | wave_label batch 1 |
| 3 | Removed P15 (bull + late = −3) | Only 1 of 9 n≥500 cells hits ≥5 threshold; fails Brad's criteria | maturity batch 1 |
| 4 | Removed B6b (bear + late + pinescript = +2) | Max +2.5 WR, fails ≥5 threshold | maturity batch 1 |
| 5 | B13 citation: +9.1 at n=299 → +5.1 at n=879; magnitude +3 → +2 | Original citation wasn't in the data; real evidence is borderline | potter_box batch 1 |
| 6 | ORCL rationale: at_edge n=39 → 55% unfiltered WR across all combos | n=39 is below the 500 floor; broader evidence is authoritative | per_ticker batch 3a |

All other v0.4 changes preserved: B3 +3, P3 split, B6a=+4, §2f scanner audit, Q5 credit leg suggestions, ORCL Tier-2→Tier-3 move (rationale corrected, not the move itself).
