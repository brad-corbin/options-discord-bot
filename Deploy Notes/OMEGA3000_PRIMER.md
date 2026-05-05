# Omega 3000 — Session Primer

Paste this at the top of any new chat. This replaces re-explaining.

---

## Who I am, how I trade

I'm Brad. Options trader, not a developer. I trade on Schwab: 1–14 DTE debit spreads (both strikes ITM, held to Friday), credit spreads on deep OTM, occasional long calls/puts. I roll contracts, adjust strikes mid-trade, sometimes turn paper losers into real wins by managing the position. I am NOT an algorithm — the bot surfaces signals, I execute and manage.

I trade with Seth. My main channel is the live trade surface. Alpha SPY Omega is a separate 0DTE engine with its own backtested rules — do not conflate it with the 1–14 DTE bot. Diagnostic chat is a sink for Potter Box / OI / stalk context.

## What this project actually is

Started as a pinescript on TradingView — "Unified Signal v3.0" — that fires T1 and T2 signals on bar close using a confluence of 7 indicators (EMA, MACD, Wave Trend, RSI+MFI, StochRSI, VWAP, ADX) plus HTF and Daily trend filters, plus candle quality and session timing gates. This was the original edge, built on two-commonality debit spread logic I'd been trading for years.

It grew into a Python bot on Render that:
- Consumes pinescript webhooks from TradingView and turns them into trade cards
- Runs 22+ analytical layers (Potter Box, OI flow, flow conviction, active scanner, thesis monitor, EM model, GEX, skew, etc.) as overlays
- Posts to Telegram on main + intraday + diagnosis channels
- Maintains a Dashboard Sheet ("Dashboard 3000") as read-only observability
- Has a recommendation tracker with grading, a trade journal, and multiple data stores in Redis

The current release is v8.2. Core files: `app.py` (~542KB), `potter_box.py`, `thesis_monitor.py`, `active_scanner.py`, `swing_scanner.py`, `oi_flow.py`, `schwab_stream.py`, `schwab_adapter.py`, `dashboard.py`, `recommendation_tracker.py`, `card_formatters.py`, `telegram_commands.py`.

Repo root on Render: `/opt/render/project/src/`.

## What we just validated (April 2026 backtest)

**Dataset:** 172,678 pinescript T1/T2 signals across 35 tickers, Aug 2023 → Apr 2026 (20 months). Live Potter Box engine (`detect_boxes()`) ported directly into the backtest — same algorithm the bot uses. Grading: full_win = price ≤1% against at Friday close, partial = 1-2% against, full_loss = >2% against. Min 3-day hold, exit at next Friday.

**Headline numbers:**
- Overall WR: 61.6% (headline) / 70.5% (win + partial)
- T1 Bull: 65.6%, T2 Bull: 64.7%
- T1 Bear: 57.6%, T2 Bear: 57.6%
- BULL regime WR: 62.1%. BEAR regime WR: 54.7% (small sample, ~9K trades).

### The biggest single finding — CB side (Potter Box midpoint)

17-20 WR point swing based on where inside the box the signal fires.

| Signal | CB side | WR | Samples |
|---|---|---|---|
| T2 Bull | below CB | 73.0% | 15,571 |
| T2 Bull | above CB | 55.6% | 14,508 |
| T2 Bear | above CB | 72.1% | 11,742 |
| T2 Bear | below CB | 52.6% | 13,223 |

**Rule:** Only take bulls firing from below CB. Only take bears firing from above CB. This is the single highest-impact filter in the data.

### Wave label matters too

`breakout_imminent` (max_touches ≥ 5) adds +5 to +7 WR points vs baseline across all tiers and directions. `established` is worst.

### Indicator quintiles — "Diamond" is real

EMA diff pct Q3 (near zero): +3-4 WR. Q1 and Q5 (extreme): −3-4 WR.
MACD hist pct Q3 (near zero): +3-4 WR. Q5 (very high, exhausted momentum): **−6 WR for T2 Bull.**

Stoch K/D, Wavetrend, RSI, ADX, candle body % — all basically flat across quintiles. Not differentiators.

### Timing

- Wednesday bears: 54% WR, 5+ points below other days. Filter out.
- Mon/Tue entries best (days_to_friday 3-4). Wed entries worst (days_to_friday 2).
- Hour of day: roughly flat, slight edge to mid-morning.

### Re-fires (question: should I roll when signal re-fires mid-week?)

Answer: **hold Monday, don't roll.** When a bull signal fires 4+ times in a week, the first fire's WR is 67% vs 64.5% for lone-fire weeks. For bears it's 62% vs 53% — a +9 point boost. Re-fires are confirmation the Monday entry is working, not reason to close and re-enter.

