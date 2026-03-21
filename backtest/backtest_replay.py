# backtest/backtest_replay.py
# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 backtest replay engine.
#
# Walks through historical 5-minute bars day by day, feeds each bar into your
# real ThesisMonitorEngine, and records every trade that fires.
#
# What it uses from your real bot:
#   - ThesisMonitorEngine  (the main engine, unchanged)
#   - BarStateManager      (bar ingestion, VWAP, opening ranges)
#   - EntryValidator       (setup scoring, gates)
#   - ExitPolicy           (policy-based exits)
#   - All detection logic  (breaks, retests, failed moves)
#
# What is DIFFERENT from live:
#   - get_bars_fn reads local CSV instead of calling MarketData API
#   - store_get/store_set use a plain Python dict instead of Redis
#   - _get_time_phase_ct() is patched to use bar time instead of real clock
#   - No Discord/Telegram posting
#
# Output files (written to backtest/results/):
#   trades.csv      — one row per completed trade
#   events.csv      — every alert/event fired during replay
#   summary.txt     — printed stats (also saved as text)
# ─────────────────────────────────────────────────────────────────────────────

import os
import sys
import csv
import json
import argparse
import time as _real_time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Add the repo root to sys.path so we can import your bot modules ───────────
# This file lives in backtest/, and your bot lives in the parent directory.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT   = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, REPO_ROOT)

# ── Import your real bot modules ──────────────────────────────────────────────
# These imports MUST come after the sys.path.insert above.
try:
    import thesis_monitor as _tm
    from thesis_monitor import (
        ThesisMonitorEngine,
        ThesisContext,
        ThesisLevels,
        MonitorState,
    )
    from historical_feed import HistoricalFeed
except ImportError as e:
    print(f"\nERROR: Could not import bot modules: {e}")
    print("Make sure you're running this from the repo root, or that")
    print("backtest/backtest_replay.py can see thesis_monitor.py in the parent folder.")
    sys.exit(1)


# ═════════════════════════════════════════════════════════════════════════════
# TIME PATCHING
# ─────────────────────────────────────────────────────────────────────────────
# Your engine calls _get_time_phase_ct() to decide if it's "Morning",
# "Midday", "Power Hour", etc. In live trading, this reads the real clock.
# During replay, we need it to read the bar's timestamp instead.
#
# We also patch time.monotonic() so that cooldowns between bars behave as if
# real time has passed (5 minutes per bar = 300 seconds per bar).
# ─────────────────────────────────────────────────────────────────────────────

# Mutable containers — we update these before each bar evaluation
_replay_epoch   = [0.0]   # current bar's Unix timestamp
_replay_bar_num = [0]     # increments by 1 per bar (used for monotonic)


def _patched_get_time_phase_ct() -> dict:
    """
    Replacement for thesis_monitor._get_time_phase_ct().
    Uses _replay_epoch[0] instead of the real clock.
    """
    epoch = _replay_epoch[0]
    try:
        from zoneinfo import ZoneInfo
        now = datetime.fromtimestamp(epoch, ZoneInfo("America/Chicago"))
    except ImportError:
        import pytz
        now = datetime.fromtimestamp(epoch, pytz.timezone("America/Chicago"))

    mins = now.hour * 60 + now.minute

    if mins < 510: return {"phase": "PRE_MARKET",  "label": "Pre-Market",            "favor": "wait",           "note": "Wait for open."}
    if mins < 540: return {"phase": "OPEN",        "label": "Open (first 30 min)",   "favor": "caution",        "note": "Expansion window."}
    if mins < 600: return {"phase": "MORNING",     "label": "Morning Session",        "favor": "breakout",       "note": "Breakouts more reliable."}
    if mins < 720: return {"phase": "MIDDAY",      "label": "Midday",                 "favor": "failed_move",    "note": "Chop zone."}
    if mins < 810: return {"phase": "AFTERNOON",   "label": "Afternoon",              "favor": "trend_resumption","note": "Trend resumption or reversal."}
    if mins < 870: return {"phase": "POWER_HOUR",  "label": "Power Hour",             "favor": "pin_or_expand",  "note": "GEX+ = pinning."}
    if mins < 915: return {"phase": "CLOSE",       "label": "Into Close",             "favor": "pin",            "note": "Favor pin."}
    return           {"phase": "AFTER_HOURS",      "label": "After Hours",            "favor": "wait",           "note": "Session over."}


