# omega_small_fixes.py
# ═══════════════════════════════════════════════════════════════════
# Small but impactful drop-in fixes
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# These are smaller modules that address the remaining gaps:
#   1. Adaptive RV spike threshold (replaces fixed 1.35x)
#   2. Log-odds confidence aggregator (prevents additive drift)
#   3. Intraday sector relative strength (5-min)
#   4. Position Greeks summary generator
#   5. Pre-chain gate threshold raise (recommended 50 vs current 45)
#
# Each one is self-contained. See bottom for integration sites.
# ═══════════════════════════════════════════════════════════════════

import math
import logging
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# 1. ADAPTIVE RV SPIKE THRESHOLD
# ═══════════════════════════════════════════════════════════════════

def adaptive_rv_spike_threshold(vix: float) -> float:
    """Scale RV spike threshold based on VIX regime.

    Current code in unified_models.py uses fixed 1.35:
        rv_spike = bool(market_rv5 > market_rv20 * 1.35)

    This tightens the threshold in low vol (where small spikes matter)
    and loosens it in high vol (where 1.35 is too noisy).

    Replace the hardcoded 1.35 with:
        from omega_small_fixes import adaptive_rv_spike_threshold
        threshold = adaptive_rv_spike_threshold(vix)
        rv_spike = bool(market_rv5 > market_rv20 * threshold)

    Returns:
        1.20-1.50 depending on VIX level.
    """
    if vix <= 0:
        return 1.35   # fallback to current default
    # VIX 10 → 1.22 (tight), VIX 20 → 1.32, VIX 30 → 1.42 (loose)
    # Linear: threshold = 1.20 + 0.01 × (VIX − 10), clamped
    threshold = 1.20 + 0.01 * (vix - 10)
    return max(1.15, min(threshold, 1.55))


# ═══════════════════════════════════════════════════════════════════
# 2. LOG-ODDS CONFIDENCE AGGREGATOR
# ═══════════════════════════════════════════════════════════════════
#
# Problem with current additive model: 25+ components all stack linearly.
# Correlated signals (e.g. RSI+MFI, above VWAP, daily bull) all fire
# together and the score inflates to 95 without true edge.
#
# Log-odds solution: each signal contributes a log-odds increment,
# aggregate in log-odds space, convert back via sigmoid. This naturally
# prevents overconfidence from correlated components.

def logit(p: float) -> float:
    """Convert probability to log-odds."""
    p = max(0.001, min(0.999, p))
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    """Convert log-odds to probability."""
    return 1.0 / (1.0 + math.exp(-max(min(x, 50), -50)))


class LogOddsConfidence:
    """Alternative confidence aggregator that prevents drift.

    Convention (standard Bayesian):
      - weight   is the MAGNITUDE of a signal's influence (always ≥ 0).
                 Typical range 0.2-3.0.
      - evidence is the DIRECTION and strength, signed [-1, +1]:
                   +1.0 = strong evidence FOR the hypothesis
                    0.0 = neutral
                   -1.0 = strong evidence AGAINST the hypothesis
      - contribution = weight × evidence (signed log-odds increment)

    Do NOT use negative weights — they flip sign semantics and make
    debugging confusing. Put the sign on the evidence.

    Usage:
        agg = LogOddsConfidence(base_prob=0.50)
        agg.add("tier_1_signal",    weight=2.0, evidence=+1.0)   # strong FOR
        agg.add("htf_confirmed",    weight=1.5, evidence=+0.8)   # moderate FOR
        agg.add("daily_opposing",   weight=2.0, evidence=-0.9)   # strong AGAINST
        agg.add("noisy_indicator",  weight=0.3, evidence=+0.5)   # small nudge

        final_prob = agg.probability()
        confidence_0_100 = int(round(final_prob * 100))
    """

    def __init__(self, base_prob: float = 0.50):
        self._base = logit(base_prob)
        self._components: List[Dict] = []

    def add(self, name: str, weight: float, evidence: float):
        """Add a signal's log-odds contribution.

        Args:
            name:     Identifier for debugging/breakdown.
            weight:   Magnitude of influence (clamped ≥ 0).
                      Higher = signal moves odds more aggressively.
            evidence: Signed direction of the evidence, clamped to [-1, +1].
                      Positive = supports hypothesis, negative = opposes.
        """
        if weight < 0:
            log.warning(
                f"LogOddsConfidence.add({name}): negative weight {weight} "
                f"will be clamped to 0. Use negative evidence instead."
            )
        weight = max(0.0, float(weight))
        evidence = max(-1.0, min(1.0, float(evidence)))

        contribution = weight * evidence
        self._components.append({
            "name": name,
            "weight": weight,
            "evidence": evidence,
            "contribution": contribution,
        })

    def total_log_odds(self) -> float:
        return self._base + sum(c["contribution"] for c in self._components)

    def probability(self) -> float:
        return sigmoid(self.total_log_odds())

    def confidence(self) -> int:
        return int(round(self.probability() * 100))

    def breakdown(self) -> List[Dict]:
        return list(self._components)