### Credit spread income engine — every ticker wins on credit

Sold at Potter Box floor (bulls) / roof (bears), win if price stays above/below short strike at Friday close. Every single ticker × direction × tier shows credit WR higher than debit WR. Honest caveat: I don't have historical option premiums, so exact dollar EV unknown. Rule of thumb: 85%+ credit WR = probably positive EV, 75-85% dicey, below 75% likely negative.

The Tier-D debit-losers that flip to credit winners:

| Ticker | T2 Bull debit WR | T2 Bull credit WR |
|---|---|---|
| TSLA | 54.2% | 78.0% |
| MSTR | 54.9% | 80.8% |
| COIN | 51.5% | 82.3% |
| SMCI | 53.4% | 79.5% |
| PLTR | 62.9% | 86.3% |
| MRNA | 56.0% | 82.9% |
| ARM | 57.3% | 78.8% |
| AMD | 60.6% | 85.6% |

**GLD Bull put credit at the box floor: 97.0% WR over 1,027 samples.** The best single cell in the dataset.

Credit spread width doesn't change WR (WR depends only on whether price crosses short strike). Width affects premium collected and max loss. Use $2.50 on indices/sectors/large-caps, $5.00 on high-IV names (TSLA, MSTR, COIN, SMCI, MRNA).

## Tier lists (use these, don't re-derive)

**Debit-eligible tickers (tier A/B — trade debit spreads):**
SPY, XLF, DIA, GLD, XLV, JPM, QQQ, GOOGL, CAT, XLE, GS, TLT, MSFT, SOXX, IWM, CRM, AAPL, AMZN, NVDA, NFLX, AVGO, META, LLY, UNH, ORCL

**Credit-only tickers (debit fails, credit works):**
ARM, BA, COIN, MRNA, MSTR, PLTR, SMCI, SOFI, TSLA, AMD

## The conviction take gate (for when bot tells me to trade)

A signal is "conviction" when ALL true:
- Pinescript T1 or T2 fires
- Ticker in debit-eligible tier list (for debit) OR credit-only (for credit)
- Potter Box state = `in_box`
- CB side aligned (bulls below CB, bears above CB)
- Wave label = `breakout_probable` or `breakout_imminent`
- Not Wednesday bear

Projected WR with full gate: 72-76% headline, 80%+ win-or-partial.

Trade card should SHOW additional context (day-of-week, MACD quintile, RSI quintile, wave label, projected WR from backtest cell) even if only CB side + ticker gate is strictly enforced. My eyes do final aggregation. "Trader not algorithm."

## Settled decisions — don't re-litigate

- Pinescript is the signal source (proven in the backtest). `active_scanner.py` has its own scoring that is NOT backtested — its "T2 🐂 67/100" numbers are heuristic, not validated. Should NOT drive Telegram posts.
- Long-term: port pinescript logic INTO the bot so I'm not dependent on TradingView webhooks (the backtest code in `backtest_v3_runner.py` already contains a validated Python implementation of all 7 indicator layers and the T1/T2 gate logic — `compute_v3_signals()` and helpers). This port is not weekend work. Staged approach: shadow mode for 1-2 weeks before cutover.
- Entry price in backtest = NEXT bar's OPEN (not signal bar close). Realistic live-fill assumption, 0 slippage baked in.
- Schwab rate limit: 110/min (env-tunable `SCHWAB_RATE_PER_MIN`).
- Confidence post threshold set via `/confidence` Telegram command.
- Grading uses option-exit price vs option-entry price, not underlying move.
- No hotfixes during market hours. Friday is shakedown day. Monday is my busiest trading day.
- I lost $5K on 2026-04-17 because v8.2's Patch 2 resurrected the dead thesis_monitor daemon without dedup, flooding Telegram with 474 main-channel messages in 9 hours. Root cause diagnosed, deploy since then has been more conservative.
- Alpha SPY Omega is a SEPARATE 0DTE engine with its own backtest. Rules from this 1-14 DTE backtest DO NOT apply. Leave it alone unless explicitly scoped.
- I am NOT a developer. Deploy runbooks stay under 2 pages. Explain "why" when non-obvious. Don't lecture.

## Known issues — document, don't fix unless asked

