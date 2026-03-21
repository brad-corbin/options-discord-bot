# backtest/backtest_replay.py
# ─────────────────────────────────────────────────────────────────────────────
# Phase 1/2 backtest replay engine.
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
#   - thesis_monitor time-of-day uses replay bar time instead of real clock
#   - exit_policy urgency uses replay bar time instead of real clock
#   - open trades are force-closed at the final bar of each session
#   - No Discord/Telegram posting
#
# Output files (written to backtest/results/):
#   trades.csv      — one row per trade (including forced EOD closes)
#   events.csv      — every alert/event fired during replay
#   summary.txt     — printed stats (also saved as text)
#
# This version adds richer trade metadata for Phase 2 reporting:
#   - regime
#   - gex_sign
#   - bias
#   - prior_day_context
#   - volatility_regime
# ─────────────────────────────────────────────────────────────────────────────

import os
import sys
import csv
import argparse
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Add the repo root to sys.path so we can import your bot modules ───────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, REPO_ROOT)

# ── Import your real bot modules ──────────────────────────────────────────────
try:
    import thesis_monitor as _tm
    import exit_policy as _ep
    from thesis_monitor import ThesisMonitorEngine, ThesisContext, ThesisLevels
    from historical_feed import HistoricalFeed
except ImportError as e:
    print(f"\nERROR: Could not import bot modules: {e}")
    print("Make sure you're running this from the repo root, or that")
    print("backtest/backtest_replay.py can see thesis_monitor.py in the parent folder.")
    sys.exit(1)


# ═════════════════════════════════════════════════════════════════════════════
# TIME PATCHING
# ═════════════════════════════════════════════════════════════════════════════

_replay_epoch = [0.0]     # current bar's Unix timestamp
_replay_bar_num = [0]     # increments by 1 per bar


def _replay_ct_now() -> datetime:
    """Return replay clock as America/Chicago datetime."""
    epoch = _replay_epoch[0]
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(epoch, ZoneInfo("America/Chicago"))
    except ImportError:
        import pytz
        return datetime.fromtimestamp(epoch, pytz.timezone("America/Chicago"))


def _patched_get_time_phase_ct() -> dict:
    """
    Replacement for thesis_monitor._get_time_phase_ct().
    Uses replay bar time instead of the real wall clock.
    """
    now = _replay_ct_now()
    mins = now.hour * 60 + now.minute

    if mins < 510:
        return {"phase": "PRE_MARKET", "label": "Pre-Market", "favor": "wait", "note": "Wait for open."}
    if mins < 540:
        return {"phase": "OPEN", "label": "Open (first 30 min)", "favor": "caution", "note": "Expansion window."}
    if mins < 600:
        return {"phase": "MORNING", "label": "Morning Session", "favor": "breakout", "note": "Breakouts more reliable."}
    if mins < 720:
        return {"phase": "MIDDAY", "label": "Midday", "favor": "failed_move", "note": "Chop zone."}
    if mins < 810:
        return {"phase": "AFTERNOON", "label": "Afternoon", "favor": "trend_resumption", "note": "Trend resumption or reversal."}
    if mins < 870:
        return {"phase": "POWER_HOUR", "label": "Power Hour", "favor": "pin_or_expand", "note": "GEX+ = pinning."}
    if mins < 915:
        return {"phase": "CLOSE", "label": "Into Close", "favor": "pin", "note": "Favor pin."}
    return {"phase": "AFTER_HOURS", "label": "After Hours", "favor": "wait", "note": "Session over."}


def _patched_monotonic() -> float:
    """
    Each bar is 5 minutes = 300 seconds of 'real time'.
    Using bar_num * 300 ensures cooldowns reset correctly between bars.
    """
    return float(_replay_bar_num[0] * 300)


def _patched_time() -> float:
    """Return the current bar's epoch timestamp as 'current time'."""
    return _replay_epoch[0]