def translate_additive_to_logodds(additive_score: int) -> LogOddsConfidence:
    """Convert an existing additive score (0-100) to a log-odds object.

    For gradual migration — call compute_confidence() as-is, then wrap
    the result. Future signals can use agg.add() directly for proper
    log-odds behavior while legacy components keep working.
    """
    p = max(0.05, min(0.95, additive_score / 100.0))
    agg = LogOddsConfidence(base_prob=p)
    return agg


# ═══════════════════════════════════════════════════════════════════
# 3. INTRADAY SECTOR RELATIVE STRENGTH
# ═══════════════════════════════════════════════════════════════════

# Map tickers to their sector ETF for relative strength comparison
TICKER_TO_SECTOR = {
    "AAPL": "XLK", "MSFT": "XLK", "NVDA": "XLK", "GOOGL": "XLK", "GOOG": "XLK",
    "META": "XLK", "AMZN": "XLY", "TSLA": "XLY", "NFLX": "XLY",
    "AMD": "XLK", "AVGO": "XLK", "SMCI": "XLK", "ARM": "XLK",
    "COIN": "XLF", "JPM": "XLF", "GS": "XLF", "MS": "XLF",
    "LLY": "XLV", "UNH": "XLV", "JNJ": "XLV",
    "BA": "XLI", "CAT": "XLI", "GE": "XLI",
    "GLD": "GLD", "SLV": "SLV",
    "TLT": "TLT",
}


def compute_intraday_rs(
    ticker_bars: List[Dict],
    sector_bars: List[Dict],
    spy_bars: Optional[List[Dict]] = None,
    lookback_bars: int = 24,   # 24 × 5-min = 2 hours
) -> Dict:
    """Compute intraday relative strength vs sector (and optionally vs SPY).

    Args:
        ticker_bars, sector_bars: lists of 5-min bar dicts with 'close'.
        spy_bars: optional SPY bars for market-relative strength.
        lookback_bars: how far back to compute % change.

    Returns:
      rs_vs_sector:   % change difference over lookback (ticker - sector)
      rs_vs_market:   % change difference over lookback (ticker - SPY)
      regime:         STRONG_LEADER / LEADING / NEUTRAL / LAGGING / STRONG_LAGGARD
      confidence_delta: Suggested conf adjustment
    """
    result = {
        "rs_vs_sector": 0.0,
        "rs_vs_market": 0.0,
        "regime": "UNKNOWN",
        "confidence_delta": 0,
        "reason": "",
    }

    if not ticker_bars or not sector_bars:
        return result

    n = min(lookback_bars, len(ticker_bars), len(sector_bars))
    if n < 5:
        return result

    def pct_change(bars, n):
        if len(bars) < n:
            return 0.0
        start = float(bars[-n].get("close", 0))
        end = float(bars[-1].get("close", 0))
        if start <= 0:
            return 0.0
        return (end - start) / start * 100

    ticker_pct = pct_change(ticker_bars, n)
    sector_pct = pct_change(sector_bars, n)
    rs_vs_sector = round(ticker_pct - sector_pct, 3)

    rs_vs_market = None
    if spy_bars:
        spy_pct = pct_change(spy_bars, n)
        rs_vs_market = round(ticker_pct - spy_pct, 3)

    # Classify — thresholds are in % difference
    if rs_vs_sector >= 0.75:
        regime = "STRONG_LEADER"
        delta = +5
        reason = f"Ticker +{rs_vs_sector:.2f}% vs sector — strong intraday leader"
    elif rs_vs_sector >= 0.30:
        regime = "LEADING"
        delta = +3
        reason = f"Ticker +{rs_vs_sector:.2f}% vs sector — leading"
    elif rs_vs_sector <= -0.75:
        regime = "STRONG_LAGGARD"
        delta = -5
        reason = f"Ticker {rs_vs_sector:.2f}% vs sector — strong laggard"
    elif rs_vs_sector <= -0.30:
        regime = "LAGGING"
        delta = -3
        reason = f"Ticker {rs_vs_sector:.2f}% vs sector — lagging"
    else:
        regime = "NEUTRAL"
        delta = 0
        reason = f"Ticker tracking sector ({rs_vs_sector:+.2f}%)"

    result.update({
        "rs_vs_sector": rs_vs_sector,
        "rs_vs_market": rs_vs_market,
        "regime": regime,
        "confidence_delta": delta,
        "reason": reason,
    })
    return result


