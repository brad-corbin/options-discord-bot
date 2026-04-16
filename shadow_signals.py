# shadow_signals.py
# ═══════════════════════════════════════════════════════════════════
# Phase 2 — Shadow Signal Logger + Impact Analyzer
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Wraps options_skew, vwap_bands, and gap_analyzer. Computes all three
# signals on every trade post, logs what confidence delta each WOULD
# HAVE applied, but does NOT modify the actual confidence score. This
# lets you collect 2-4 weeks of shadow data, then join it against
# closed-trade outcomes to verify each signal's edge before flipping
# any of them live.
#
# Storage key: omega:shadow_signals:<spread_id>
# Join key: spread_id (same key trade_journal.log_trade_close uses)
#
# Integration:
#   from shadow_signals import compute_and_log_shadow
#
#   # At the point record_recommendation is called in app.py:
#   compute_and_log_shadow(
#       persistent_state=_persistent_state,
#       spread_id=_spread_id,
#       trade_id=_trade_id,
#       ticker=ticker,
#       direction=direction,
#       contracts=chain_contracts,  # the chain used for scoring
#       spot=spot,
#       intraday_bars=recent_5m_bars,  # from your active_scanner cache
#       prior_day_data=prior_day_ohlc,  # daily candle or None
#       first_bars_of_session=first_bars,  # first 3 × 5-min bars or None
#   )
# ═══════════════════════════════════════════════════════════════════

import json
import time
import math
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Tuple
from collections import defaultdict

log = logging.getLogger(__name__)


STATE_KEY_PREFIX = "omega:shadow_signals:"

# Morning-window cutoff for gap analyzer (minutes since 8:30 CT open)
GAP_ANALYZER_MAX_MINUTES_SINCE_OPEN = 75


# ─────────────────────────────────────────────────────────
# CORE SHADOW COMPUTE + LOG
# ─────────────────────────────────────────────────────────

def compute_and_log_shadow(
    persistent_state,
    spread_id: str,
    trade_id: str,
    ticker: str,
    direction: str,
    contracts: Optional[List[Dict]] = None,
    spot: Optional[float] = None,
    intraday_bars: Optional[List[Dict]] = None,
    prior_day_data: Optional[Dict] = None,
    first_bars_of_session: Optional[List[Dict]] = None,
    minutes_since_open: Optional[int] = None,
) -> Dict:
    """Compute shadow signals and persist them keyed by spread_id.

    Returns the computed record. Callers should pass as much context as
    they have; each signal gracefully degrades if its inputs are missing.

    Args:
        persistent_state: PersistentState instance with .set(key, value)
        spread_id:       join key matching trade_journal's spread_id
        trade_id:        full trade identifier (spread_id + timestamp)
        ticker:          underlying symbol
        direction:       "bull" or "bear"
        contracts:       option chain (for skew). Each dict should have
                         right, strike, delta, iv
        spot:            current underlying price
        intraday_bars:   list of 5-min bars for VWAP bands. Each dict
                         should have high, low, close, volume
        prior_day_data:  {"high": ..., "low": ..., "close": ...} for gap
        first_bars_of_session: first 3 × 5-min bars for gap follow-through
        minutes_since_open: if None, will be computed from wall clock
    """
    ts = time.time()

    skew_result = _safe_compute_skew(contracts, spot, ticker, direction)
    vwap_result = _safe_compute_vwap_bands(intraday_bars, direction)
    gap_result = _safe_compute_gap(
        prior_day_data, spot, direction,
        first_bars_of_session, minutes_since_open,
    )

    shadow_delta_total = (
        skew_result.get("delta", 0)
        + vwap_result.get("delta", 0)
        + gap_result.get("delta", 0)
    )

    record = {
        "spread_id": spread_id,
        "trade_id": trade_id,
        "ticker": ticker.upper(),
        "direction": direction,
        "ts": ts,
        "iso_ts": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
        "skew": skew_result,
        "vwap": vwap_result,
        "gap": gap_result,
        "shadow_delta_total": shadow_delta_total,
    }

    # Persist
    try:
        key = f"{STATE_KEY_PREFIX}{spread_id}"
        persistent_state.set(key, json.dumps(record))
        log.info(
            f"Shadow signals logged: {ticker} {direction} "
            f"skew={skew_result.get('delta', 0):+d} "
            f"vwap={vwap_result.get('delta', 0):+d} "
            f"gap={gap_result.get('delta', 0):+d} "
            f"total={shadow_delta_total:+d}"
        )
    except Exception as e:
        log.warning(f"Shadow signals persist failed for {spread_id}: {e}")

    return record


