# Phase 3 Deployment — Subscription-Gated Alert Model

## Summary

Shift from implicit alert delivery (fire for every ticker) to an explicit subscription model with two distinct modes:

- **`/daytrade <ticker> <call|put>`** — Full monitoring: thesis alerts, trade cards, conviction, entries, exits, trade mgmt. For tickers you're actively trading intraday.
- **`/conviction <ticker> <call|put>`** — Flow-only: conviction re-fires and exit signals. Silent on technical momentum/level/gamma thesis alerts. For riding longer-dated institutional positioning.

Without a subscription, tickers are silent. Conviction flow still fires as a **compact prompt** inviting you to subscribe.

**Does not touch:** `active_scanner.py` (verified zero imports/dependencies on thesis_monitor, oi_flow, ActiveTrade, or subscription state).

**`SHADOW` ActiveTrade status** is added and used purely for **Google Sheets edge tracking** — conviction plays always create SHADOW entries regardless of subscription, so the system logs its own institutional-flow performance independently of whether the user acts on it. SHADOW does not drive any alerts; only subscriptions (and real OPEN trades from other paths) do.

## Behavior matrix

| Event | No sub | `/conviction` sub | `/daytrade` sub |
|---|---|---|---|
| Conviction flow (new, ticker X, dir D) | Compact prompt with DTE tag | Full conviction card | Full conviction card |
| Conviction re-fire (same X, D) | Compact prompt (rate-limited) | Full card | Full card |
| EXIT SIGNAL (flip on X) | Silent | Posts + **auto-closes conviction sub** | Posts (sub stays open) |
| THESIS ALERT (momentum fade, level break, gamma flip) | Silent | **Silent** (flow-only mode) | Posts |
| TRADE ALERT (entry signal from technicals) | Silent | Silent | Posts |
| TRADE MGMT (scale/trail/exit of real trade) | Silent | Silent | Posts |

## Files touched

- `thesis_monitor.py` — add `SHADOW` status, rewrite `_poll_cycle` gate, modify `create_conviction_trade`
- `oi_flow.py` — add `signal_lag_sec` field (Phase 4 rider)
- `app.py` — subscription Redis helpers, compact-prompt formatter, gate conviction post sites, exit-signal auto-close
- `telegram_commands.py` — register `/daytrade`, `/conviction`, `/positions` handlers
- New file: `subscriptions.py` — subscription state manager (small module)

Total change: ~300 LOC, 1 new file.

---

## Redis subscription schema

Single key pattern:

```
subscriptions:{chat_id}:{ticker}:{direction}
```

Value (JSON):
```json
{
  "mode": "daytrade" | "conviction",
  "created_at": "2026-04-20T14:32:11",
  "source": "manual" | "flow_prompt",
  "source_dte": 14,               // conviction only: DTE of triggering signal
  "source_expiry": "2026-05-04",  // conviction only: expiry of triggering contract
  "source_notional": 31300000     // conviction only: notional that triggered
}
```

**TTL:** 7 days rolling (keys expire if not touched; subscription manager refreshes on use). Handles forgotten subscriptions without explicit cleanup.

**Precedence rule:** if both `daytrade` and `conviction` keys exist for the same (ticker, direction), `daytrade` wins (more permissive). In practice, users upgrading conviction → daytrade just write a new key with `mode=daytrade`; the conviction row can stay or be overwritten.

---

## New file: `subscriptions.py`

