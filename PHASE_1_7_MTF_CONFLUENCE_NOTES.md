# Phase 1.7 — Multi-Timeframe Confluence Discovery

This patch is **backtest-only**. It does not change live Telegram alerts, scanner thresholds, or trade posting.

## Goal

The scanner was always intended to help build 2–5 day trade ideas. Phase 1.7 adds a research layer that asks:

> When a 5-minute scanner candidate fires, are the 15m, 30m, 60m, and daily trends aligned with it or fighting it?

## What changed

`backtest/bt_active_v8.py` now:

- Aggregates historical 5-minute bars into completed 15m, 30m, and 60m bars.
- Uses only completed higher-timeframe candles at the signal timestamp to avoid lookahead bias.
- Computes higher-timeframe trend and MACD state for 15m/30m/60m.
- Adds a daily-trend component using the existing `daily_bull` state.
- Adds one alignment score and label per candidate.

## New columns in trades / edge_discovery

- `mtf_15m_trend`
- `mtf_30m_trend`
- `mtf_60m_trend`
- `mtf_15m_macd`
- `mtf_30m_macd`
- `mtf_60m_macd`
- `mtf_15m_rsi`
- `mtf_30m_rsi`
- `mtf_60m_rsi`
- `mtf_15m_adx`
- `mtf_30m_adx`
- `mtf_60m_adx`
- `mtf_alignment_score`
- `mtf_match_count`
- `mtf_oppose_count`
- `mtf_alignment_label`
- `mtf_stack`

## New output files

- `edge_by_mtf.csv`
- `mtf_confluence_summary.csv`

## Alignment labels

- `full_aligned` — 15m, 30m, 60m, and daily all align with the signal direction.
- `strong_aligned` — at least 3 align and the score is strongly positive.
- `partial_aligned` — positive alignment, but not full/strong.
- `mixed` — alignment and opposition offset.
- `countertrend` — higher timeframes mostly oppose the signal.

## What to inspect first

After the next run, start with:

1. `mtf_confluence_summary.csv`
2. `edge_by_mtf.csv`
3. `edge_by_combo.csv` rows containing `approved_mtf`, `bias_pb_mtf`, and `bias_fib_mtf`

Primary question:

> Do the approved bullish Potter Box/CB setups improve when 15m/30m/60m/daily are aligned?

Secondary question:

> Are there countertrend setup buckets that still work, suggesting a distinct pullback/reclaim archetype?
