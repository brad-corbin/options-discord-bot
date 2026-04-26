# Phase 1.7.1 — MTF/Fib Bucket Hotfix

Fixes the failed Active Scanner Backtest run caused by:

```text
NameError: name '_fib_bucket' is not defined
```

Root cause: `write_edge_by_combo()` referenced `_fib_bucket()` even though Phase 1.7 uses the existing `_edge_fields(t)['fib_bucket']` helper path for Fib bucketing.

Change made:

```python
("bias_fib_mtf", lambda t: f"{t.bias}|fib={_edge_fields(t)['fib_bucket']}|mtf={t.mtf_alignment_label}"),
```

This is a reporting-only fix. It does not change scanner logic, trade grading, setup classification, or MTF calculations.