```python
"""Subscription state manager for /daytrade and /conviction modes.

Persists to Redis via persistent_state. Single-user bot — chat_id keys
the subscription set.
"""
import json
import logging
from datetime import datetime
from typing import Optional, Literal, List

log = logging.getLogger(__name__)

SubMode = Literal["daytrade", "conviction"]
Direction = Literal["call", "put"]

SUB_TTL_SEC = 7 * 24 * 3600   # 7 days rolling


def _sub_key(chat_id: str, ticker: str, direction: str) -> str:
    return f"subscriptions:{chat_id}:{ticker.upper()}:{direction.lower()}"


def _prefix(chat_id: str) -> str:
    return f"subscriptions:{chat_id}:"


class SubscriptionManager:
    def __init__(self, state):
        """state must expose _json_get, _json_set, _redis (for scan)."""
        self._state = state

    def add(self, chat_id: str, ticker: str, direction: str,
            mode: SubMode, source: str = "manual",
            source_dte: Optional[int] = None,
            source_expiry: Optional[str] = None,
            source_notional: Optional[float] = None) -> dict:
        """Add or replace a subscription. Returns the stored record."""
        key = _sub_key(chat_id, ticker, direction)
        rec = {
            "mode": mode,
            "created_at": datetime.now().isoformat(),
            "source": source,
            "ticker": ticker.upper(),
            "direction": direction.lower(),
        }
        if source_dte is not None:
            rec["source_dte"] = source_dte
        if source_expiry:
            rec["source_expiry"] = source_expiry
        if source_notional is not None:
            rec["source_notional"] = source_notional

        self._state._json_set(key, rec, ttl=SUB_TTL_SEC)
        log.info(f"Subscription added: {chat_id} {ticker} {direction} "
                 f"mode={mode} source={source}")
        return rec

    def remove(self, chat_id: str, ticker: str,
               direction: Optional[str] = None) -> int:
        """Remove subscriptions for ticker. If direction=None, removes both
        directions. Returns count removed."""
        removed = 0
        dirs = [direction] if direction else ["call", "put"]
        for d in dirs:
            key = _sub_key(chat_id, ticker, d)
            try:
                if self._state._redis.delete(key):
                    removed += 1
            except Exception as e:
                log.warning(f"Sub remove failed for {key}: {e}")
        log.info(f"Subscription removed: {chat_id} {ticker} "
                 f"dir={direction or 'all'} count={removed}")
        return removed

    def get(self, chat_id: str, ticker: str, direction: str) -> Optional[dict]:
        """Get specific subscription record. None if not subscribed."""
        key = _sub_key(chat_id, ticker, direction)
        try:
            return self._state._json_get(key)
        except Exception:
            return None

    def mode_for(self, chat_id: str, ticker: str,
                 direction: str) -> Optional[SubMode]:
        """Returns 'daytrade', 'conviction', or None. Daytrade wins if both."""
        # Check daytrade key first (precedence)
        rec = self.get(chat_id, ticker, direction)
        if rec:
            return rec.get("mode")
        return None

    def has_daytrade(self, chat_id: str, ticker: str) -> bool:
        """Any daytrade sub on this ticker (either direction)?"""
        for d in ("call", "put"):
            rec = self.get(chat_id, ticker, d)
            if rec and rec.get("mode") == "daytrade":
                return True
        return False

    def has_any(self, chat_id: str, ticker: str, direction: str) -> bool:
        """Any sub (daytrade or conviction) for (ticker, direction)?"""
        return self.get(chat_id, ticker, direction) is not None

    def list_all(self, chat_id: str) -> List[dict]:
        """List all active subscriptions for chat_id, sorted by created_at."""
        prefix = _prefix(chat_id)
        results = []
        try:
            for key in self._state._redis.scan_iter(match=f"{prefix}*"):
                rec = self._state._json_get(key if isinstance(key, str)
                                             else key.decode())
                if rec:
                    results.append(rec)
        except Exception as e:
            log.warning(f"Sub list failed: {e}")
        results.sort(key=lambda r: r.get("created_at", ""))
        return results


_sub_manager: Optional[SubscriptionManager] = None


def init_subscription_manager(state) -> SubscriptionManager:
    global _sub_manager
    _sub_manager = SubscriptionManager(state)
    log.info("Subscription manager initialized")
    return _sub_manager


def get_subscription_manager() -> Optional[SubscriptionManager]:
    return _sub_manager
```

---

## Telegram commands

**`telegram_commands.py` — add handlers** (pattern matches existing `/hold`, `/portfolio` etc. near line 238+):

