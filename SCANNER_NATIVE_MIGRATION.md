# Scanner-Native Migration — Phase 1

Goal: make the active scanner the real signal source instead of packaging scanner alerts as old TradingView (`tv`) jobs.

## What changed

### 1. `active_scanner.py`
- Removed the duplicated second copy of the scanner implementation.
- Kept one live definition of:
  - `_compute_adx()`
  - `_analyze_ticker()`
  - `ActiveScanner`
- Fixed the header duplication.
- Changed scanner dispatch from:
  - `self._enqueue("tv", ticker, bias, webhook_data, signal_msg)`
- to:
  - `self._enqueue("scanner", ticker, bias, webhook_data, signal_msg)`
- Preserved the existing scanner scoring logic. This phase is plumbing cleanup, not a strategy rewrite.
- Preserved the ADX pass-through in `webhook_data` so scorer/audit rows can see ADX.

### 2. `app.py`
- Added support for `job_type == "scanner"` through the same scalp/active execution path previously used by `tv`.
- Updated crisis/volatility early gate to treat scanner scalp signals like TV scalp signals.
- Updated the pre-chain gate to receive the real `job_type` instead of hardcoding `"tv"`.
- Updated `prechain_gate.py` so scanner scalp jobs still receive the same macro-event and CRISIS bull-call blocking that old `tv` scalp jobs received.
- Updated digest labeling so scanner-originated cards show as scanner signals instead of TV signals.

### 3. Active scanner backtesting
- Updated `.github/workflows/active_backtest.yml` to run:
  - `backtest/bt_active_v8.py`
- instead of stale:
  - `backtest/active_backtest.py`
- `bt_active_v8.py` already imports live scanner helper functions and is closer to the real scanner than the older duplicated backtest.
- Added ADX into `bt_active_v8.py` output and `summary_by_indicator.csv` so scanner ADX behavior can be studied instead of being invisible.

### 4. Old active backtests removed
Removed stale active scanner backtests:
- `backtest/active_backtest.py`
- `backtest/bt_active.py`

The remaining active scanner backtest source of truth is:
- `backtest/bt_active_v8.py`

## What this phase does NOT do yet

This phase does not rewrite the scanner into a full institutional setup engine yet. It only fixes the plumbing so scanner signals are honest scanner jobs and the active scanner backtest uses the most live-aligned runner.

Next phase should focus on making the scanner detect trade setups instead of only indicator/momentum alignment:
- gamma flip interaction
- EM high/low interaction
- OR30 high/low
- VWAP reclaim/failure
- failed breakouts/breakdowns
- retests
- no-chase / extension gates
- entry trigger state machine

## Backtest rule going forward

Any scanner logic change should be tested with:

```bash
BACKTEST_OUT_DIR=results python backtest/bt_active_v8.py --ticker SPY --days 180
```

or through GitHub Actions:

`.github/workflows/active_backtest.yml`

Do not reintroduce copied scanner logic into new backtests. The backtest must import live scanner helpers whenever possible.