def _patched_minutes_since_open() -> int:
    """Replacement for exit_policy._minutes_since_open() using replay clock."""
    now = _replay_ct_now()
    return now.hour * 60 + now.minute - 510  # 510 = 8:30 AM CT


def install_time_patches() -> None:
    """Patch modules that read wall-clock time so replay uses bar time."""
    _tm._get_time_phase_ct = _patched_get_time_phase_ct
    _tm.time.monotonic = _patched_monotonic
    _tm.time.time = _patched_time
    _ep._minutes_since_open = _patched_minutes_since_open
    print("  Time patches installed (time_phase, monotonic, time.time, minutes_since_open)")


def set_replay_time(bar_epoch: float, bar_seq_number: int) -> None:
    """Update the replay clock to the current bar. Call before each evaluate()."""
    _replay_epoch[0] = float(bar_epoch)
    _replay_bar_num[0] = int(bar_seq_number)


# ═════════════════════════════════════════════════════════════════════════════
# DICT-BASED STORE
# ═════════════════════════════════════════════════════════════════════════════

def make_dict_store():
    """Return (store_get, store_set) backed by a plain Python dict."""
    _store = {}

    def store_get(key: str) -> Optional[str]:
        return _store.get(key)

    def store_set(key: str, value: str, ttl: int = 86400):
        _store[key] = value

    return store_get, store_set


# ═════════════════════════════════════════════════════════════════════════════
# THESIS BUILDER
# ═════════════════════════════════════════════════════════════════════════════

def estimate_gex_sign(prior_day: dict) -> str:
    pdh = prior_day["high"]
    pdl = prior_day["low"]
    pdc = prior_day["close"]
    pdo = prior_day["open"]

    total_range = pdh - pdl
    if total_range == 0:
        return "positive"

    body = abs(pdc - pdo)
    body_pct_of_range = body / total_range
    range_pct = total_range / pdc * 100
    close_position = (pdc - pdl) / total_range

    trending = (
        range_pct > 0.8
        and body_pct_of_range > 0.50
        and (close_position > 0.70 or close_position < 0.30)
    )
    strong_trend = range_pct > 1.5 and body_pct_of_range > 0.60
    return "negative" if (strong_trend or trending) else "positive"


def estimate_regime(prior_day: dict) -> str:
    total_range = prior_day["high"] - prior_day["low"]
    range_pct = total_range / prior_day["close"] * 100
    body = abs(prior_day["close"] - prior_day["open"])
    body_pct = body / prior_day["close"] * 100

    if range_pct > 1.5:
        return "HIGH_VOL_TREND"
    if range_pct > 0.8 and body_pct > 0.4:
        return "BULL_TREND" if prior_day["close"] > prior_day["open"] else "BEAR_TREND"
    return "LOW_VOL_CHOP"


def build_auto_thesis(ticker: str, prior_day: dict, today_open: float) -> ThesisContext:
    pdh = prior_day["high"]
    pdl = prior_day["low"]
    pdc = prior_day["close"]

    pivot = (pdh + pdl + pdc) / 3
    r1 = 2 * pivot - pdl
    s1 = 2 * pivot - pdh

    gap_pct = (today_open - pdc) / pdc * 100
    if gap_pct > 0.3:
        prior_day_context = "GAP_UP"
    elif gap_pct < -0.3:
        prior_day_context = "GAP_DOWN"
    else:
        prior_day_context = "NORMAL"

    bias = "BULLISH" if today_open >= pdc else "BEARISH"
    gex_sign = estimate_gex_sign(prior_day)
    regime = estimate_regime(prior_day)

    levels = ThesisLevels(
        local_resistance=round(pdh, 2),
        local_support=round(pdl, 2),
        range_break_up=round(pdh, 2),
        range_break_down=round(pdl, 2),
        pivot=round(pivot, 2),
        r1=round(r1, 2),
        s1=round(s1, 2),
    )

    return ThesisContext(
        ticker=ticker,
        bias=bias,
        bias_score=2 if bias == "BULLISH" else -2,
        gex_sign=gex_sign,
        regime=regime,
        volatility_regime="ELEVATED" if (pdh - pdl) / pdc * 100 > 1.2 else "NORMAL",
        vix=20.0,
        iv=0.20,
        prior_day_close=round(pdc, 2),
        prior_day_context=prior_day_context,
        session_label=f"BT {ticker}",
        levels=levels,
        created_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        spot_at_creation=today_open,
    )