```python
# ─────────────────────────────────────
# SUBSCRIPTION COMMANDS (Phase 3)
# ─────────────────────────────────────

if cmd in ("/daytrade", "/daytrade@omegabot"):
    from subscriptions import get_subscription_manager
    sm = get_subscription_manager()
    if not sm:
        reply("Subscription manager not ready. Try again in a moment.")
        return
    if len(args) < 2:
        reply("Usage: /daytrade <TICKER> <call|put|close>")
        return
    ticker = args[0].upper()
    action = args[1].lower()
    if action == "close":
        n = sm.remove(chat_id, ticker)
        reply(f"Closed {n} daytrade subscription(s) for {ticker}" if n
              else f"No active daytrade sub for {ticker}")
        return
    if action not in ("call", "put"):
        reply("Direction must be 'call', 'put', or 'close'")
        return
    sm.add(chat_id, ticker, action, mode="daytrade", source="manual")
    reply(f"📡 Daytrade active: {ticker} {action.upper()}\n"
          f"You'll receive thesis alerts, trade cards, flow conviction, "
          f"and exits for {ticker}. Stop with /daytrade {ticker} close")
    return

if cmd in ("/conviction", "/conviction@omegabot"):
    from subscriptions import get_subscription_manager
    sm = get_subscription_manager()
    if not sm:
        reply("Subscription manager not ready. Try again in a moment.")
        return
    if len(args) < 2:
        reply("Usage: /conviction <TICKER> <call|put|close>")
        return
    ticker = args[0].upper()
    action = args[1].lower()
    if action == "close":
        n = sm.remove(chat_id, ticker)
        reply(f"Closed {n} conviction subscription(s) for {ticker}" if n
              else f"No active conviction sub for {ticker}")
        return
    if action not in ("call", "put"):
        reply("Direction must be 'call', 'put', or 'close'")
        return
    sm.add(chat_id, ticker, action, mode="conviction", source="manual")
    reply(f"💎 Conviction tracking: {ticker} {action.upper()}\n"
          f"You'll receive conviction re-fires and exit signals. "
          f"Technical thesis alerts stay silent. Auto-closes on exit signal. "
          f"Stop with /conviction {ticker} close")
    return

if cmd in ("/positions", "/positions@omegabot"):
    from subscriptions import get_subscription_manager
    sm = get_subscription_manager()
    if not sm:
        reply("Subscription manager not ready.")
        return
    subs = sm.list_all(chat_id)
    if not subs:
        reply("No active subscriptions.\n"
              "Use /daytrade <TICKER> <call|put> or /conviction <TICKER> <call|put>")
        return
    lines = ["📋 Active subscriptions:"]
    for s in subs:
        tag = "📡" if s["mode"] == "daytrade" else "💎"
        line = f"  {tag} {s['ticker']} {s['direction'].upper()} ({s['mode']})"
        if s.get("source_expiry"):
            line += f" — exp {s['source_expiry']} ({s.get('source_dte','?')}DTE)"
        lines.append(line)
    reply("\n".join(lines))
    return
```

**Wire into app.py init** (near other subsystem inits):
```python
from subscriptions import init_subscription_manager
_subscription_manager = init_subscription_manager(_persistent_state)
```

---

## ActiveTrade SHADOW status

**`thesis_monitor.py` line 226** — update the status docstring (informational):
```python
# was:
status: str = "OPEN"  # OPEN / SCALED / TRAILED / CLOSED / INVALIDATED
# becomes:
status: str = "OPEN"  # SHADOW / OPEN / SCALED / TRAILED / CLOSED / INVALIDATED
```

**`thesis_monitor.py:1625` — modify `create_conviction_trade`:**

Find:
```python
        trade = ActiveTrade(
            ticker=ticker,
            direction=direction,
            entry_type="CONVICTION",
            entry_price=spot,
            ...
            status="OPEN",
```

Replace with:
```python
        # Phase 3: always SHADOW. Conviction trades are tracked internally
        # for Google Sheets edge analysis — they are NOT the user's position.
        # The user's actual positions come from /hold, portfolio entries,
        # and other manual trade paths, which remain status="OPEN".
        # Thesis daemon gates on subscription presence (not on trade status)
        # so SHADOW trades silently feed stats without driving any alerts.
        trade = ActiveTrade(
            ticker=ticker,
            direction=direction,
            entry_type="CONVICTION",
            entry_price=spot,
            ...
            status="SHADOW",
```

