# Phase 1.9 — Graded Setup + Vehicle Selection Layer

Backtest-only change. This does not change live posting, scoring, or trade execution.

## Purpose

Splits the backtest decision into two separate questions:

1. **Setup quality:** Should this scanner candidate be treated as A+, A, B, Shadow, Block, or Exclude?
2. **Vehicle expression:** If the setup is tradeable, does the historical behavior look more suited to debit continuation or a credit-spread support-hold structure?

## New Trade columns

- `setup_grade`
- `grade_reason`
- `vehicle_preference`
- `vehicle_reason`
- `debit_score`
- `credit_score`
- `debit_proxy_win`
- `credit_proxy_win`
- `recommended_structure`

## New output files

- `setup_grade_summary.csv`
- `vehicle_selection_summary.csv`
- `grade_x_vehicle_summary.csv`

## Important caveat

`credit_proxy_win` is a price-boundary proxy based on Potter Box support/resistance survival. It is not a full option P/L model. It does not include premium received, IV/skew, commissions, assignment risk, early exits, or spread width optimization.

The purpose is to test whether the underlying price behavior is more consistent with a debit continuation trade or a credit-spread hold/defend trade.

## Intended interpretation

- `PB_CB_RECLAIM_BULL_APPROVED` is graded highest when MTF alignment is full/strong and is generally vehicle-preferred as credit/hybrid because the edge often appears to be price holding structure over 5D.
- `PB_BREAKOUT_BULL_APPROVED` is generally debit-preferred because the edge is directional continuation after a Potter Box breakout.
- Block/shadow setups should remain non-actionable until separate models prove them.