def _patched_monotonic() -> float:
    """
    Each bar is 5 minutes = 300 seconds of 'real time'.
    Using bar_num * 300 ensures cooldowns reset correctly between bars.
    """
    return float(_replay_bar_num[0] * 300)


def _patched_time() -> float:
    """Return the current bar's epoch timestamp as 'current time'."""
    return _replay_epoch[0]


def install_time_patches():
    """
    Monkey-patch the time functions that thesis_monitor.py uses.
    Call this ONCE before starting any replay.
    """
    # Patch the time-phase function
    _tm._get_time_phase_ct = _patched_get_time_phase_ct

    # Patch time.monotonic and time.time on the module-level time object
    # that thesis_monitor imported. This affects only calls from thesis_monitor.
    _tm.time.monotonic = _patched_monotonic
    _tm.time.time      = _patched_time

    print("  Time patches installed (time_phase, monotonic, time.time)")


def set_replay_time(bar_epoch: float, bar_seq_number: int):
    """Update the replay clock to the current bar. Call before each evaluate()."""
    _replay_epoch[0]   = bar_epoch
    _replay_bar_num[0] = bar_seq_number


# ═════════════════════════════════════════════════════════════════════════════
# DICT-BASED STORE  (replaces Redis)
# ─────────────────────────────────────────────────────────────────────────────
# Your engine calls store_set(key, value, ttl=...) and store_get(key).
# In backtest mode we just use a plain Python dict.
# ─────────────────────────────────────────────────────────────────────────────

def make_dict_store():
    """Returns a (store_get, store_set) pair backed by a plain Python dict."""
    _store = {}

    def store_get(key: str) -> Optional[str]:
        return _store.get(key)

    def store_set(key: str, value: str, ttl: int = 86400):
        _store[key] = value

    return store_get, store_set


# ═════════════════════════════════════════════════════════════════════════════
# THESIS BUILDER
# ─────────────────────────────────────────────────────────────────────────────
# Builds a simple ThesisContext from the prior day's price action.
# This gives the engine real levels to watch: PDH, PDL, pivot, R1, S1.
# ─────────────────────────────────────────────────────────────────────────────

def estimate_gex_sign(prior_day: dict) -> str:
    """
    Estimate GEX sign from prior day's price behavior.

    Real GEX data isn't available in backtest mode, so we use a simple
    heuristic based on how the prior day actually moved:

    GEX NEGATIVE (dealers short gamma — they AMPLIFY moves):
        - Prior day had a large directional range (trending day)
        - Body was large relative to total range (not much wicking)
        - Price closed strongly near the high or low

    GEX POSITIVE (dealers long gamma — they DAMPEN moves):
        - Prior day had a tight range (pinning/choppy day)
        - Price closed near the middle (indecision)
        - Large wicks relative to body (dealers absorbing momentum)

    This lets the engine decide:
        - GEX negative days: confirmed breakouts fire BREAK trades
        - GEX positive days: breakouts are fades, only FAILED moves fire
    """
    pdh  = prior_day["high"]
    pdl  = prior_day["low"]
    pdc  = prior_day["close"]
    pdo  = prior_day["open"]

    total_range = pdh - pdl
    if total_range == 0:
        return "positive"

    # Body = distance between open and close
    body = abs(pdc - pdo)
    body_pct_of_range = body / total_range

    # Range as % of close — how much did it move relative to price
    range_pct = total_range / pdc * 100

    # Where did it close? 0 = at the low, 1 = at the high
    close_position = (pdc - pdl) / total_range

    # Trending day signals (GEX negative):
    #   - Range > 0.8% of price (real movement)
    #   - Body > 50% of range (not just wicks)
    #   - Closed in the top 30% or bottom 30% (strong directional close)
    trending = (
        range_pct > 0.8
        and body_pct_of_range > 0.50
        and (close_position > 0.70 or close_position < 0.30)
    )

    # Strong trend signals (very clearly GEX negative):
    strong_trend = range_pct > 1.5 and body_pct_of_range > 0.60

    if strong_trend or trending:
        return "negative"
    else:
        return "positive"