**Every check of `status in ("OPEN", "SCALED", "TRAILED")`** throughout `thesis_monitor.py` (14 sites) **stays as-is**. Because `SHADOW` isn't in that tuple, shadow trades are naturally excluded from all active-trade logic. **Do not modify these lines** — the design relies on SHADOW being a separate value.

**No promotion logic needed.** SHADOW and OPEN track different things:

- **SHADOW** = system-generated tracking entry. Feeds Google Sheets, measures system's own edge on institutional flow. Invisible to the user's alert flow.
- **OPEN** = user's actual position (from `/hold`, portfolio entries, manual commands). Drives thesis alerts when combined with an active subscription.

When the user `/daytrade SPY call`s without a prior position, the subscription alone enables thesis evaluation. There's no SHADOW to promote; subscription is sufficient.

---

## Thesis monitor gating rewrite

**`thesis_monitor.py:3928-3960`** — replace the entire block:

Find:
```python
        slow = (self._cycle_count % self._slow_n == 0)
        for ticker in self.engine.get_monitored_tickers():
            fast = ticker.upper() in MONITOR_FAST_POLL_TICKERS
            if not fast and not slow: continue
            thesis = self.engine.get_thesis(ticker)

            # v8.3.2 Fix (post-review): For non-SPY/QQQ tickers, only evaluate
            # when an active trade exists. ...
            if not fast:
                state = self.engine.get_state(ticker)
                _has_active = (state and any(
                    t.status in ("OPEN", "SCALED", "TRAILED")
                    for t in state.active_trades))
                if not _has_active:
                    continue

            # Issue 1: Don't skip tickers without thesis if they have active trades
            if not thesis:
                state = self.engine.get_state(ticker)
                has_active = state and any(
                    t.status in ("OPEN", "SCALED", "TRAILED")
                    for t in state.active_trades)
                if not has_active:
                    continue
            try:
                price = self.get_spot(ticker)
                if not price or price <= 0: continue
                for ev in self.engine.evaluate(ticker, price):
                    if ev.get("priority", 1) >= 4: self._post_alert(ticker, price, ev)
                    else: log.info(f"Monitor [{ticker}]: {ev.get('msg','')}")
            except Exception as e: log.warning(f"Monitor {ticker} failed: {e}")
```

Replace with:
```python
        slow = (self._cycle_count % self._slow_n == 0)
        
        # Phase 3: subscription-gated evaluation
        try:
            from subscriptions import get_subscription_manager
            _sm = get_subscription_manager()
        except Exception:
            _sm = None
        
        for ticker in self.engine.get_monitored_tickers():
            fast = ticker.upper() in MONITOR_FAST_POLL_TICKERS
            if not fast and not slow: continue
            
            # ── Phase 3 GATE: thesis daemon evaluates a ticker only if:
            #    (a) user has a /daytrade subscription on this ticker, OR
            #    (b) a real (non-SHADOW) OPEN trade exists
            # /conviction subscriptions do NOT enable thesis evaluation —
            # they're flow-only by design. SHADOW trades don't count either.
            _has_daytrade = (_sm and _sm.has_daytrade(TELEGRAM_CHAT_ID, ticker))
            _has_real_trade = False
            state = self.engine.get_state(ticker)
            if state:
                _has_real_trade = any(
                    t.status in ("OPEN", "SCALED", "TRAILED")
                    for t in state.active_trades
                )
            if not _has_daytrade and not _has_real_trade:
                continue
            
            thesis = self.engine.get_thesis(ticker)
            if not thesis and not _has_real_trade:
                # No thesis AND no real trade — can't evaluate
                continue
            
            try:
                price = self.get_spot(ticker)
                if not price or price <= 0: continue
                for ev in self.engine.evaluate(ticker, price):
                    if ev.get("priority", 1) >= 4: self._post_alert(ticker, price, ev)
                    else: log.info(f"Monitor [{ticker}]: {ev.get('msg','')}")
            except Exception as e: log.warning(f"Monitor {ticker} failed: {e}")
```

