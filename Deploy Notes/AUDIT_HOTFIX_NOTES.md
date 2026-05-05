# Phase 2.4 Audit Hotfix Notes

This package is the audited Phase 2.4 package with two safety fixes:

1. Credit-spread exit tracking is now gated by `REVIEW_CARDS_AUTO_OPEN_ENABLED`.
   - Before hotfix: a v8.4 credit card that passed vehicle filters could still register `track_spread(...)` and start 50% profit / 2x credit stop monitoring even though cards are review-only by default.
   - After hotfix: review-only credit cards do not register live spread tracking unless Brad explicitly sets `REVIEW_CARDS_AUTO_OPEN_ENABLED=1`.

2. Momentum Burst score display is capped at 10.
   - Before hotfix: additive scoring could show values like `11/10`.
   - After hotfix: thresholding can still use raw score, but the displayed card score is capped at `10/10`.

AST syntax checks passed for:
- app.py
- v2_5d_edge_model.py
- v2_dual_card_integration.py
- oi_flow.py
- oi_tracker.py
- em_reconciler.py