def estimate_regime(prior_day: dict) -> str:
    """Estimate market regime from prior day's range."""
    total_range = prior_day["high"] - prior_day["low"]
    range_pct   = total_range / prior_day["close"] * 100
    body        = abs(prior_day["close"] - prior_day["open"])
    body_pct    = body / prior_day["close"] * 100

    if range_pct > 1.5:
        return "HIGH_VOL_TREND"
    elif range_pct > 0.8 and body_pct > 0.4:
        return "BULL_TREND" if prior_day["close"] > prior_day["open"] else "BEAR_TREND"
    else:
        return "LOW_VOL_CHOP"


def build_auto_thesis(ticker: str, prior_day: dict, today_open: float) -> ThesisContext:
    """
    Build a ThesisContext from prior day's OHLC data.

    Levels set:
        local_resistance = prior day high (PDH)
        local_support    = prior day low  (PDL)
        pivot            = (PDH + PDL + PDC) / 3
        r1               = 2 * pivot - PDL
        s1               = 2 * pivot - PDH
        range_break_up   = PDH (alias for breakout detection)
        range_break_down = PDL (alias for breakdown detection)

    GEX sign is estimated from prior day's trending vs pinning behavior:
        - Prior day trended hard → gex_sign = "negative" → BREAK trades fire
        - Prior day chopped/pinned → gex_sign = "positive" → only FAILED trades fire

    Bias is BULLISH if today opens above prior close, else BEARISH.
    """
    pdh = prior_day["high"]
    pdl = prior_day["low"]
    pdc = prior_day["close"]

    pivot = (pdh + pdl + pdc) / 3
    r1    = 2 * pivot - pdl
    s1    = 2 * pivot - pdh

    # Gap context
    gap_pct = (today_open - pdc) / pdc * 100
    if gap_pct > 0.3:
        prior_day_context = "GAP_UP"
    elif gap_pct < -0.3:
        prior_day_context = "GAP_DOWN"
    else:
        prior_day_context = "NORMAL"

    bias = "BULLISH" if today_open >= pdc else "BEARISH"

    # Estimate GEX sign from prior day behavior
    gex_sign = estimate_gex_sign(prior_day)
    regime   = estimate_regime(prior_day)

    # Add range_break_up/down as explicit breakout trigger levels
    # (separate from local_resistance/support so the engine tracks both)
    levels = ThesisLevels(
        local_resistance  = round(pdh, 2),
        local_support     = round(pdl, 2),
        range_break_up    = round(pdh, 2),   # breakout above PDH
        range_break_down  = round(pdl, 2),   # breakdown below PDL
        pivot             = round(pivot, 2),
        r1                = round(r1, 2),
        s1                = round(s1, 2),
    )

    thesis = ThesisContext(
        ticker            = ticker,
        bias              = bias,
        bias_score        = 2 if bias == "BULLISH" else -2,
        gex_sign          = gex_sign,
        regime            = regime,
        volatility_regime = "ELEVATED" if (pdh - pdl) / pdc * 100 > 1.2 else "NORMAL",
        vix               = 20.0,
        iv                = 0.20,
        prior_day_close   = round(pdc, 2),
        prior_day_context = prior_day_context,
        session_label     = f"BT {ticker}",
        levels            = levels,
        created_at        = datetime.now().strftime("%Y-%m-%d %H:%M"),
        spot_at_creation  = today_open,
    )

    return thesis


# ═════════════════════════════════════════════════════════════════════════════
# RESULTS RECORDER
# ─────────────────────────────────────────────────────────────────────────────