**Import `TELEGRAM_CHAT_ID`** at the top of `thesis_monitor.py` if not already present. If environment variable access isn't clean from this module, pass `chat_id` via the `ThesisMonitorDaemon.__init__` signature:
```python
def __init__(self, engine, get_spot_fn, post_fn, chat_id=""):
    self.chat_id = chat_id
    ...
```
and store it on `self`. Then use `self.chat_id` in the gate check.

---

## Conviction post gate (app.py — three sites)

Each conviction post site (~5945, ~7642, ~11767 in `app.py`) needs gating by subscription mode. Pattern:

**Find (at the post decision point):**
```python
                        if _is_shadow:
                            log.info(f"🔇 SHADOW: ...")
                        elif _is_phantom_exit:
                            log.info(f"🔇 EXIT SIGNAL SUPPRESSED: ...")
                        elif _exit_within_cd:
                            log.info(f"🔇 EXIT CD: ...")
                        elif route == "immediate":
                            _cp_chat = TELEGRAM_CHAT_INTRADAY or TELEGRAM_CHAT_ID
                            _tg_rate_limited_post(msg, chat_id=_cp_chat)
                            ...
```

**Replace with:**
```python
                        # Phase 3: subscription-based gating
                        _sub_mode = None
                        _sub_ticker = cp["ticker"]
                        _sub_dir = "call" if cp.get("trade_direction") == "bullish" else "put"
                        try:
                            from subscriptions import get_subscription_manager
                            _sm = get_subscription_manager()
                            if _sm:
                                _sub_mode = _sm.mode_for(
                                    TELEGRAM_CHAT_INTRADAY or TELEGRAM_CHAT_ID,
                                    _sub_ticker, _sub_dir
                                )
                        except Exception as _se:
                            log.debug(f"Sub check failed for {_sub_ticker}: {_se}")
                        
                        _is_exit = cp.get("is_exit_signal", False)
                        
                        if _is_shadow:
                            log.info(f"🔇 SHADOW: ...")
                        elif _is_phantom_exit:
                            log.info(f"🔇 EXIT SIGNAL SUPPRESSED: ...")
                        elif _exit_within_cd:
                            log.info(f"🔇 EXIT CD: ...")
                        elif _is_exit and not _sub_mode:
                            # Exit for un-subscribed ticker = noise
                            log.info(f"🔇 EXIT SIGNAL (un-subbed): {cp['ticker']} "
                                     f"— user not subscribed, suppressing")
                        elif not _sub_mode and not _is_exit:
                            # Un-subscribed new conviction — compact prompt
                            _compact = _build_compact_conviction_prompt(cp)
                            _cp_chat = TELEGRAM_CHAT_INTRADAY or TELEGRAM_CHAT_ID
                            _tg_rate_limited_post(_compact, chat_id=_cp_chat)
                            log.info(f"💎 COMPACT PROMPT: {cp['ticker']} {_sub_dir} "
                                     f"({cp.get('dte')}DTE, ${cp.get('notional',0)/1e6:.1f}M)")
                        elif route == "immediate":
                            # Subscribed — full card (existing logic)
                            _cp_chat = TELEGRAM_CHAT_INTRADAY or TELEGRAM_CHAT_ID
                            _tg_rate_limited_post(msg, chat_id=_cp_chat)
                            # ...rest of existing immediate-route logic...
                            
                            # Exit signal → auto-close /conviction sub
                            if _is_exit and _sub_mode == "conviction":
                                try:
                                    _sm.remove(TELEGRAM_CHAT_INTRADAY or TELEGRAM_CHAT_ID,
                                               _sub_ticker, _sub_dir)
                                    log.info(f"🔄 AUTO-CLOSED conviction sub on exit: "
                                             f"{_sub_ticker} {_sub_dir}")
                                except Exception:
                                    pass
```

Apply the same structural change to the parallel blocks at ~7642 and ~11767. Hoist the `_sub_mode` check and the compact-prompt branch; auto-close on exit stays identical.

---

## Compact prompt format

**`app.py`** — add helper near other card formatters:

