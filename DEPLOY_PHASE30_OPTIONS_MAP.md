# Phase 3.0-B/C — Watch Map Sidecar

This patch adds a manual-only `/watchmap` command that builds a compact watch map from existing chain, EM, dealer, and Flow/OI context.

## Scope

Display/context only. It does **not** change:

- V1/V2 cards
- Momentum/Burst cards
- scorer decisions
- spread ranking
- managed-trade registration
- entry/exit logic
- backtests

## Commands

```text
/watchmap SPY
/watchmap QQQ
/watchmap SPY diag
/watchmap QQQ intraday
/watchmap SPY both
/watchmap SPY compact
/omap SPY
/optionsmap SPY     # backward-compatible alias
/emap SPY            # backward-compatible alias
```

## Routing env vars

```env
WATCH_MAP_ENABLED=1
WATCH_MAP_ROUTE=intraday          # intraday | diagnosis | both | main
WATCH_MAP_ALLOW_MAIN=0            # keep 0 during testing
WATCH_MAP_TOP_N=3
WATCH_MAP_SHOW_TRIGGERS=1

# OPTIONS_MAP_* aliases still work for backward compatibility.
```

Default behavior posts to the intraday channel. Main-channel posting is blocked unless `WATCH_MAP_ALLOW_MAIN=1` or `OPTIONS_MAP_ALLOW_MAIN=1`.

## What the card shows

- spot
- expiration + DTE
- EM range
- gamma flip / put wall / call wall / max pain when available
- call-resistance contracts with expiry/DTE/OI/volume
- put-support contracts with expiry/DTE/OI/volume
- existing Morning OI/Stalk tags when exact contract matches exist
- simple Above / Below watch levels
- conservative/primary/stretch targets in full mode
- plain-English Watch triggers
- compact mode for short mobile output

## Important

This command intentionally avoids the existing `_get_0dte_iv()` path because that path can trigger intraday flow detection and conviction routing side effects. `/watchmap` fetches the chain and builds a display snapshot without changing trading decisions.