def _safe_compute_skew(
    contracts: Optional[List[Dict]],
    spot: Optional[float],
    ticker: str,
    direction: str,
) -> Dict:
    """Compute skew signal with graceful degradation on missing inputs."""
    if not contracts or not spot:
        return {
            "available": False,
            "reason": "missing_inputs",
            "delta": 0,
            "reasons": [],
        }
    try:
        from options_skew import compute_skew, score_skew_for_trade
        skew_signal = compute_skew(contracts, spot, ticker=ticker)
        if not skew_signal.get("ok"):
            return {
                "available": False,
                "reason": "insufficient_chain_for_25d",
                "delta": 0,
                "reasons": [],
            }
        delta, reasons = score_skew_for_trade(skew_signal, direction)
        return {
            "available": True,
            "signal": skew_signal,
            "delta": delta,
            "reasons": reasons,
        }
    except Exception as e:
        log.warning(f"Shadow skew compute failed: {e}")
        return {"available": False, "reason": f"error: {e}", "delta": 0, "reasons": []}


def _safe_compute_vwap_bands(
    intraday_bars: Optional[List[Dict]],
    direction: str,
) -> Dict:
    """Compute VWAP bands signal with graceful degradation."""
    if not intraday_bars or len(intraday_bars) < 5:
        return {
            "available": False,
            "reason": "insufficient_bars",
            "delta": 0,
            "reasons": [],
        }
    try:
        from vwap_bands import compute_vwap_bands, score_vwap_bands_for_trade
        highs = [float(b.get("high", 0)) for b in intraday_bars]
        lows = [float(b.get("low", 0)) for b in intraday_bars]
        closes = [float(b.get("close", 0)) for b in intraday_bars]
        volumes = [float(b.get("volume", 0)) for b in intraday_bars]
        band_signal = compute_vwap_bands(highs, lows, closes, volumes)
        if not band_signal.get("ok"):
            return {
                "available": False,
                "reason": "vwap_compute_failed",
                "delta": 0,
                "reasons": [],
            }
        delta, reasons = score_vwap_bands_for_trade(band_signal, direction)
        return {
            "available": True,
            "signal": band_signal,
            "delta": delta,
            "reasons": reasons,
        }
    except Exception as e:
        log.warning(f"Shadow VWAP compute failed: {e}")
        return {"available": False, "reason": f"error: {e}", "delta": 0, "reasons": []}


def _safe_compute_gap(
    prior_day_data: Optional[Dict],
    spot: Optional[float],
    direction: str,
    first_bars: Optional[List[Dict]],
    minutes_since_open: Optional[int],
) -> Dict:
    """Compute gap signal only within first ~75 minutes of the session."""
    # Compute minutes_since_open if not provided
    if minutes_since_open is None:
        try:
            now = datetime.now(timezone.utc) - timedelta(hours=5)  # approx CT
            minutes_since_open = now.hour * 60 + now.minute - 510
        except Exception:
            minutes_since_open = None

    if minutes_since_open is None or minutes_since_open < 0:
        return {
            "available": False,
            "reason": "outside_session",
            "delta": 0,
            "reasons": [],
        }
    if minutes_since_open > GAP_ANALYZER_MAX_MINUTES_SINCE_OPEN:
        return {
            "available": False,
            "reason": "past_gap_window",
            "delta": 0,
            "reasons": [],
        }
    if not prior_day_data or not spot:
        return {
            "available": False,
            "reason": "missing_prior_day_or_spot",
            "delta": 0,
            "reasons": [],
        }
    try:
        from gap_analyzer import analyze_gap, score_gap_for_trade
        gap_signal = analyze_gap(
            prior_day_high=float(prior_day_data.get("high", 0)),
            prior_day_low=float(prior_day_data.get("low", 0)),
            prior_day_close=float(prior_day_data.get("close", 0)),
            session_open=float(prior_day_data.get("session_open") or spot),
            first_bars=first_bars,
            avg_opening_volume=prior_day_data.get("avg_opening_volume"),
        )
        if gap_signal.get("gap_type") in ("NO_GAP", "INSIDE_DAY"):
            return {
                "available": True,
                "signal": gap_signal,
                "delta": 0,
                "reasons": ["No meaningful gap"],
            }
        delta, reasons = score_gap_for_trade(gap_signal, direction)
        return {
            "available": True,
            "signal": gap_signal,
            "delta": delta,
            "reasons": reasons,
        }
    except Exception as e:
        log.warning(f"Shadow gap compute failed: {e}")
        return {"available": False, "reason": f"error: {e}", "delta": 0, "reasons": []}