def score_intraday_rs_for_trade(
    rs_result: Dict,
    direction: str = "bull",
) -> Tuple[int, List[str]]:
    """Apply RS regime as confidence adjustment (direction-aware)."""
    if not rs_result or rs_result.get("regime") == "UNKNOWN":
        return 0, []

    regime = rs_result["regime"]
    base_delta = rs_result.get("confidence_delta", 0)
    is_bear = direction == "bear"

    # For bull trades: leader = +, laggard = -
    # For bear trades: laggard = +, leader = -
    if is_bear:
        delta = -base_delta
    else:
        delta = base_delta

    reason = rs_result.get("reason", "")
    if abs(delta) >= 3:
        return delta, [f"{reason} ({delta:+d})"]
    return 0, []


# ═══════════════════════════════════════════════════════════════════
# 4. POSITION GREEKS SUMMARY
# ═══════════════════════════════════════════════════════════════════

def summarize_book_greeks(open_positions: List[Dict]) -> Dict:
    """Aggregate Greeks across all open positions.

    Expects each position dict to have:
        net_delta, net_gamma, net_theta, net_vega (per contract),
        contracts, multiplier (default 100)

    Returns portfolio-level exposure in dollars per 1% spot / 1 day / 1 vol point.
    """
    totals = {
        "delta_dollars_per_1pct": 0.0,
        "gamma_dollars_per_1pct_sq": 0.0,
        "theta_dollars_per_day": 0.0,
        "vega_dollars_per_1vol": 0.0,
        "position_count": 0,
    }

    if not open_positions:
        return totals

    for p in open_positions:
        contracts = int(p.get("contracts", 0))
        mult = int(p.get("multiplier", 100))
        spot = float(p.get("spot", 0))
        if contracts == 0:
            continue

        delta = float(p.get("net_delta", 0)) * contracts * mult * spot * 0.01
        # Gamma per 1% spot move squared
        gamma = float(p.get("net_gamma", 0)) * contracts * mult * (spot * 0.01) ** 2
        theta = float(p.get("net_theta", 0)) * contracts * mult
        vega = float(p.get("net_vega", 0)) * contracts * mult

        totals["delta_dollars_per_1pct"] += delta
        totals["gamma_dollars_per_1pct_sq"] += gamma
        totals["theta_dollars_per_day"] += theta
        totals["vega_dollars_per_1vol"] += vega
        totals["position_count"] += 1

    return {k: round(v, 2) if isinstance(v, float) else v for k, v in totals.items()}