```python
def _build_compact_conviction_prompt(cp: dict) -> str:
    """Phase 3: compact 'subscribe to receive more' prompt for un-subscribed
    tickers when a conviction event fires. Includes DTE tag to help user
    judge whether to subscribe — longer-dated = forward-looking institutional
    positioning, shorter-dated = reactive/hedging."""
    ticker = cp.get("ticker", "?")
    direction = "call" if cp.get("trade_direction") == "bullish" else "put"
    dir_emoji = "📗" if direction == "call" else "📕"
    dte = cp.get("dte", 0)
    expiry = str(cp.get("expiry", ""))[:10]
    notional = cp.get("notional", 0)
    
    # Notional label
    if notional >= 1_000_000:
        notional_str = f"${notional/1e6:.1f}M"
    elif notional >= 1_000:
        notional_str = f"${notional/1e3:.0f}K"
    else:
        notional_str = f"${notional:.0f}"
    
    # DTE horizon tag — emphasizes forward-looking character on longer-dated
    if dte <= 2:
        horizon_tag = "IMMEDIATE — intraday horizon (hedging/scalping territory)"
    elif dte <= 7:
        horizon_tag = "SHORT-TERM — weekly thesis"
    elif dte <= 30:
        horizon_tag = "INSTITUTIONAL POSITIONING — forward-looking"
    else:
        horizon_tag = "INSTITUTIONAL CAMPAIGN — slow build, multi-week thesis"
    
    lines = [
        f"💎 {ticker} flow conviction — {direction.upper()} {dir_emoji} {notional_str} notional",
        f"   Expiry: {expiry} ({dte}DTE) — {horizon_tag}",
        f"",
        f"Reply /conviction {ticker} {direction} to track this flow thesis",
        f"Reply /daytrade {ticker} {direction} for full intraday monitoring",
    ]
    return "\n".join(lines)
```

**Rate-limiting** for compact prompts specifically — use existing `conviction:{route}:{ticker}` cooldown (already in `detect_conviction_plays`). One compact prompt per ticker per route per 5 min is the right density. No new cooldown needed.

---

## Phase 4 latency field (bundled in)

**`oi_flow.py`** — add `signal_lag_sec` to the play dict in `detect_conviction_plays` (near line 2450, the `play = {...}` construction):

```python
            # Phase 4: latency instrumentation
            _signal_lag_sec = None
            try:
                # Flow detected_ts is the alert creation time.
                # Move start: the earliest bar in the last ~30 min where price
                # started moving in the flow direction by >0.3%.
                _alert_ts_iso = alert.get("timestamp")
                if _alert_ts_iso:
                    _alert_ts = datetime.fromisoformat(_alert_ts_iso).timestamp()
                    _move_start = self._detect_move_start(
                        ticker, trade_direction, lookback_min=30
                    )
                    if _move_start:
                        _signal_lag_sec = max(0, int(_alert_ts - _move_start))
            except Exception:
                pass
            
            play = {
                ...
                "signal_lag_sec": _signal_lag_sec,  # None if indeterminate
            }
```

**Add method on FlowDetector:**
```python
def _detect_move_start(self, ticker: str, direction: str,
                       lookback_min: int = 30) -> Optional[float]:
    """Return epoch ts of when price started moving in `direction` by >0.3%.
    None if move can't be isolated (chop, no recent movement)."""
    try:
        from schwab_stream import get_intraday_bars
        bars = get_intraday_bars(ticker, count=lookback_min)
        if not bars or len(bars) < 5:
            return None
        # Walk back from current bar; find first bar where price was 0.3%
        # away in opposite direction (i.e., where move originated)
        sign = 1 if direction == "bullish" else -1
        current_px = bars[-1]["close"]
        for bar in reversed(bars[:-1]):
            px = bar["close"]
            pct = ((current_px - px) / px) * 100 * sign
            if pct >= 0.3:
                return bar["timestamp"]  # epoch
        return None
    except Exception:
        return None
```