def compute_shadow_bundle(
    ticker: str,
    direction: str,
    contracts: Optional[List[Dict]] = None,
    spot: Optional[float] = None,
    intraday_bars: Optional[List[Dict]] = None,
    prior_day_data: Optional[Dict] = None,
    first_bars_of_session: Optional[List[Dict]] = None,
    minutes_since_open: Optional[int] = None,
) -> Dict:
    """Compute shadow signals without persisting.

    Safe default for Phase 1b when you want signals attached directly to the
    recommendation campaign record instead of stored separately.
    """
    skew_result = _safe_compute_skew(contracts, spot, ticker, direction)
    vwap_result = _safe_compute_vwap_bands(intraday_bars, direction)
    gap_result = _safe_compute_gap(
        prior_day_data, spot, direction, first_bars_of_session, minutes_since_open
    )
    return {
        "ticker": ticker.upper(),
        "direction": direction,
        "skew": skew_result,
        "vwap": vwap_result,
        "gap": gap_result,
        "total_delta": (
            skew_result.get("delta", 0)
            + vwap_result.get("delta", 0)
            + gap_result.get("delta", 0)
        ),
    }




# ─────────────────────────────────────────────────────────
# SHADOW READER
# ─────────────────────────────────────────────────────────

def get_shadow_signals(persistent_state, spread_id: str) -> Optional[Dict]:
    """Retrieve stored shadow signals for a given spread_id."""
    try:
        key = f"{STATE_KEY_PREFIX}{spread_id}"
        raw = persistent_state.get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as e:
        log.warning(f"Shadow signals load failed for {spread_id}: {e}")
        return None


def list_all_shadow_signals(persistent_state) -> List[Dict]:
    """Return all stored shadow records. Uses scan_fn if available on the store."""
    records = []
    try:
        # PersistentState exposes a scan method — try it first
        if hasattr(persistent_state, "scan"):
            keys = persistent_state.scan(f"{STATE_KEY_PREFIX}*") or []
        elif hasattr(persistent_state, "_scan_fn") and persistent_state._scan_fn:
            keys = persistent_state._scan_fn(f"{STATE_KEY_PREFIX}*") or []
        else:
            log.warning("PersistentState has no scan method — cannot list shadow records")
            return []

        for k in keys:
            try:
                raw = persistent_state.get(k) if hasattr(persistent_state, "get") else None
                if raw is None and hasattr(persistent_state, "_get_fn"):
                    raw = persistent_state._get_fn(k)
                if raw:
                    records.append(json.loads(raw))
            except Exception:
                continue
    except Exception as e:
        log.warning(f"Shadow signals scan failed: {e}")
    return records


# ─────────────────────────────────────────────────────────
# IMPACT ANALYZER
# ─────────────────────────────────────────────────────────
# Joins shadow records with closed trade outcomes.
#
# Per-signal metrics:
#   - Win rate when signal said +N (favorable)
#   - Win rate when signal said -N (unfavorable)
#   - Win rate when signal was silent
#   - Counterfactual: how many trades would NOT have posted if we'd
#     applied the delta (confidence would have dropped below 60)
#   - Counterfactual hit rate: of those silenced trades, what fraction
#     were actual losers? (>0.5 = signal has edge)
# ═══════════════════════════════════════════════════════════════════

def analyze_shadow_edge(
    persistent_state,
    trade_journal,
    lookback_days: int = 30,
    min_sample_size: int = 5,
) -> Dict:
    """Cross-reference shadow signals with closed trade outcomes.

    Returns:
        {
            "window": {...},
            "signals": {
                "skew": {label_stats, counterfactual},
                "vwap": {label_stats, counterfactual},
                "gap": {label_stats, counterfactual},
                "combined_delta": {bucketed stats, counterfactual},
            },
            "recommendation": str,
        }
    """
    result = {
        "window": {"lookback_days": lookback_days},
        "signals": {},
        "recommendation": "",
    }

    # Collect closed trades
    try:
        since_ts = time.time() - (lookback_days * 86400)
        closed = trade_journal.get_closed_trades(since_ts=since_ts) or []
    except Exception as e:
        result["error"] = f"Could not load closed trades: {e}"
        return result

    # Load all shadow records, index by spread_id
    all_shadow = list_all_shadow_signals(persistent_state)
    shadow_by_spread = {s.get("spread_id"): s for s in all_shadow if s.get("spread_id")}

    # Join
    joined = []
    for t in closed:
        sid = t.get("spread_id")
        if not sid:
            continue
        shadow = shadow_by_spread.get(sid)
        if shadow is None:
            continue
        joined.append({"trade": t, "shadow": shadow})

    result["window"]["closed_trades_total"] = len(closed)
    result["window"]["joined_with_shadow"] = len(joined)

    if len(joined) < min_sample_size:
        result["recommendation"] = (
            f"Only {len(joined)} joined records. Need at least "
            f"{min_sample_size} for meaningful analysis. Wait for more data."
        )
        return result

    # Analyze each signal
    for signal_name in ("skew", "vwap", "gap"):
        result["signals"][signal_name] = _analyze_single_signal(joined, signal_name)

    # Analyze combined delta
    result["signals"]["combined_delta"] = _analyze_combined_delta(joined)

    # Synthesize recommendation
    result["recommendation"] = _synthesize_recommendation(result["signals"])
    return result