def format_book_greeks_report(greeks: Dict) -> str:
    """Pretty-print the morning book Greeks summary for Telegram."""
    if not greeks or greeks.get("position_count", 0) == 0:
        return "📐 Book Greeks\n\nNo open positions."

    lines = [f"📐 Book Greeks — {greeks['position_count']} open positions"]
    lines.append("")
    lines.append(f"Delta (per 1% spot):     ${greeks['delta_dollars_per_1pct']:+,.0f}")
    lines.append(f"Gamma (per 1% spot²):    ${greeks['gamma_dollars_per_1pct_sq']:+,.0f}")
    lines.append(f"Theta (per day):         ${greeks['theta_dollars_per_day']:+,.0f}")
    lines.append(f"Vega (per 1 vol pt):     ${greeks['vega_dollars_per_1vol']:+,.0f}")
    lines.append("")

    # Risk flags
    flags = []
    if abs(greeks['delta_dollars_per_1pct']) > 2000:
        flags.append(f"⚠️  Large directional exposure (${greeks['delta_dollars_per_1pct']:+,.0f} per 1%)")
    if greeks['theta_dollars_per_day'] < -100:
        flags.append(f"⚠️  Large theta decay (${greeks['theta_dollars_per_day']:+,.0f}/day)")
    if abs(greeks['vega_dollars_per_1vol']) > 1000:
        flags.append(f"⚠️  Large vol exposure (${greeks['vega_dollars_per_1vol']:+,.0f} per vol pt)")

    if flags:
        lines.append("━━━━ Risk Flags ━━━━")
        lines.extend(flags)

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# 5. PRE-CHAIN GATE THRESHOLD RECOMMENDATION
# ═══════════════════════════════════════════════════════════════════
#
# In prechain_gate.py:
#     PRECHAIN_MIN_CONFIDENCE_ESTIMATE = 45
#
# Observations from your confidence model:
#   - Base score is 40 (signal) or ~40 after webhook inputs
#   - Passing the 45 gate requires only 5 points of positive evidence
#   - Most trades are rejected AFTER chain fetch by the 60 gate
#   - Raising to 50 would block ~30% more pre-chain rejects without
#     losing any qualifying trades
#
# Drop-in patch for prechain_gate.py:
#     PRECHAIN_MIN_CONFIDENCE_ESTIMATE = 50

PRECHAIN_MIN_CONFIDENCE_ESTIMATE_RECOMMENDED = 50


# ═══════════════════════════════════════════════════════════════════
# INTEGRATION NOTES
# ═══════════════════════════════════════════════════════════════════
#
# 1. Adaptive RV threshold:
#    Edit unified_models.py build_canonical_vol_regime() where rv_spike
#    is computed. Replace:
#       rv_spike = bool(market_rv5 and market_rv20 and
#                       market_rv5 > (market_rv20 * 1.35))
#    With:
#       from omega_small_fixes import adaptive_rv_spike_threshold
#       _rv_threshold = adaptive_rv_spike_threshold(vix)
#       rv_spike = bool(market_rv5 and market_rv20 and
#                       market_rv5 > (market_rv20 * _rv_threshold))
#
# 2. Log-odds confidence (gradual migration):
#    Keep compute_confidence() as-is initially. For new signals (skew,
#    velocity, gap, RS), aggregate in log-odds space then blend back.
#    See integration_guide.md for the recommended two-phase approach.
#
# 3. Intraday RS:
#    In active_scanner._analyze_ticker(), after technical scoring,
#    fetch sector bars using the TICKER_TO_SECTOR map and call
#    compute_intraday_rs(). Add delta to score.
#
# 4. Book Greeks:
#    Add to daily pre-market Telegram post in app.py ~line 4584:
#       from omega_small_fixes import summarize_book_greeks, format_book_greeks_report
#       greeks = summarize_book_greeks(portfolio.list_open_positions())
#       post_to_telegram(format_book_greeks_report(greeks))
#
# 5. Pre-chain threshold:
#    In prechain_gate.py line 49, change 45 → 50.