**Log line** when lag is significant, added to the existing conviction log at app.py ~6075:
```python
                        _lag = cp.get("signal_lag_sec")
                        _lag_str = f" LAG={_lag}s" if _lag and _lag > 60 else ""
                        log.info(f"💎 CONVICTION [{route.upper()}]{_lag_str}...")
```

This adds visibility, no behavior change.

---

## Verification

**Pre-deploy:** sanity-check Redis connection handles scan_iter (subscription list). Test:
```python
from subscriptions import init_subscription_manager, get_subscription_manager
init_subscription_manager(_persistent_state)
sm = get_subscription_manager()
sm.add("test_chat", "SPY", "call", mode="daytrade")
assert sm.has_daytrade("test_chat", "SPY")
sm.remove("test_chat", "SPY")
```

**Post-deploy:** 

First session:
```bash
# Confirm subscription gate is firing silences:
grep -c "COMPACT PROMPT"        render.log   # should be non-zero when flow fires on un-subbed tickers
grep -c "EXIT SIGNAL (un-subbed)" render.log # should equal former exit noise
grep -c "Monitor \[SPY\]"       render.log   # should be ~0 if you haven't /daytrade'd SPY

# Confirm thesis daemon idle:
grep "Thesis monitor:" render.log   # startup logs OK
grep "SPY THESIS ALERT" render.log  # should be 0 unless you /daytrade'd SPY
```

Test the full cycle:
1. Wait for conviction flow to fire on an un-subbed ticker → receive compact prompt
2. Reply `/conviction <ticker> <dir>` → receive confirmation
3. Next conviction re-fire on same ticker → full card
4. Wait for opposing direction flow → EXIT SIGNAL posts → sub auto-closes (check `/positions`)
5. `/daytrade SPY call` → thesis alerts for SPY start flowing
6. `/daytrade SPY close` → thesis alerts stop

---

## Rollback

The subscription gate can be disabled without deleting code:
1. Comment out the Phase 3 gate block in `_poll_cycle` → reverts to v8.3.2 behavior
2. Comment out the `_sub_mode` branch in app.py conviction posts → reverts to unconditional posts
3. `SHADOW` status: search-and-replace back to `"OPEN"` in `create_conviction_trade` — shadow trades become real trades, thesis daemon fires on them again (original buggy behavior)
4. Subscription Redis keys can be left in place (TTL expires in 7 days) or explicitly flushed: `redis-cli --pattern 'subscriptions:*' | xargs redis-cli DEL`

Telegram commands (`/daytrade`, `/conviction`, `/positions`) can stay registered harmlessly even if the gating is disabled — they just won't have any effect on alert behavior.

No data migrations, no schema changes to existing tables.

---

## Ordering relative to Phase 1/2

Deploy order is independent — Phase 1, 2, and 3 touch disjoint code paths. Recommended sequence:

1. **Phase 2** first (conviction quality gates + sweep fix + bug #1/#4) — lowest risk, highest immediate signal-to-noise win on the conviction side. Lets you evaluate Phase 3 against a clean baseline.
2. **Phase 3** second — the architectural shift. Ships after Phase 2 has stabilized for a session or two.
3. **Phase 1** (thesis_monitor per-`alert_key` cooldown) — **skip entirely** once Phase 3 is in place. Phase 3's subscription gate subsumes Phase 1's dedup: if you're subscribed to SPY, you want the alerts; if you're not, they don't fire at all. The alert_key cooldown was only necessary because everything fired unconditionally. With subscription gating, the remaining alerts are small enough in volume that per-`alert_key` dedup is overkill.

---

## Not touched by this PR

- **`active_scanner.py`** — verified zero dependencies on thesis_monitor, oi_flow, ActiveTrade, or subscription state. Continues to fire as-is through `enqueue_fn` → main channel. Its own `_signal_dedup` is unaffected.
- **`income_scanner.py`**, **`potter_box.py`**, **`swing_scanner.py`** — not in the alert pipeline this PR touches.
- **Portfolio commands** (`/hold`, `/portfolio`, etc.) — unchanged.
- **Phase 1 (thesis alert_key cooldown)** — deprecated by Phase 3.
- **Phase 2 (conviction gates A-D, bug fixes)** — independent; ship separately.
