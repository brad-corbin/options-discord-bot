# Backtest Runner — Brad's Unified Signal v3.0 (enriched)

One-off backtest of the v3.0 pinescript, with overlays for Potter Box, Fib
levels, swing H/L, and timing cuts. Answers: *"does the v3.0 signal have
edge today, where, and does confluence with other systems improve it?"*

**Runs completely separate from the live bot.** No Redis writes. No Sheets.
No Telegram. No touches to any bot state. Safe to run while the bot is trading.

---

## What you get (in `/tmp/backtest_v3/`)

| File | What's in it |
|---|---|
| **`trades.csv`** | Every signal fired, one row each. All context columns — entry, exit, MAE/MFE, Potter Box state, Fib proximity, regime, timing |
| **`summary_by_ticker.csv`** | WR by ticker × tier × direction. Sort this to find the "which tickers carry the edge" list |
| **`summary_by_regime.csv`** | WR by BULL/BEAR trend × VIX bucket × tier × direction |
| **`summary_by_timing.csv`** | WR by hour-of-day, day-of-week, days-to-Friday |
| **`summary_by_confluence.csv`** | WR by Potter Box / Fib / HTF / Daily alignment overlays, with a **`vs_baseline_wr_pct`** column showing how much each overlay helps or hurts vs. pinescript alone |
| **`report.md`** | One-page written interpretation |

---

## How to run it on Render

### 1. Get the script into your repo

Drop `backtest_v3_runner.py` at the repo root (same level as `app.py`).
Commit and push. Render picks it up on next deploy.

Don't want to redeploy just for this? Upload via the Render shell:
```bash
cd /opt/render/project/src
nano backtest_v3_runner.py   # paste contents, Ctrl-O to save, Ctrl-X to exit
```

### 2. Confirm `MARKETDATA_TOKEN` is set

Already set for the live bot. Verify in Render shell:
```bash
echo $MARKETDATA_TOKEN
```
Should return a long string.

### 3. Open the Render shell

Render dashboard → your service → "Shell" tab.

### 4. Run it

```bash
cd /opt/render/project/src
python backtest_v3_runner.py
```

Prints progress per ticker. Takes 10-20 minutes first run — each ticker
needs ~9 chunked fetches of 90-day windows to cover Aug 2023 → today.

If it crashes or you disconnect, just re-run — it resumes from the last
completed ticker via a progress checkpoint.

### 5. Read the report

```bash
cat /tmp/backtest_v3/report.md
```

### 6. Get the CSVs back

Small summaries are easy to read in-terminal:
```bash
cat /tmp/backtest_v3/summary_by_ticker.csv
cat /tmp/backtest_v3/summary_by_regime.csv
cat /tmp/backtest_v3/summary_by_timing.csv
cat /tmp/backtest_v3/summary_by_confluence.csv
```

Paste those four back into our chat and I can read them directly.

The full `trades.csv` is large (~5-10MB depending on signal count). If you
need me to see it for spot-checks, we'll deal with that separately — for the
first-pass analysis the four summary CSVs carry all the information.

---

## Grading rules baked into the backtest

- **Entry:** next 15m bar's open after signal fires
- **Primary exit:** Friday close, minimum 3 trading days out
  - Signal Mon/Tue → exit this Friday (3-4 trading days)
  - Signal Wed/Thu/Fri → exit **next** Friday (gives trade room to breathe)
- **Parallel 2-week exit:** Friday ~10 trading days out, for comparison
- **Win buckets** (matching the AAPL 262.5/265 and 265/267.5 spread example):
  - **Full win:** price moved ≤ 1% against direction (short strike still ITM)
  - **Partial:** 1-2% against (between strikes — **NOT counted as win**)
  - **Full loss:** > 2% against (past long strike)
- **Hold-through-drawdown:** no intraweek stops. MAE/MFE tracked for diagnostic
- **Grading metric used throughout:** "Full win" only. Partials are separate column

## Overlays computed per signal (no lookahead)

All overlays use the **prior** daily bar's state at signal time — no future info.

- **Potter Box** (20-day range): `above_roof`, `below_floor`, `in_box`, or `no_box`
- **Fib level**: nearest of 23.6/38.2/50/61.8/78.6, distance %, above/below
- **Swing H/L**: nearest fractal swing high/low + distance %
- **Regime**: SPY vs 200DMA (BULL/BEAR) + VIX bucket (LOW/NORMAL/ELEVATED/CRISIS)
- **Timing**: hour of day (9-15 ET), day of week (Mon-Fri), days to Friday

---

## Customizing

Different date range:
```bash
BACKTEST_START=2024-08-01 BACKTEST_END=2025-04-01 python backtest_v3_runner.py
```

Only specific tickers:
```bash
BACKTEST_TICKERS="SPY,QQQ,AAPL" python backtest_v3_runner.py
```

Full reset (delete progress + re-run from scratch):
```bash
rm -rf /tmp/backtest_v3
python backtest_v3_runner.py
```

---

## What "edge" looks like in the output

Real edge, in order of strength:

1. **N trades ≥ 30** (not a fluke)
2. **Headline WR ≥ 60%** (meaningful above coin flip)
3. **+Partial ≥ 75%** (few full losses — trade structure forgiving)
4. **Consistent across regime cells** — if only BULL+NORMAL shows it, that's the
   gate condition to trade under

The **`vs_baseline_wr_pct`** column in `summary_by_confluence.csv` is the
most diagnostic thing in the output. It shows:
- How much Potter Box **above_roof** changes WR for Bull signals vs. baseline
- How much Fib **near 61.8%** changes WR
- How much HTF **aligned** vs. **not aligned** changes WR

Overlays with `+5 or more` are candidates to gate on (only take signals
when they fire). Overlays with `−5 or more` are candidates to filter out
(never take signals when they fire that way).

If multiple cells clear the ≥60% / ≥30-trade bar but they're all in a
specific combination — say, T2 Bull + above_roof + BULL+NORMAL — **that's
the rule**. The bot trades only that. Everything else stays quiet.

If NO cell clears the bar, the v3.0 signal doesn't have actionable edge
in the current regime. That's a real answer and we pivot from there.
