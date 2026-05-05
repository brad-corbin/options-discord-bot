# Phase 1.8.1 — Summary Writer Hotfix

Replace only:

```text
backtest/bt_active_v8.py
```

Fixes:

- Adds the missing `_stat()` helper used by `write_mtf_confluence_summary()`.
- Keeps `_stat()` as a thin alias to `_edge_stats()` so future summary writers use the same 1D/3D/5D statistics.
- Does not change signal detection, exit grading, MTF calculations, approved setup classification, or location/timing logic.

Purpose:

- Prevents another long `ALL` run from failing at the final summary-writing stage.