def _analyze_single_signal(joined: List[Dict], signal_name: str) -> Dict:
    """Compute per-signal edge metrics."""
    # Bucket by signal delta sign
    positive_deltas = []  # trades where signal suggested +conf
    negative_deltas = []  # trades where signal suggested -conf
    silent = []           # no delta contribution

    for j in joined:
        sig = j["shadow"].get(signal_name, {})
        delta = sig.get("delta", 0) or 0
        trade = j["trade"]
        pnl = float(trade.get("pnl_usd", 0) or 0)
        is_win = pnl > 0
        entry = {"pnl": pnl, "win": is_win, "delta": delta, "trade": trade}
        if delta > 0:
            positive_deltas.append(entry)
        elif delta < 0:
            negative_deltas.append(entry)
        else:
            silent.append(entry)

    def stats(entries):
        if not entries:
            return {"count": 0, "win_rate": 0, "avg_pnl": 0, "net_pnl": 0}
        wins = sum(1 for e in entries if e["win"])
        return {
            "count": len(entries),
            "win_rate": round(wins / len(entries), 3),
            "avg_pnl": round(sum(e["pnl"] for e in entries) / len(entries), 2),
            "net_pnl": round(sum(e["pnl"] for e in entries), 2),
            "avg_delta": round(
                sum(e["delta"] for e in entries) / len(entries), 2
            ) if entries else 0,
        }

    pos_stats = stats(positive_deltas)
    neg_stats = stats(negative_deltas)
    silent_stats = stats(silent)

    # Counterfactual: if negative deltas had been applied, how many would
    # have dropped below the confidence gate (60)?
    cf_silenced = []
    cf_would_fire = []
    for e in negative_deltas:
        conf_at_entry = e["trade"].get("confidence") or 0
        hypothetical = conf_at_entry + e["delta"]
        if hypothetical < 60:
            cf_silenced.append(e)
        else:
            cf_would_fire.append(e)

    cf_losers_silenced = sum(1 for e in cf_silenced if not e["win"])
    cf_precision = (
        round(cf_losers_silenced / len(cf_silenced), 3)
        if cf_silenced else 0
    )

    return {
        "positive_delta_stats": pos_stats,
        "negative_delta_stats": neg_stats,
        "silent_stats": silent_stats,
        "edge_metric": {
            "pos_wr_minus_neg_wr": round(
                pos_stats["win_rate"] - neg_stats["win_rate"], 3
            ),
            "pos_pnl_minus_neg_pnl_per_trade": round(
                pos_stats["avg_pnl"] - neg_stats["avg_pnl"], 2
            ),
        },
        "counterfactual_silence": {
            "trades_silenced": len(cf_silenced),
            "losers_silenced": cf_losers_silenced,
            "precision": cf_precision,
            "dollars_saved": round(
                sum(abs(e["pnl"]) for e in cf_silenced if not e["win"]) -
                sum(e["pnl"] for e in cf_silenced if e["win"]),
                2
            ),
        },
    }


def _analyze_combined_delta(joined: List[Dict]) -> Dict:
    """Bucket trades by total shadow delta."""
    buckets = defaultdict(list)
    for j in joined:
        total = j["shadow"].get("shadow_delta_total", 0) or 0
        trade = j["trade"]
        pnl = float(trade.get("pnl_usd", 0) or 0)
        is_win = pnl > 0
        if total <= -10:
            label = "strong_negative"
        elif total <= -3:
            label = "mild_negative"
        elif total < 3:
            label = "neutral"
        elif total < 10:
            label = "mild_positive"
        else:
            label = "strong_positive"
        buckets[label].append({"pnl": pnl, "win": is_win})

    result = {}
    for label, items in buckets.items():
        wins = sum(1 for i in items if i["win"])
        result[label] = {
            "count": len(items),
            "win_rate": round(wins / len(items), 3),
            "net_pnl": round(sum(i["pnl"] for i in items), 2),
        }
    return result