- **TSLA 23-alert bug**: Potter Box break monitor in `schwab_stream.py` has no post-breakout cooldown. Once a break fires at $X, streaming monitor keeps firing on every new high because cached roof stays stale until 8+ daily bars form at new level (takes days). Fix is in `PotterBoxBreakMonitor` — add "box_break_already_fired" sentinel.
- **Flow conviction re-fires every 8 min** on same snapshot. Needs digest scheduling (9/11/1/2:30 CT).
- **Alpha SPY Omega cross-posts every ticker** — app.py lines 5361, 5462, 6811, 7033 all cross-post to TELEGRAM_CHAT_INTRADAY without ticker filter. Simple 4-line fix: add `and ticker.upper() in ("SPY","QQQ")` check.
- **Dashboard 3000 logging is polluted** — `_detect_new_signals` logs every oi_flow direction flip as a new signal event. Produced 1,055 flow_conviction rows in one day, most noise. Dedup needed.
- **PLTR phantom PnL** on Position PnL tab: Peak Premium shows 0.0 (impossible, entry was 0.75), Current PnL% is −1.0 for a bear_call_spread that closed at 0.00 (should be MAX PROFIT for credit spread; sign convention inverted).
- **Conviction posts as both CONVICTION PLAY and FLOW CONVICTION** (2× noise per signal). Deferred dedup.
- **Exit signal re-fires** multiple times on same position. Deferred.
- **BREAKDOWN CONFIRMED** annotation: 0 hits in Telegram export because thesis_monitor daemon was dead for weeks before v8.2 revived it.

## Code artifacts — where things live

- **Backtest code (final, proven correct):** `/mnt/user-data/outputs/backtest_v3_runner.py`. 1,914 lines. Contains: all 7 pinescript indicators as Python functions, `compute_v3_signals()` producing T1/T2 SignalBar objects, `detect_boxes()` import from live `potter_box.py`, trade simulation, 7 summary CSV writers, resume-from-checkpoint logic with dataclass-field introspection for type casting.
- **Backtest outputs:** `/tmp/backtest_v3/` on Render. `trades.csv` (172,678 rows), seven summary CSVs, `report.md`, `.progress.json` checkpoint.
- **Dashboard Sheet:** ID `1v9UN6qoTWdFJWe332qfnrvJwMF88JavN-MteHL66Iv8`. Service account `bot-sheets-writer@corbin-bot-tracking.iam.gserviceaccount.com` (needs Editor permission).
- **Trade journal Sheet:** separate from Dashboard. "Omega 3000 Bot Tracking" — the legacy sheet with `position_tracking_*` tabs, `signal_decisions` (1,032 rows), `conviction_plays` (442 rows), `em_predictions`, etc.
- **Telegram channels:** main, intraday (Alpha SPY Omega), diagnosis.

## Code style (when patching)

- Every patch gets a `# v<version>:` or `# v<version> (Patch N):` marker grep-able in the file
- Header block on `app.py` lists every patch applied with file + one-liner description
- Wrap all new background work in try/except at every level — dashboard bugs must NEVER bring down trading
- Log every failure. `except: pass` is forbidden. At minimum log.debug with context, log.warning for anything actionable.
- Every new feature gets an on/off env var that defaults to OFF
- Any observability feature must be purely additive: daemon thread, reads from existing stores, never blocks trading, fails gracefully with clear rollback
- After each edit: `python3 -c "import ast; ast.parse(open('file.py').read())"` to confirm syntax clean

## How I want to be talked to

- Don't restate my constraints back at me unless I've just changed them
- Don't propose building something without first hearing what I actually asked for
- When I ask a question, answer THAT question before adding context
- Deploy runbooks under 2 pages
- Code changes go to `/mnt/user-data/outputs/`, not inline
- When mid-task and running out of context, produce a precise handoff covering what's done, what's half-done, what decisions were made, what the next chat needs to finish — don't ship partial files labeled as finals
- When QA'ing, actually run the tests. Don't describe what tests would check.
- When something's done, say it's done. Don't invent more work to justify continued engagement.
- I have limited patience for long theoretical discussions when I've given direction

## Current state as of 2026-04-18

- Backtest complete, edge validated
- Conviction gate rules defined, tier lists finalized
- Weekend plan was: patch `app.py` + `oi_flow.py` + `thesis_monitor.py` to route through a new `telegram_gate.py` + `card_enrichment.py`, add flow digest scheduling, fix Alpha SPY cross-post bug. Medium strictness gate (CB side + ticker list live, wave label + day-of-week shown on card but not gated). Deploy Sunday evening, go live Monday.
- Pinescript-to-bot port decision was pending when this document was written. Current recommendation: port next week in shadow mode, not this weekend.
- Project may or may not continue. If it does, resume from here. If it doesn't, this document is a record of what the edge is — enough to trade manually on TradingView using the pinescript + Potter Box CB side rule + tier lists, without the bot at all.
