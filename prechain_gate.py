# prechain_gate.py
# ═══════════════════════════════════════════════════════════════════
# Pre-Chain Qualification Gate
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Problem: The current pipeline pulls full option chains (5 MarketData
# API calls per ticker) BEFORE checking if the ticker is even trade-worthy.
# ~60% of signals get rejected by earnings, regime, confidence, or
# signal validation checks that don't need chain data at all.
#
# Solution: Run all cheap checks FIRST. Only pull chains for tickers
# that pass every pre-chain gate. This saves 3-5 API calls per
# rejected ticker and cuts daily MarketData usage by 40-60%.
#
# Gate order (cheapest → most expensive, zero chain data needed):
#   1. Earnings check         — Finnhub (cached 1hr, not MarketData)
#   2. Economic calendar      — cached daily, zero API calls
#   3. Signal freshness       — pure math, zero API calls
#   4. Vol regime gate        — cached VIX + candles (already cached)
#   5. Fundamental score      — cached nightly, zero API calls
#   6. Sector strength        — cached daily, zero API calls
#   7. Volume confirmation    — spot quote only (1 API call, already fetched)
#   8. Signal confidence      — pre-estimate from webhook data alone
#
# If ALL gates pass → pull chains → run full check_ticker()
# If ANY gate fails → reject immediately, log reason, save 5 API calls
#
# Usage:
#   from prechain_gate import should_pull_chains
#   result = should_pull_chains(ticker, bias, webhook_data, spot, candle_closes, regime, ...)
#   if result["qualified"]:
#       chains = get_options_chain(ticker)  # only now
#   else:
#       log.info(f"Rejected pre-chain: {result['reason']}")
# ═══════════════════════════════════════════════════════════════════

import time
import logging
from typing import Dict, Optional, List
from trading_rules import (
    SIGNAL_STALE_AFTER_SEC,
    SCALP_SIGNAL_HARD_BLOCK_PCT,
    SWING_SIGNAL_HARD_BLOCK_PCT,
)

log = logging.getLogger(__name__)


# ── Confidence pre-estimate thresholds ──
# Before chains, we can estimate confidence from webhook data alone.
# If the pre-estimate is too low, chains won't save it.
PRECHAIN_MIN_CONFIDENCE_ESTIMATE = 45   # below this, chains are pointless
PRECHAIN_EARNINGS_BLOCK = True          # block tickers with earnings in DTE window
PRECHAIN_MACRO_EVENT_BLOCK_0DTE = True  # block 0DTE during high-impact macro events
PRECHAIN_MIN_FUNDAMENTAL_SCORE = 30     # for swing trades, minimum fundamental score
PRECHAIN_MIN_SECTOR_RANK = 8           # sector must be in top 8 of 11 (swing only)


def _pre_estimate_confidence(webhook_data: dict, bias: str) -> int:
    """
    Estimate confidence score using ONLY webhook data — no chain needed.
    This mirrors the additive scoring in trading_rules.py CONFIDENCE_BOOSTS/PENALTIES
    but only uses fields available before chain fetch.

    Returns estimated confidence 0-100.
    """
    from trading_rules import CONFIDENCE_BOOSTS, CONFIDENCE_PENALTIES

    score = 40  # base score (same as check_ticker starts with)

    tier = str(webhook_data.get("tier", "2"))
    if tier == "1":
        score += CONFIDENCE_BOOSTS.get("tier_1", 15)
    elif tier == "2":
        score += CONFIDENCE_BOOSTS.get("tier_2", 5)

    # HTF trend alignment (from TradingView webhook)
    if webhook_data.get("htf_confirmed"):
        score += CONFIDENCE_BOOSTS.get("htf_confirmed", 10)
    elif webhook_data.get("htf_converging"):
        score += CONFIDENCE_BOOSTS.get("htf_converging", 5)
    else:
        score += CONFIDENCE_PENALTIES.get("htf_diverging", -10)

    # Daily trend alignment
    daily_bull = webhook_data.get("daily_bull", False)
    if bias == "bull" and daily_bull:
        score += CONFIDENCE_BOOSTS.get("daily_bull", 10)
    elif bias == "bull" and not daily_bull:
        score += CONFIDENCE_PENALTIES.get("daily_bear", -10)
    elif bias == "bear" and not daily_bull:
        score += CONFIDENCE_BOOSTS.get("daily_bear", 10)
    elif bias == "bear" and daily_bull:
        score += CONFIDENCE_PENALTIES.get("daily_bull", -10)

    # WaveTrend zone
    wt2 = webhook_data.get("wt2") or 0
    if isinstance(wt2, (int, float)):
        if bias == "bull" and wt2 < -30:
            score += CONFIDENCE_BOOSTS.get("wave_oversold", 10)
        elif bias == "bull" and wt2 > 60:
            score += CONFIDENCE_PENALTIES.get("wave_overbought", -15)
        elif bias == "bear" and wt2 > 60:
            score += CONFIDENCE_BOOSTS.get("wave_overbought", 10)
        elif bias == "bear" and wt2 < -30:
            score += CONFIDENCE_PENALTIES.get("wave_oversold", -15)

    # VWAP position
    if webhook_data.get("above_vwap") and bias == "bull":
        score += CONFIDENCE_BOOSTS.get("above_vwap", 5)
    elif not webhook_data.get("above_vwap") and bias == "bear":
        score += CONFIDENCE_BOOSTS.get("below_vwap", 5)

    # RSI+MFI
    if webhook_data.get("rsi_mfi_bull") and bias == "bull":
        score += CONFIDENCE_BOOSTS.get("rsi_mfi_bull", 5)
    elif not webhook_data.get("rsi_mfi_bull") and bias == "bear":
        score += CONFIDENCE_BOOSTS.get("rsi_mfi_bear", 5)

    return max(0, min(100, score))


def _check_signal_freshness(webhook_data: dict) -> Dict:
    """
    Check if the signal is too stale to be worth pulling chains for.
    Returns {"ok": True/False, "reason": str, "age_sec": int}
    """
    received_at = webhook_data.get("received_at_epoch", 0)
    if not received_at:
        return {"ok": True, "reason": "", "age_sec": None}

    age_sec = int(time.time() - received_at)

    # Hard stale limit — unified with app.py via trading_rules
    STALE_LIMIT = SIGNAL_STALE_AFTER_SEC
    if age_sec > STALE_LIMIT:
        return {
            "ok": False,
            "reason": f"Signal too stale ({age_sec}s > {STALE_LIMIT}s) — skipping chain fetch",
            "age_sec": age_sec,
        }

    return {"ok": True, "reason": "", "age_sec": age_sec}


def _check_price_drift(webhook_data: dict, live_spot: float) -> Dict:
    """
    Pre-check price drift before pulling chains.
    If price has moved too far from TV signal, chains are wasted.
    """
    alert_close = webhook_data.get("close")
    if not alert_close or not live_spot or alert_close <= 0 or live_spot <= 0:
        return {"ok": True, "reason": "", "drift_pct": None}

    try:
        alert_close = float(alert_close)
        drift_pct = abs((live_spot - alert_close) / alert_close) * 100.0

        # Timeframe-aware drift: swing signals get wider tolerance
        _tf = str((webhook_data or {}).get("timeframe") or "").lower()
        _is_swing = (any(tag in _tf for tag in ("d", "w", "day", "week"))
                     or bool((webhook_data or {}).get("is_swing")))
        HARD_DRIFT_PCT = SWING_SIGNAL_HARD_BLOCK_PCT if _is_swing else SCALP_SIGNAL_HARD_BLOCK_PCT
        if drift_pct > HARD_DRIFT_PCT:
            return {
                "ok": False,
                "reason": f"Price drift {drift_pct:.2f}% > {HARD_DRIFT_PCT}% "
                          f"(${alert_close:.2f} → ${live_spot:.2f}) — skipping chain fetch",
                "drift_pct": drift_pct,
            }

        return {"ok": True, "reason": "", "drift_pct": drift_pct}
    except (ValueError, TypeError):
        return {"ok": True, "reason": "", "drift_pct": None}


def should_pull_chains(
    ticker: str,
    bias: str,
    webhook_data: dict,
    live_spot: float = None,
    candle_closes: list = None,
    regime: dict = None,
    vol_regime: dict = None,
    enrichment: dict = None,
    fundamental_data: dict = None,
    sector_data: dict = None,
    econ_events: list = None,
    job_type: str = "tv",
) -> Dict:
    """
    Master pre-chain qualification gate.

    Runs all cheap checks before any chain API calls.

    Returns:
        {
            "qualified": bool,
            "reason": str,          # why rejected (empty if qualified)
            "gate_failed": str,     # which gate killed it
            "pre_confidence": int,  # estimated confidence before chains
            "gates_passed": list,   # which gates passed
            "api_calls_saved": int, # estimated API calls saved by early rejection
        }
    """
    ticker = ticker.strip().upper()
    gates_passed = []
    api_calls_saved = 5  # 1 expirations + 4 chain calls

    def _reject(gate: str, reason: str) -> Dict:
        return {
            "qualified": False,
            "reason": reason,
            "gate_failed": gate,
            "pre_confidence": 0,
            "gates_passed": gates_passed,
            "api_calls_saved": api_calls_saved,
        }

    # ── Gate 1: Signal Freshness ──
    freshness = _check_signal_freshness(webhook_data)
    if not freshness["ok"]:
        return _reject("signal_freshness", freshness["reason"])
    gates_passed.append("signal_freshness")

    # ── Gate 2: Price Drift ──
    if live_spot:
        drift = _check_price_drift(webhook_data, live_spot)
        if not drift["ok"]:
            return _reject("price_drift", drift["reason"])
        gates_passed.append("price_drift")

    # ── Gate 3: Earnings Check ──
    if PRECHAIN_EARNINGS_BLOCK and enrichment:
        if enrichment.get("has_earnings"):
            from trading_rules import NO_EARNINGS_WEEK
            if NO_EARNINGS_WEEK:
                return _reject(
                    "earnings",
                    f"Earnings within DTE window — {enrichment.get('earnings_warn', 'blocking')}"
                )
    gates_passed.append("earnings")

    # ── Gate 4: Economic Calendar (0DTE only) ──
    if PRECHAIN_MACRO_EVENT_BLOCK_0DTE and econ_events and job_type in ("tv", "scanner"):
        from trading_rules import MIN_DTE, MAX_DTE
        # Only block if DTE range includes today (0DTE)
        if MIN_DTE == 0:
            high_impact = [e for e in econ_events if e.get("impact") == "high"]
            if high_impact:
                event_names = ", ".join(e.get("event", "?")[:30] for e in high_impact[:3])
                return _reject(
                    "macro_event",
                    f"High-impact macro event today: {event_names} — 0DTE blocked"
                )
    gates_passed.append("econ_calendar")

    # ── Gate 5: Vol Regime (CRISIS — direction-aware) ──
    # v5.0: Bears are the correct trade in CRISIS. Only block bulls.
    if vol_regime:
        label = (vol_regime.get("label") or "").upper()
        caution = vol_regime.get("caution_score", 0)

        if label == "CRISIS" or caution >= 6:
            if job_type == "swing" and bias == "bull":
                return _reject("vol_regime",
                    f"CRISIS regime (VIX {vol_regime.get('vix', '?')}, caution {caution}/8) — bull swing blocked")
            if job_type in ("tv", "scanner") and bias == "bull":
                return _reject("vol_regime",
                    f"CRISIS regime (VIX {vol_regime.get('vix', '?')}) — bull calls blocked")
            # Bears in CRISIS: log but allow through
            log.debug(f"Pre-chain gate: CRISIS regime but {bias} signal — allowing through")
    gates_passed.append("vol_regime")

    # ── Gate 6: Fundamental Score (swing trades only) ──
    if job_type == "swing" and fundamental_data:
        fscore = fundamental_data.get("fundamental_score", 100)
        if fscore < PRECHAIN_MIN_FUNDAMENTAL_SCORE:
            return _reject("fundamental_score",
                f"Fundamental score {fscore}/100 < {PRECHAIN_MIN_FUNDAMENTAL_SCORE} minimum for swing")
    gates_passed.append("fundamental_score")

    # ── Gate 7: Sector Strength (swing trades only) ──
    if job_type == "swing" and sector_data:
        sector_rank = sector_data.get("rank", 1)
        total_sectors = sector_data.get("total", 11)
        if sector_rank > PRECHAIN_MIN_SECTOR_RANK:
            return _reject("sector_strength",
                f"Sector rank {sector_rank}/{total_sectors} — too weak for swing entry")
    gates_passed.append("sector_strength")

    # ── Gate 8: Pre-Confidence Estimate ──
    pre_conf = _pre_estimate_confidence(webhook_data, bias)

    # Apply regime penalty to pre-estimate
    if vol_regime:
        label = (vol_regime.get("label") or "").upper()
        if "CHOP" in label:
            from trading_rules import CHOP_REGIME_CONF_GATE
            if pre_conf < CHOP_REGIME_CONF_GATE:
                return _reject("pre_confidence",
                    f"Pre-confidence {pre_conf}/100 below CHOP gate {CHOP_REGIME_CONF_GATE} — chains not worth pulling")

    if pre_conf < PRECHAIN_MIN_CONFIDENCE_ESTIMATE:
        return _reject("pre_confidence",
            f"Pre-confidence estimate {pre_conf}/100 < {PRECHAIN_MIN_CONFIDENCE_ESTIMATE} minimum — chains not worth pulling")
    gates_passed.append("pre_confidence")

    # ── All gates passed ──
    return {
        "qualified": True,
        "reason": "",
        "gate_failed": None,
        "pre_confidence": pre_conf,
        "gates_passed": gates_passed,
        "api_calls_saved": 0,
    }