def _synthesize_recommendation(signals: Dict) -> str:
    """Turn the analysis into an actionable take."""
    parts = []
    for name in ("skew", "vwap", "gap"):
        sig = signals.get(name, {})
        if not sig:
            continue
        edge = sig.get("edge_metric", {}).get("pos_wr_minus_neg_wr", 0)
        cf = sig.get("counterfactual_silence", {})
        cf_precision = cf.get("precision", 0)
        n_silenced = cf.get("trades_silenced", 0)
        dollars_saved = cf.get("dollars_saved", 0)

        if edge >= 0.15 and cf_precision >= 0.60 and n_silenced >= 5:
            parts.append(
                f"✅ {name.upper()}: GO LIVE. "
                f"Edge {edge:+.2f} win-rate diff, "
                f"would have saved ${dollars_saved:,.0f} by silencing "
                f"{cf['losers_silenced']}/{n_silenced} losers."
            )
        elif edge >= 0.05:
            parts.append(
                f"🟡 {name.upper()}: PROMISING but marginal. "
                f"Edge {edge:+.2f}. Wait 2 more weeks of data."
            )
        elif edge <= -0.05:
            parts.append(
                f"❌ {name.upper()}: INVERTED. The signal appears "
                f"anti-correlated with outcomes — do NOT go live. "
                f"Re-examine the scoring logic."
            )
        else:
            parts.append(
                f"⚪ {name.upper()}: NO EDGE detected in shadow window. "
                f"Edge {edge:+.2f}."
            )
    return "\n".join(parts) if parts else "Insufficient data for recommendation."


# ─────────────────────────────────────────────────────────
# TELEGRAM REPORT BUILDER
# ─────────────────────────────────────────────────────────

def format_shadow_report(analysis: Dict) -> str:
    """Telegram-ready formatted version of the analysis."""
    if "error" in analysis:
        return f"⚠️ Shadow analysis error: {analysis['error']}"

    lines = ["🔍 Shadow Signal Analysis"]
    w = analysis.get("window", {})
    lines.append(f"Window: {w.get('lookback_days', 0)}d")
    lines.append(
        f"Joined: {w.get('joined_with_shadow', 0)} of "
        f"{w.get('closed_trades_total', 0)} closed trades"
    )
    lines.append("")

    for name in ("skew", "vwap", "gap"):
        sig = analysis.get("signals", {}).get(name)
        if not sig:
            continue
        lines.append(f"━━━━ {name.upper()} ━━━━")
        pos = sig.get("positive_delta_stats", {})
        neg = sig.get("negative_delta_stats", {})
        sil = sig.get("silent_stats", {})
        edge = sig.get("edge_metric", {})
        cf = sig.get("counterfactual_silence", {})

        lines.append(
            f"  Pos delta: {pos.get('count', 0)}T, "
            f"WR {pos.get('win_rate', 0):.1%}, "
            f"${pos.get('avg_pnl', 0):+.0f}/trade"
        )
        lines.append(
            f"  Neg delta: {neg.get('count', 0)}T, "
            f"WR {neg.get('win_rate', 0):.1%}, "
            f"${neg.get('avg_pnl', 0):+.0f}/trade"
        )
        lines.append(
            f"  Silent:    {sil.get('count', 0)}T, "
            f"WR {sil.get('win_rate', 0):.1%}"
        )
        lines.append(
            f"  Edge: {edge.get('pos_wr_minus_neg_wr', 0):+.3f} WR diff, "
            f"${edge.get('pos_pnl_minus_neg_pnl_per_trade', 0):+.2f}/trade"
        )
        if cf.get("trades_silenced", 0) > 0:
            lines.append(
                f"  CF silence: {cf['trades_silenced']} silenced, "
                f"{cf['losers_silenced']} losers "
                f"(precision {cf['precision']:.0%}), "
                f"${cf['dollars_saved']:+,.0f} saved"
            )
        lines.append("")

    combined = analysis.get("signals", {}).get("combined_delta", {})
    if combined:
        lines.append("━━━━ COMBINED DELTA ━━━━")
        for label in ("strong_negative", "mild_negative", "neutral",
                      "mild_positive", "strong_positive"):
            b = combined.get(label)
            if not b:
                continue
            lines.append(
                f"  {label:20s}: {b['count']:3d}T, "
                f"WR {b['win_rate']:.1%}, net ${b['net_pnl']:+,.0f}"
            )
        lines.append("")

    lines.append("━━━━ RECOMMENDATION ━━━━")
    lines.append(analysis.get("recommendation", "(no recommendation)"))
    return "\n".join(lines)