TRADE_FIELDS = [
    "date", "ticker", "direction", "entry_type", "setup_score", "setup_label",
    "level_name", "level_tier", "time_phase",
    "entry_bar_time", "entry_price", "stop_level",
    "close_bar_time", "close_price", "close_reason", "status",
    "pnl_pts", "pnl_pct",
    "mae_pts", "mae_pct",   # max adverse excursion
    "mfe_pts", "mfe_pct",   # max favorable excursion
    "exit_policy", "validation_summary",
]

EVENT_FIELDS = [
    "date", "bar_time", "ticker", "bar_close",
    "event_type", "priority", "alert_key", "message",
]


class ResultsRecorder:
    """Collects trades and events during replay and writes them to CSV."""

    def __init__(self, output_dir: str):
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        self._output_dir  = output_dir
        self._trades       = []
        self._events       = []
        self._seen_trade_ids = set()  # track which trades we've already captured

    def record_events(self, events: list, date: str, bar_time: str,
                      ticker: str, bar_close: float):
        """Record all events returned by engine.evaluate()."""
        for ev in events:
            self._events.append({
                "date":        date,
                "bar_time":    bar_time,
                "ticker":      ticker,
                "bar_close":   round(bar_close, 4),
                "event_type":  ev.get("type", ""),
                "priority":    ev.get("priority", 0),
                "alert_key":   ev.get("alert_key", ""),
                "message":     ev.get("msg", "").replace("\n", " | "),
            })

    def snapshot_trades(self, engine: ThesisMonitorEngine, ticker: str,
                        date: str, bar_time: str, bar_close: float):
        """
        Check for newly completed trades after each bar.
        Records any CLOSED or INVALIDATED trades we haven't captured yet.
        """
        state = engine.get_state(ticker)
        if not state:
            return

        for trade in state.active_trades:
            if trade.trade_id in self._seen_trade_ids:
                # Already recorded — but update close info if now done
                if trade.status in ("CLOSED", "INVALIDATED"):
                    # Find existing record and update it
                    for rec in self._trades:
                        if rec.get("_trade_id") == trade.trade_id:
                            if not rec.get("close_price"):
                                self._fill_close_fields(rec, trade)
                            break
                continue

            # New trade — record it
            self._seen_trade_ids.add(trade.trade_id)
            rec = self._build_trade_record(trade, date, bar_time)
            self._trades.append(rec)

    def _build_trade_record(self, trade, date: str, bar_time: str) -> dict:
        """Build a result row from an ActiveTrade object."""
        ep = trade.entry_price or 0.0
        cp = trade.close_price

        pnl_pts = pnl_pct = mae_pts = mae_pct = mfe_pts = mfe_pct = ""
        if cp is not None:
            if trade.direction == "LONG":
                pnl_pts = round(cp - ep, 4)
                mae_pts = round(trade.min_favorable, 4)  # worst adverse excursion
                mfe_pts = round(trade.max_favorable, 4)
            else:
                pnl_pts = round(ep - cp, 4)
                mae_pts = round(trade.min_favorable, 4)
                mfe_pts = round(trade.max_favorable, 4)
            if ep > 0:
                pnl_pct = round(pnl_pts / ep * 100, 3)
                mae_pct = round(mae_pts / ep * 100, 3)
                mfe_pct = round(mfe_pts / ep * 100, 3)

        return {
            "_trade_id":       trade.trade_id,   # internal key — not written to CSV
            "date":            date,
            "ticker":          trade.ticker,
            "direction":       trade.direction,
            "entry_type":      trade.entry_type,
            "setup_score":     trade.setup_score,
            "setup_label":     trade.setup_label,
            "level_name":      trade.level_name,
            "level_tier":      trade.level_tier,
            "time_phase":      "",  # filled in by caller
            "entry_bar_time":  bar_time,
            "entry_price":     round(ep, 4),
            "stop_level":      round(trade.stop_level, 4) if trade.stop_level else "",
            "close_bar_time":  bar_time if trade.status in ("CLOSED","INVALIDATED") else "",
            "close_price":     round(cp, 4) if cp is not None else "",
            "close_reason":    trade.close_reason,
            "status":          trade.status,
            "pnl_pts":         pnl_pts,
            "pnl_pct":         pnl_pct,
            "mae_pts":         mae_pts,
            "mae_pct":         mae_pct,
            "mfe_pts":         mfe_pts,
            "mfe_pct":         mfe_pct,
            "exit_policy":     trade.exit_policy_name,
            "validation_summary": trade.validation_summary,
        }

    def _fill_close_fields(self, rec: dict, trade):
        """Update a previously recorded trade with its close data."""
        ep = rec.get("entry_price") or trade.entry_price or 0.0
        cp = trade.close_price
        if cp is None:
            return

        rec["close_price"]   = round(cp, 4)
        rec["close_reason"]  = trade.close_reason
        rec["status"]        = trade.status

        if trade.direction == "LONG":
            pnl_pts = round(cp - ep, 4)
        else:
            pnl_pts = round(ep - cp, 4)

        rec["pnl_pts"] = pnl_pts
        rec["mae_pts"] = round(trade.min_favorable, 4)
        rec["mfe_pts"] = round(trade.max_favorable, 4)

        if ep > 0:
            rec["pnl_pct"] = round(pnl_pts / ep * 100, 3)
            rec["mae_pct"] = round(trade.min_favorable / ep * 100, 3)
            rec["mfe_pct"] = round(trade.max_favorable / ep * 100, 3)

    def write(self):
        """Write trades.csv and events.csv to the output directory."""
        trades_path = os.path.join(self._output_dir, "trades.csv")
        events_path = os.path.join(self._output_dir, "events.csv")

        # Write trades (filter out internal _trade_id key)
        with open(trades_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(self._trades)

        # Write events
        with open(events_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=EVENT_FIELDS)
            writer.writeheader()
            writer.writerows(self._events)

        print(f"\n  📄 trades.csv   → {trades_path}  ({len(self._trades)} rows)")
        print(f"  📄 events.csv   → {events_path}  ({len(self._events)} rows)")
        return trades_path, events_path

    def print_summary(self, output_dir: str):
        """Print a plain-English summary of backtest results and save to summary.txt."""
        trades = [t for t in self._trades if t.get("pnl_pts") != ""]
        closed = [t for t in trades if t["status"] in ("CLOSED", "INVALIDATED")]

        lines = []
        lines.append("=" * 60)
        lines.append("  BACKTEST SUMMARY")
        lines.append("=" * 60)
        lines.append(f"  Total trades recorded:  {len(self._trades)}")
        lines.append(f"  Trades with close data: {len(closed)}")
        lines.append(f"  Total events fired:     {len(self._events)}")

        if closed:
            wins   = [t for t in closed if (t.get("pnl_pts") or 0) > 0]
            losses = [t for t in closed if (t.get("pnl_pts") or 0) < 0]
            pnls   = [float(t["pnl_pts"]) for t in closed]
            total_pnl = sum(pnls)
            win_rate  = len(wins) / len(closed) * 100 if closed else 0

            lines.append("")
            lines.append(f"  Win rate:  {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)")
            lines.append(f"  Total P&L: {total_pnl:+.2f} pts")
            if pnls:
                lines.append(f"  Avg trade: {sum(pnls)/len(pnls):+.3f} pts")
                lines.append(f"  Best:      {max(pnls):+.3f} pts")
                lines.append(f"  Worst:     {min(pnls):+.3f} pts")

            # Breakdown by entry_type
            lines.append("")
            lines.append("  By entry type:")
            for etype in ("BREAK", "FAILED", "RETEST"):
                group = [t for t in closed if t.get("entry_type") == etype]
                if group:
                    g_pnls = [float(t["pnl_pts"]) for t in group]
                    g_wins = [p for p in g_pnls if p > 0]
                    wr = len(g_wins) / len(group) * 100
                    lines.append(f"    {etype:<8}: {len(group):>3} trades, "
                                 f"{wr:.0f}% win, {sum(g_pnls):+.2f} pts total")

            # Breakdown by setup_score
            lines.append("")
            lines.append("  By setup score:")
            for score in (5, 4, 3, 2, 1):
                group = [t for t in closed if t.get("setup_score") == score]
                if group:
                    g_pnls = [float(t["pnl_pts"]) for t in group]
                    g_wins = [p for p in g_pnls if p > 0]
                    wr = len(g_wins) / len(group) * 100
                    lines.append(f"    Score {score}: {len(group):>3} trades, "
                                 f"{wr:.0f}% win, {sum(g_pnls):+.2f} pts total")

            # Breakdown by time_phase
            phases = sorted(set(t.get("time_phase", "") for t in closed if t.get("time_phase")))
            if phases:
                lines.append("")
                lines.append("  By time phase:")
                for ph in phases:
                    group = [t for t in closed if t.get("time_phase") == ph]
                    g_pnls = [float(t["pnl_pts"]) for t in group]
                    g_wins = [p for p in g_pnls if p > 0]
                    wr = len(g_wins) / len(group) * 100 if group else 0
                    lines.append(f"    {ph:<14}: {len(group):>3} trades, "
                                 f"{wr:.0f}% win, {sum(g_pnls):+.2f} pts total")

        lines.append("")
        lines.append("=" * 60)

        summary_text = "\n".join(lines)
        print(summary_text)

        # Save to file
        summary_path = os.path.join(output_dir, "summary.txt")
        with open(summary_path, "w") as f:
            f.write(summary_text + "\n")
        print(f"  📄 summary.txt  → {summary_path}")


# ═════════════════════════════════════════════════════════════════════════════
# MAIN REPLAY LOOP
# ─────────────────────────────────────────────────────────────────────────────

def run_backtest(ticker: str, csv_path: str, output_dir: str, skip_days: int = 1):
    """
    Main replay loop.

    For each trading day (after skipping the first N days for thesis seeding):
      1. Build a fresh engine with dict store + historical get_bars_fn
      2. Build an auto-thesis from prior day's OHLC
      3. Walk through each 5-minute bar
      4. Set the replay clock to bar's timestamp
      5. Call engine.evaluate(ticker, bar.close)
      6. Record events and trades

    Args:
        ticker:     Stock symbol, e.g. "SPY"
        csv_path:   Path to the downloaded bars CSV
        output_dir: Where to write results
        skip_days:  How many days to skip at the start (need prior day for thesis)
    """
    print(f"\n{'='*55}")
    print(f"  Backtest Replay: {ticker}")
    print(f"{'='*55}\n")

    # ── Load historical data ──────────────────────────────────────────────────
    feed = HistoricalFeed(csv_path)
    trading_days = feed.get_trading_days()
    print(f"  Available days: {len(trading_days)}")

    if len(trading_days) < 2:
        print("  ERROR: Need at least 2 trading days (1 for prior-day thesis, 1 to replay).")
        sys.exit(1)

    # ── Install time patches ──────────────────────────────────────────────────
    install_time_patches()

    # ── Results recorder ──────────────────────────────────────────────────────
    recorder = ResultsRecorder(output_dir)

    # ── Global bar sequence counter (for monotonic patching) ─────────────────
    global_bar_seq = [0]

    # ── Replay each day ───────────────────────────────────────────────────────
    days_to_replay = trading_days[skip_days:]  # skip first N days (no prior day available)
    print(f"  Replaying {len(days_to_replay)} days (skipping first {skip_days} for prior-day seeding)\n")

    for day in days_to_replay:
        bars = feed.get_bars_for_day(day)
        if not bars:
            print(f"  {day}: no bars, skipping")
            continue

        # ── Build prior-day thesis ────────────────────────────────────────────
        prior = feed.get_prior_day_summary(day)
        if not prior:
            print(f"  {day}: no prior day data, skipping")
            continue

        today_open = bars[0]["open"]
        thesis = build_auto_thesis(ticker, prior, today_open)

        print(f"  {day}  ({len(bars)} bars)  "
              f"PDH={prior['high']:.2f}  PDL={prior['low']:.2f}  PDC={prior['close']:.2f}  "
              f"Bias={thesis.bias}  {'GEX-' if thesis.gex_sign == 'negative' else 'GEX+'}  Regime={thesis.regime}  Context={thesis.prior_day_context}")

        # ── Create fresh engine for this day ──────────────────────────────────
        store_get, store_set = make_dict_store()

        engine = ThesisMonitorEngine(
            store_get_fn = store_get,
            store_set_fn = store_set,
            get_bars_fn  = feed.get_bars_fn,  # cursor-aware feed
        )

        # Store the thesis so the engine has levels to watch
        engine.store_thesis(ticker, thesis)

        # ── Walk through each bar ─────────────────────────────────────────────
        for bar_idx, bar in enumerate(bars):
            # Advance the feed cursor so the engine can only see bars up to here
            feed.set_cursor(day, bar_idx)

            # Advance the replay clock
            set_replay_time(bar["timestamp"], global_bar_seq[0])
            global_bar_seq[0] += 1

            bar_time = bar["datetime_ct"]

            # Get current time phase (using patched clock)
            tp = _patched_get_time_phase_ct()
            time_phase = tp["phase"]

            # Skip pre-market and after-hours bars — no trading
            if time_phase in ("PRE_MARKET", "AFTER_HOURS"):
                continue

            # ── Evaluate this bar ─────────────────────────────────────────────
            try:
                events = engine.evaluate(ticker, bar["close"])
            except Exception as e:
                print(f"    WARNING: evaluate() failed at {bar_time}: {e}")
                events = []

            # ── Record events ─────────────────────────────────────────────────
            recorder.record_events(events, day, bar_time, ticker, bar["close"])

            # ── Snapshot trades (capture entries + exits) ─────────────────────
            recorder.snapshot_trades(engine, ticker, day, bar_time, bar["close"])

            # ── Update time_phase on newly created trades ─────────────────────
            state = engine.get_state(ticker)
            if state:
                for trade in state.active_trades:
                    for rec in recorder._trades:
                        if rec.get("_trade_id") == trade.trade_id and not rec.get("time_phase"):
                            rec["time_phase"] = time_phase

        # After day ends, do a final snapshot to capture any still-open trades
        recorder.snapshot_trades(engine, ticker, day, bars[-1]["datetime_ct"], bars[-1]["close"])

        # Count trades for this day
        day_trades = [t for t in recorder._trades if t.get("date") == day]
        print(f"    → {len(day_trades)} trades fired")

    # ── Write output files ────────────────────────────────────────────────────
    print(f"\n{'─'*55}")
    print("  Writing results...")
    recorder.write()
    recorder.print_summary(output_dir)

    print(f"\n✅ Backtest complete.")
    print(f"   Results in: {output_dir}")


# ═════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Run backtest replay on historical bars")
    parser.add_argument("--ticker",  default="SPY",  help="Ticker symbol (default: SPY)")
    parser.add_argument("--data",    default=None,   help="Path to bars CSV file")
    parser.add_argument("--output",  default=None,   help="Output directory for results")
    parser.add_argument("--skip",    default=1, type=int,
                        help="Days to skip at start for prior-day seeding (default: 1)")
    args = parser.parse_args()

    # Default paths relative to this script's location
    if args.data is None:
        args.data = os.path.join(SCRIPT_DIR, "data", f"{args.ticker}_5m.csv")
    if args.output is None:
        args.output = os.path.join(SCRIPT_DIR, "results")

    if not os.path.exists(args.data):
        print(f"ERROR: Bars file not found: {args.data}")
        print("Run download_bars.py first to create this file.")
        sys.exit(1)

    run_backtest(
        ticker     = args.ticker.upper(),
        csv_path   = args.data,
        output_dir = args.output,
        skip_days  = args.skip,
    )


if __name__ == "__main__":
    main()