# ═════════════════════════════════════════════════════════════════════════════
# RESULTS RECORDER
# ═════════════════════════════════════════════════════════════════════════════

TRADE_FIELDS = [
    "date", "ticker", "direction", "entry_type", "setup_score", "setup_label",
    "level_name", "level_tier", "time_phase",
    "bias", "regime", "gex_sign", "volatility_regime", "prior_day_context",
    "entry_bar_time", "entry_price", "stop_level",
    "close_bar_time", "close_price", "close_reason", "status",
    "pnl_pts", "pnl_pct",
    "mae_pts", "mae_pct",
    "mfe_pts", "mfe_pct",
    "exit_policy", "validation_summary",
]

EVENT_FIELDS = [
    "date", "bar_time", "ticker", "bar_close",
    "event_type", "priority", "alert_key", "message",
]


class ResultsRecorder:
    """Collect trades and events during replay and write them to CSV."""

    def __init__(self, output_dir: str):
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        self._output_dir = output_dir
        self._trades = []
        self._events = []

    def record_events(self, events: list, date: str, bar_time: str, ticker: str, bar_close: float) -> None:
        for ev in events:
            self._events.append({
                "date": date,
                "bar_time": bar_time,
                "ticker": ticker,
                "bar_close": round(bar_close, 4),
                "event_type": ev.get("type", ""),
                "priority": ev.get("priority", 0),
                "alert_key": ev.get("alert_key", ""),
                "message": ev.get("msg", "").replace("\n", " | "),
            })

    def snapshot_trades(
        self,
        engine: ThesisMonitorEngine,
        ticker: str,
        date: str,
        bar_time: str,
        time_phase: str,
        session_meta: dict,
    ) -> None:
        """Capture new trades and update existing ones."""
        state = engine.get_state(ticker)
        if not state:
            return

        for trade in state.active_trades:
            existing = self._find_record(trade.trade_id)
            if existing is None:
                rec = self._build_trade_record(trade, date, bar_time, time_phase, session_meta)
                self._trades.append(rec)
            else:
                if not existing.get("time_phase"):
                    existing["time_phase"] = time_phase
                self._fill_meta(existing, session_meta)
                if trade.status in ("CLOSED", "INVALIDATED"):
                    self._fill_close_fields(existing, trade, bar_time)

    def _find_record(self, trade_id: str) -> Optional[dict]:
        for rec in self._trades:
            if rec.get("_trade_id") == trade_id:
                return rec
        return None

    def _fill_meta(self, rec: dict, session_meta: dict) -> None:
        for key, value in session_meta.items():
            if rec.get(key, "") in ("", None):
                rec[key] = value

    def _build_trade_record(self, trade, date: str, bar_time: str, time_phase: str, session_meta: dict) -> dict:
        ep = trade.entry_price or 0.0
        cp = trade.close_price

        pnl_pts = pnl_pct = mae_pts = mae_pct = mfe_pts = mfe_pct = ""
        close_bar_time = ""
        if cp is not None:
            close_bar_time = bar_time
            pnl_pts, pnl_pct, mae_pts, mae_pct, mfe_pts, mfe_pct = self._calc_performance_fields(trade, ep, cp)

        rec = {
            "_trade_id": trade.trade_id,
            "date": date,
            "ticker": trade.ticker,
            "direction": trade.direction,
            "entry_type": trade.entry_type,
            "setup_score": trade.setup_score,
            "setup_label": trade.setup_label,
            "level_name": trade.level_name,
            "level_tier": trade.level_tier,
            "time_phase": time_phase,
            "bias": session_meta.get("bias", ""),
            "regime": session_meta.get("regime", ""),
            "gex_sign": session_meta.get("gex_sign", ""),
            "volatility_regime": session_meta.get("volatility_regime", ""),
            "prior_day_context": session_meta.get("prior_day_context", ""),
            "entry_bar_time": bar_time,
            "entry_price": round(ep, 4),
            "stop_level": round(trade.stop_level, 4) if trade.stop_level is not None else "",
            "close_bar_time": close_bar_time,
            "close_price": round(cp, 4) if cp is not None else "",
            "close_reason": trade.close_reason,
            "status": trade.status,
            "pnl_pts": pnl_pts,
            "pnl_pct": pnl_pct,
            "mae_pts": mae_pts,
            "mae_pct": mae_pct,
            "mfe_pts": mfe_pts,
            "mfe_pct": mfe_pct,
            "exit_policy": trade.exit_policy_name,
            "validation_summary": trade.validation_summary,
        }
        return rec

    def _calc_performance_fields(self, trade, entry_price: float, close_price: float):
        if trade.direction == "LONG":
            pnl_pts = round(close_price - entry_price, 4)
        else:
            pnl_pts = round(entry_price - close_price, 4)

        mae_pts = round(trade.min_favorable, 4)
        mfe_pts = round(trade.max_favorable, 4)

        pnl_pct = mae_pct = mfe_pct = ""
        if entry_price > 0:
            pnl_pct = round(pnl_pts / entry_price * 100, 3)
            mae_pct = round(mae_pts / entry_price * 100, 3)
            mfe_pct = round(mfe_pts / entry_price * 100, 3)

        return pnl_pts, pnl_pct, mae_pts, mae_pct, mfe_pts, mfe_pct

    def _fill_close_fields(self, rec: dict, trade, bar_time: str) -> None:
        cp = trade.close_price
        ep = rec.get("entry_price") or trade.entry_price or 0.0
        if cp is None:
            return

        pnl_pts, pnl_pct, mae_pts, mae_pct, mfe_pts, mfe_pct = self._calc_performance_fields(trade, ep, cp)
        rec["close_bar_time"] = bar_time
        rec["close_price"] = round(cp, 4)
        rec["close_reason"] = trade.close_reason
        rec["status"] = trade.status
        rec["pnl_pts"] = pnl_pts
        rec["pnl_pct"] = pnl_pct
        rec["mae_pts"] = mae_pts
        rec["mae_pct"] = mae_pct
        rec["mfe_pts"] = mfe_pts
        rec["mfe_pct"] = mfe_pct

    def write(self):
        trades_path = os.path.join(self._output_dir, "trades.csv")
        events_path = os.path.join(self._output_dir, "events.csv")

        with open(trades_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(self._trades)

        with open(events_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=EVENT_FIELDS)
            writer.writeheader()
            writer.writerows(self._events)

        print(f"\n  📄 trades.csv   → {trades_path}  ({len(self._trades)} rows)")
        print(f"  📄 events.csv   → {events_path}  ({len(self._events)} rows)")
        return trades_path, events_path

    def _append_group_block(self, lines: list, closed: list, label: str, field: str) -> None:
        values = sorted({str(t.get(field, "")) for t in closed if str(t.get(field, "")).strip()})
        if not values:
            return
        lines.append("")
        lines.append(f"  By {label}:")
        for val in values:
            group = [t for t in closed if str(t.get(field, "")) == val]
            g_pnls = [float(t["pnl_pts"]) for t in group]
            g_wins = [p for p in g_pnls if p > 0]
            wr = len(g_wins) / len(group) * 100 if group else 0
            lines.append(f"    {val:<14}: {len(group):>3} trades, {wr:.0f}% win, {sum(g_pnls):+.2f} pts total")

    def print_summary(self, output_dir: str) -> None:
        trades_with_close = [t for t in self._trades if t.get("pnl_pts") != ""]
        closed = [t for t in trades_with_close if t["status"] in ("CLOSED", "INVALIDATED")]
        still_open = [t for t in self._trades if t.get("status") not in ("CLOSED", "INVALIDATED")]

        lines = []
        lines.append("=" * 60)
        lines.append("  BACKTEST SUMMARY")
        lines.append("=" * 60)
        lines.append(f"  Total trades recorded:  {len(self._trades)}")
        lines.append(f"  Trades with close data: {len(closed)}")
        lines.append(f"  Trades still open:      {len(still_open)}")
        lines.append(f"  Total events fired:     {len(self._events)}")

        if closed:
            wins = [t for t in closed if float(t.get("pnl_pts") or 0) > 0]
            losses = [t for t in closed if float(t.get("pnl_pts") or 0) < 0]
            pnls = [float(t["pnl_pts"]) for t in closed]
            total_pnl = sum(pnls)
            win_rate = len(wins) / len(closed) * 100 if closed else 0

            lines.append("")
            lines.append(f"  Win rate:  {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)")
            lines.append(f"  Total P&L: {total_pnl:+.2f} pts")
            lines.append(f"  Avg trade: {sum(pnls) / len(pnls):+.3f} pts")
            lines.append(f"  Best:      {max(pnls):+.3f} pts")
            lines.append(f"  Worst:     {min(pnls):+.3f} pts")

            self._append_group_block(lines, closed, "entry type", "entry_type")
            self._append_group_block(lines, closed, "setup score", "setup_score")
            self._append_group_block(lines, closed, "time phase", "time_phase")
            self._append_group_block(lines, closed, "regime", "regime")
            self._append_group_block(lines, closed, "gex_sign", "gex_sign")
            self._append_group_block(lines, closed, "bias", "bias")
            self._append_group_block(lines, closed, "prior_day_context", "prior_day_context")

        lines.append("")
        lines.append("=" * 60)

        summary_text = "\n".join(lines)
        print(summary_text)

        summary_path = os.path.join(output_dir, "summary.txt")
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write(summary_text + "\n")
        print(f"  📄 summary.txt  → {summary_path}")


# ═════════════════════════════════════════════════════════════════════════════
# REPLAY HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def close_all_open_trades(engine: ThesisMonitorEngine, ticker: str, price: float, reason: str) -> int:
    """
    Force-close every remaining open trade for this ticker.
    Uses the engine's real close_trade() helper so status and close fields are set
    the same way they are in production.
    """
    closed_count = 0
    while True:
        state = engine.get_state(ticker)
        if not state:
            break
        open_trades = [t for t in state.active_trades if t.status in ("OPEN", "SCALED", "TRAILED")]
        if not open_trades:
            break
        engine.close_trade(ticker, price=price, reason=reason)
        closed_count += 1
    return closed_count


# ═════════════════════════════════════════════════════════════════════════════
# MAIN REPLAY LOOP
# ═════════════════════════════════════════════════════════════════════════════

def run_backtest(ticker: str, csv_path: str, output_dir: str, skip_days: int = 1) -> None:
    print(f"\n{'=' * 55}")
    print(f"  Backtest Replay: {ticker}")
    print(f"{'=' * 55}\n")

    feed = HistoricalFeed(csv_path)
    trading_days = feed.get_trading_days()
    print(f"  Available days: {len(trading_days)}")

    if len(trading_days) < 2:
        print("  ERROR: Need at least 2 trading days (1 for prior-day thesis, 1 to replay).")
        sys.exit(1)

    install_time_patches()
    recorder = ResultsRecorder(output_dir)
    global_bar_seq = [0]

    days_to_replay = trading_days[skip_days:]
    print(f"  Replaying {len(days_to_replay)} days (skipping first {skip_days} for prior-day seeding)\n")

    for day in days_to_replay:
        bars = feed.get_bars_for_day(day)
        if not bars:
            print(f"  {day}: no bars, skipping")
            continue

        prior = feed.get_prior_day_summary(day)
        if not prior:
            print(f"  {day}: no prior day data, skipping")
            continue

        today_open = bars[0]["open"]
        thesis = build_auto_thesis(ticker, prior, today_open)
        session_meta = {
            "bias": thesis.bias,
            "regime": thesis.regime,
            "gex_sign": thesis.gex_sign,
            "volatility_regime": thesis.volatility_regime,
            "prior_day_context": thesis.prior_day_context,
        }

        print(
            f"  {day}  ({len(bars)} bars)  "
            f"PDH={prior['high']:.2f}  PDL={prior['low']:.2f}  PDC={prior['close']:.2f}  "
            f"Bias={thesis.bias}  {'GEX-' if thesis.gex_sign == 'negative' else 'GEX+'}  "
            f"Regime={thesis.regime}  Context={thesis.prior_day_context}"
        )

        store_get, store_set = make_dict_store()
        engine = ThesisMonitorEngine(
            store_get_fn=store_get,
            store_set_fn=store_set,
            get_bars_fn=feed.get_bars_fn,
        )
        engine.store_thesis(ticker, thesis)

        for bar_idx, bar in enumerate(bars):
            feed.set_cursor(day, bar_idx)
            set_replay_time(bar["timestamp"], global_bar_seq[0])
            global_bar_seq[0] += 1

            bar_time = bar["datetime_ct"]
            time_phase = _patched_get_time_phase_ct()["phase"]

            if time_phase in ("PRE_MARKET", "AFTER_HOURS"):
                continue

            try:
                events = engine.evaluate(ticker, bar["close"])
            except Exception as e:
                print(f"    WARNING: evaluate() failed at {bar_time}: {e}")
                events = []

            recorder.record_events(events, day, bar_time, ticker, bar["close"])
            recorder.snapshot_trades(engine, ticker, day, bar_time, time_phase, session_meta)

        last_bar = bars[-1]
        set_replay_time(last_bar["timestamp"], global_bar_seq[0])
        global_bar_seq[0] += 1
        forced = close_all_open_trades(
            engine,
            ticker,
            price=last_bar["close"],
            reason="Forced EOD close",
        )
        recorder.snapshot_trades(
            engine,
            ticker,
            day,
            last_bar["datetime_ct"],
            _patched_get_time_phase_ct()["phase"],
            session_meta,
        )

        day_trades = [t for t in recorder._trades if t.get("date") == day]
        if forced:
            print(f"    → {len(day_trades)} trades fired ({forced} forced EOD closes)")
        else:
            print(f"    → {len(day_trades)} trades fired")

    print(f"\n{'─' * 55}")
    print("  Writing results...")
    recorder.write()
    recorder.print_summary(output_dir)

    print("\n✅ Backtest complete.")
    print(f"   Results in: {output_dir}")


# ═════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="Run backtest replay on historical bars")
    parser.add_argument("--ticker", default="SPY", help="Ticker symbol (default: SPY)")
    parser.add_argument("--data", default=None, help="Path to bars CSV file")
    parser.add_argument("--output", default=None, help="Output directory for results")
    parser.add_argument(
        "--skip",
        default=1,
        type=int,
        help="Days to skip at start for prior-day seeding (default: 1)",
    )
    args = parser.parse_args()

    if args.data is None:
        args.data = os.path.join(SCRIPT_DIR, "data", f"{args.ticker}_5m.csv")
    if args.output is None:
        args.output = os.path.join(SCRIPT_DIR, "results")

    if not os.path.exists(args.data):
        print(f"ERROR: Bars file not found: {args.data}")
        print("Run download_bars.py first to create this file.")
        sys.exit(1)

    run_backtest(
        ticker=args.ticker.upper(),
        csv_path=args.data,
        output_dir=args.output,
        skip_days=args.skip,
    )


if __name__ == "__main__":
    main()
