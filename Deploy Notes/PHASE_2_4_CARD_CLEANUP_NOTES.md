# Phase 2.4 — Card Cleanup + Momentum Burst + DTE-aware Flow

Replace these files on `scanner-native-phase1`:

- `app.py`
- `v2_5d_edge_model.py`
- `v2_dual_card_integration.py`
- `oi_flow.py`
- `oi_tracker.py`
- `em_reconciler.py`

## What changed

### 1. Suppresses bad V1 credit vehicles
Garbage credit vehicles like `$0.03 on $2.50 wide` are now suppressed before Telegram.

New env defaults:

- `V84_CREDIT_SUPPRESS_BAD_VEHICLES=1`
- `V84_CREDIT_MIN_CREDIT=0.10`
- `V84_CREDIT_MIN_ROC=0.06`
- `V84_CREDIT_POST_V2_WHEN_SUPPRESSED=1`

If a V1 credit card is suppressed, V2 can still post a review-only card showing `Vehicle Status: REJECTED`.

### 2. V2 now separates setup grade from vehicle status
V2 cards now show:

- setup grade
- setup archetype
- vehicle status: `APPROVED`, `REJECTED`, or `NOT_CHECKED`
- final action: `REVIEW`, `STALK`, `NO TRADE`, `FIND BETTER VEHICLE`, or `MOMENTUM REVIEW`

This prevents an A+ setup from visually approving a bad spread.

### 3. Momentum Burst label
V2 can now label fast reclaim/breakout situations as:

- `V2 MOMENTUM BURST`

This is separate from the normal 5D spread-hold model.

The classifier looks for live evidence such as:

- recent move expansion
- relative volume
- reclaim/break from structure
- above VWAP
- MTF alignment
- ADX / strong trend / explosive regime
- blue-sky / above resistance
- scorer strength

### 4. DTE-aware flow/OI context
Morning OI confirmation and unusual flow cards now include better context:

- expiration
- DTE / active status
- distance from spot
- horizon label such as 0DTE intraday, 1–3DTE momentum, weekly, swing, larger thesis

`oi_tracker.py` now stores per-expiration strike OI going forward. The first day after deploy may still show aggregate expirations until the new baseline exists.

### 5. EM scorecard plain-English read
The EM scorecard now leads with what the stats mean:

- whether direction is useful
- whether range/neutral is stronger
- whether realized move is under/over expected move
- how to use EM in trading decisions

## Suggested commit message

`Add V2 card cleanup and DTE-aware flow context`
