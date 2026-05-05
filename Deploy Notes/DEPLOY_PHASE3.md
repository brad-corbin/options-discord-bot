# Phase 3 Deploy — v8.5 Subscription-Gated Alert Model

Built on **Phase 2 + v8.4.7**. 5 files modified, 1 new file, ~350 LOC. Ship after Phase 2 has stabilized for at least one session.

## What's in this deploy

| File | Change |
|---|---|
| `subscriptions.py` (new) | `SubscriptionManager` — Redis-backed, 7-day TTL, `add/remove/get/mode_for/has_daytrade/list_all` |
| `telegram_commands.py` | `/daytrade <TICKER> <call|put|close>`, `/conviction <TICKER> <call|put|close>`, `/positions` |
| `thesis_monitor.py` | `ActiveTrade.status` can now be `SHADOW`. Conviction trades always create as `SHADOW`. `_poll_cycle` gated on `/daytrade` sub OR real (OPEN/SCALED/TRAILED) trade. `init_daemon(chat_id=...)` propagated through. |
| `oi_flow.py` | `signal_lag_sec` field on conviction plays + `_detect_move_start` method |
| `app.py` | Sub manager init; `chat_id=TELEGRAM_CHAT_ID` into thesis daemon; `_build_compact_conviction_prompt` helper; all 4 conviction post sites wrapped with sub gate layered on Phase 2's `_exit_within_cd` |

## Behavior matrix

| Event | No sub | `/conviction` sub | `/daytrade` sub |
|---|---|---|---|
| New conviction flow on ticker X dir D | Compact prompt (rate-limited by existing `conviction:{route}:{ticker}` cooldown) | Full conviction card | Full conviction card |
| Conviction re-fire | Compact prompt | Full card | Full card |
| EXIT SIGNAL on X (flow flip) | Silent | Posts + **auto-closes** the `/conviction` sub | Posts (sub stays open) |
| THESIS ALERT (momentum fade, level break, gamma flip) | Silent | **Silent** (flow-only mode) | Posts |
| TRADE ALERT (entry from technicals) | Silent | Silent | Posts |
| TRADE MGMT (scale/trail/exit of real trade) | Silent | Silent | Posts |

## Redis subscription schema

Key: `subscriptions:{chat_id}:{TICKER}:{call|put}`

Value (JSON, 7-day rolling TTL):
```json
{
  "mode": "daytrade" | "conviction",
  "created_at": "2026-04-20T14:32:11",
  "source": "manual" | "flow_prompt",
  "ticker": "NVDA",
  "direction": "call",
  "source_dte": 14,               // optional, conviction only
  "source_expiry": "2026-05-04",  // optional, conviction only
  "source_notional": 31300000     // optional, conviction only
}
```

**Precedence:** when `mode_for()` finds a key, it returns that mode. Upgrading conviction→daytrade writes a fresh key with `mode=daytrade`.

## Deviations from handoff — flagged

1. **`_detect_move_start` uses `self._spot_history`**, not `schwab_stream.get_intraday_bars`. The latter function does not exist in v8.4.7 and would have failed silently under the try/except the handoff specified. `_spot_history` is the same primitive `_get_recent_move_pct` uses — drop-in replacement, same semantics, returns `None` on thin history. Feature is informational only (no behavior change), so degraded results are harmless.

2. **Four conviction post sites, not three** (carried over from Phase 2). Same four sites you already patched for Bug #4. Phase 3 applies its subscription gate identically on all four, including the unmentioned site 3 (chain-iv path).

## Pre-deploy sanity test

Before pushing, on a local container with `_persistent_state` wired:

```python
from subscriptions import init_subscription_manager, get_subscription_manager
init_subscription_manager(_persistent_state)
sm = get_subscription_manager()
sm.add("test_chat", "SPY", "call", mode="daytrade")
assert sm.has_daytrade("test_chat", "SPY")
assert sm.mode_for("test_chat", "SPY", "call") == "daytrade"
sm.remove("test_chat", "SPY")
assert not sm.has_daytrade("test_chat", "SPY")
```

