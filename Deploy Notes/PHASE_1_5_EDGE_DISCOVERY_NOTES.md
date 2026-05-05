# Phase 1.5 — Edge Discovery Backtest

This package replaces only:

- `backtest/bt_active_v8.py`

It adds research outputs without changing live bot logic.

## New files produced in the GitHub Actions artifact

- `edge_discovery.csv` — one row per scanner-qualified candidate with derived feature/context columns.
- `edge_by_feature.csv` — feature-level performance vs baseline.
- `edge_by_combo.csv` — combination-level performance vs baseline.
- `missed_edge_candidates.csv` — strong combinations that may be underused by the live bot.
- `negative_edge_filters.csv` — weak combinations that may deserve shadow-only/block treatment.

## Purpose

This is designed to answer: which already-computed features actually contain edge?

Examples:

- Potter Box state
- CB side
- Wave label
- Regime + HTF alignment
- VWAP location
- Fib/swing proximity
- RSI / ADX / MACD / volume buckets
- Feature combinations

No live strategy rules are changed in this patch.
