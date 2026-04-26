# Phase 1.6 — Approved Setup Classifier

Backtest-only layer. This does **not** change the live bot.

## Goal

Separate raw scanner candidates into named setup archetypes so we can compare:

- raw scanner candidates
- approved bullish setup candidates
- shadow/research-only candidates
- blocked negative-edge candidates

## New columns on `trades.csv` and `edge_discovery.csv`

- `setup_archetype`
- `approved_setup`
- `setup_action` (`APPROVE`, `SHADOW`, `BLOCK`, `RESEARCH`)
- `block_reason`
- `suggested_hold_window`
- `suggested_vehicle`
- `setup_score`

## New output files

- `approved_setups.csv` — classifier output for clean scanner candidates
- `setup_classifier_summary.csv` — performance summary by action/archetype/bias

## Initial classifier logic

Approved bullish archetypes:

- `PB_CB_RECLAIM_BULL_APPROVED`: bull + in_box + below/at CB
- `PB_BREAKOUT_BULL_APPROVED`: bull + above_roof/post_box

Blocked / shadow archetypes:

- `BULL_CHASE_IN_BOX_BLOCK`: bull + in_box + above CB
- `BEAR_NO_BOX_BLOCK`
- `BEAR_ABOVE_ROOF_BLOCK`
- `BEAR_IN_BOX_BELOW_CB_BLOCK`
- bear breakdown is research-only until separately validated

## Next analysis

After running the backtest, inspect `setup_classifier_summary.csv` first. The key question is whether `APPROVE` materially improves 3D/5D win rate and average move versus raw scanner candidates.