If `scan_iter` doesn't work on the Redis client, `sm.list_all()` returns `[]` and the test still passes on `has_daytrade/mode_for`. The `/positions` command tolerates empty list.

## Post-deploy verification (first full session)

```bash
# Subscription gate is firing silences:
grep -c "COMPACT PROMPT"            render.log   # non-zero when flow fires on un-subbed tickers
grep -c "EXIT SIGNAL (un-subbed"    render.log   # should roughly replace prior exit noise
grep -c "AUTO-CLOSED conviction"    render.log   # only non-zero if you /conviction'd something and it exited

# Thesis daemon idle on un-subbed tickers:
grep -c "Thesis monitor:"           render.log   # startup logs only
grep "SPY THESIS ALERT"             render.log   # 0 unless you /daytrade'd SPY this session

# Sub commands healthy:
grep "Subscription manager"         render.log   # should see "initialized" at startup
grep "Subscription added"           render.log   # one line per /daytrade or /conviction
grep "Subscription removed"         render.log   # one per close or auto-close
```

**Red flags:**
- `Subscription manager init failed` at startup → Redis not wired correctly; bot runs with commands reporting "not ready" (no crash, but subscription gating degrades to "no sub" for everyone — conviction events all become compact prompts)
- Compact prompts firing every 8 minutes on the same ticker → existing `conviction:{route}:{ticker}` cooldown isn't gating prompts as expected; check `_should_suppress_flow_post` still runs ahead of the sub gate
- Thesis alerts firing on a ticker you haven't `/daytrade`d → confirm `chat_id=TELEGRAM_CHAT_ID` was actually wired (grep `init_thesis_daemon` in logs for the daemon startup line)

## Full-cycle manual test

1. Wait for a conviction fire on an un-subbed ticker → receive compact prompt
2. `/conviction <ticker> <call|put>` → receive confirmation
3. Wait for next conviction re-fire same ticker → full card posts
4. Wait for opposing-direction flow on same ticker → EXIT SIGNAL posts, then `/positions` shows sub removed
5. `/daytrade SPY call` → thesis alerts for SPY start flowing
6. `/daytrade SPY close` → thesis alerts stop immediately (by next poll cycle)

## Rollback

Incremental options, lowest to highest risk:

1. **Just disable the gate, keep commands**: Comment out the Phase 3 gate block in `thesis_monitor._poll_cycle` (everything between `# Phase 3 GATE` and `continue` on the `if not _has_daytrade and not _has_real_trade:` check) → reverts to Phase 2 thesis behavior. Subscription commands remain functional but do nothing.
2. **Un-gate conviction posts**: At each of the 4 post sites in `app.py`, comment out the `elif _is_exit and not _sub_mode:` and `elif not _sub_mode and not _is_exit:` branches → un-subbed users get full cards again (pre-Phase-3 behavior).
3. **Un-SHADOW conviction trades**: In `thesis_monitor.create_conviction_trade`, change `status="SHADOW"` back to `status="OPEN"`. Conviction entries resume driving thesis alerts (original pre-Phase-3 behavior — noisier).
4. **Full revert**: Replace all 5 files with their pre-Phase-3 versions. Subscription Redis keys expire in 7 days or `redis-cli --pattern 'subscriptions:*' | xargs redis-cli DEL`.

No schema migrations, no state migrations.

## Ordering

Phase 2 must be deployed and stable first — Phase 3's post-site changes sit on top of Phase 2's `_exit_within_cd` block. Deploying Phase 3 without Phase 2 will not crash but will be structurally inconsistent (Phase 3 won't have the exit cooldown). Phase 1 (thesis alert_key cooldown) is **deprecated** by Phase 3 — skip it entirely. Bug #2 (per-snapshot dedup) and Bug #3 (dual-header) stay on the shelf until this runs for a week.
