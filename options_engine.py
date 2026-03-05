# options_engine.py
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# UPGRADED ENGINE v2 — Full rewrite
# Key fixes:
#   - Correct bull/bear × debit/credit × call/put mapping
#   - Delta-gated short strike selection
#   - IV rank/percentile awareness
#   - Skew-informed direction bias
#   - Dynamic strike increments (handles SPY, SPX, high-price stocks)
#   - Probability of profit estimate (delta-based)
#   - Minimum quality filters (credit % of width, debit RoR floor)
#   - RoR ranking normalized by width
#   - Position sizing: max-$ risk + 2% account cap → contract count
#   - Confidence gating: suppresses low-conviction trades

import math
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

# ─────────────────────────────────────────────────────────
# TUNABLES
# ─────────────────────────────────────────────────────────
IV_HIGH_FOR_CREDIT    = 0.45
IV_LOW_FOR_DEBIT      = 0.22
IVR_HIGH_FOR_CREDIT   = 50      # IV rank above this → credit bias
IVR_LOW_FOR_DEBIT     = 30      # IV rank below this → debit bias

K_SIGMA_CREDIT        = 1.20    # short strike 1.2 sigma OTM for credits
K_SIGMA_DEBIT_OTM     = 0.80    # short (protective) leg for debits

MAX_SHORT_DELTA_CREDIT = 0.38
MAX_SHORT_DELTA_DEBIT  = 0.55

MIN_CREDIT_PCT_WIDTH   = 0.25   # minimum 20% of width as credit
MIN_CREDIT_ABSOLUTE    = 0.30   # minimum $0.25 credit regardless of width
MIN_DEBIT_ROR          = 0.25   # debit RoR floor
MIN_CONFIDENCE         = 40

ROR_WEIGHT             = 1.0
WIDTH_BONUS_WEIGHT     = 0.10
POP_WEIGHT             = 0.20
PRICE_WEIGHT           = 0.05

DEFAULT_ACCOUNT_SIZE   = 100_000.0
DEFAULT_MAX_RISK_PCT   = 0.02
DEFAULT_MAX_RISK_USD   = 500.0

DEFAULT_WARN_OI        = 500
DEFAULT_WARN_BA        = 0.30


# ─────────────────────────────────────────────────────────
# CORE MATH
# ─────────────────────────────────────────────────────────

def implied_move(spot: float, iv: float, dte: int) -> float:
    return spot * iv * math.sqrt(max(dte, 1) / 365.0)


def vertical_metrics(width, debit=None, credit=None):
    if debit is not None:
        mp, ml = width - debit, debit
        return mp, ml, (mp / ml if ml > 0 else None)
    if credit is not None:
        mp, ml = credit, width - credit
        return mp, ml, (mp / ml if ml > 0 else None)
    raise ValueError("Provide debit or credit")


def delta_to_pop(delta: Optional[float], spread_type: str) -> Optional[float]:
    if delta is None:
        return None
    d = abs(delta)
    return round(1.0 - d if spread_type == "credit" else d, 3)


def compute_iv_rank(iv_current, iv_52w_high, iv_52w_low) -> Optional[float]:
    if None in (iv_52w_high, iv_52w_low):
        return None
    rng = iv_52w_high - iv_52w_low
    if rng <= 0:
        return None
    return round(min(max((iv_current - iv_52w_low) / rng * 100.0, 0), 100), 1)


def skew_score(call_iv: Optional[float], put_iv: Optional[float]) -> Tuple[float, str]:
    if call_iv is None or put_iv is None:
        return 0.0, "neutral"
    skew = put_iv - call_iv
    if skew > 0.03:
        return skew, "bear_skew"
    if skew < -0.03:
        return skew, "bull_skew"
    return skew, "neutral"


def position_size_contracts(
    max_loss_per_contract: float,
    account_size:  float = DEFAULT_ACCOUNT_SIZE,
    max_risk_pct:  float = DEFAULT_MAX_RISK_PCT,
    max_risk_usd:  float = DEFAULT_MAX_RISK_USD,
) -> Tuple[int, float, str]:
    if max_loss_per_contract <= 0:
        return 1, 0.0, "Could not compute sizing"
    budget_pct = account_size * max_risk_pct
    budget     = min(budget_pct, max_risk_usd)
    contracts  = max(1, int(budget / max_loss_per_contract))
    actual     = contracts * max_loss_per_contract
    note = (f"${actual:.0f} risk | {contracts} contract(s) "
            f"[2% cap=${budget_pct:.0f}, hard cap=${max_risk_usd:.0f}]")
    return contracts, actual, note


# ─────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────

def normalize_side(x) -> str:
    s = (str(x) if x is not None else "").strip().lower()
    return "call" if s in ("c", "call") else "put" if s in ("p", "put") else s


def _sg(lst, i, default=None):
    if not isinstance(lst, list) or i < 0 or i >= len(lst):
        return default
    return default if lst[i] is None else lst[i]


def leg_warnings(bid, ask, oi, warn_oi=DEFAULT_WARN_OI, warn_ba=DEFAULT_WARN_BA):
    w = []
    if oi is not None and oi < warn_oi:
        w.append(f"Low OI (<{warn_oi})")
    if bid is not None and ask is not None and (ask - bid) > warn_ba:
        w.append(f"Wide B/A ({ask - bid:.2f})")
    return w


def nearest_strike(strikes: List[float], target: float) -> float:
    return min(strikes, key=lambda x: abs(x - target))


def detect_increment(strikes: List[float]) -> float:
    s = sorted(set(strikes))
    if len(s) < 2:
        return 1.0
    diffs = [round(s[i+1] - s[i], 4) for i in range(len(s)-1) if s[i+1] > s[i]]
    if not diffs:
        return 1.0
    counts = Counter(round(d, 2) for d in diffs)
    return counts.most_common(1)[0][0]


# ─────────────────────────────────────────────────────────
# DATA BUILDERS
# ─────────────────────────────────────────────────────────

def build_quotes(md: Dict, side: str) -> Dict[float, Dict]:
    strikes = md.get("strike", [])
    sides   = md.get("side", [])
    bids    = md.get("bid", [])
    asks    = md.get("ask", [])
    mids    = md.get("mid", None)
    ois     = md.get("openInterest", [])
    deltas  = md.get("delta", None)
    ivs     = md.get("iv", None)

    out = {}
    n = min(len(strikes), len(sides), len(bids), len(asks))
    for i in range(n):
        if normalize_side(_sg(sides, i)) != side:
            continue
        kr = _sg(strikes, i)
        if kr is None:
            continue
        k = float(kr)

        bid = _sg(bids,   i, None)
        ask = _sg(asks,   i, None)
        mid = _sg(mids,   i, None) if isinstance(mids, list) else None
        oi  = _sg(ois,    i, None)
        d   = _sg(deltas, i, None) if isinstance(deltas, list) else None
        iv  = _sg(ivs,    i, None) if isinstance(ivs,    list) else None

        bid = float(bid) if bid is not None else None
        ask = float(ask) if ask is not None else None
        mid = float(mid) if mid is not None else None
        oi  = int(oi)    if oi  is not None else None
        d   = float(d)   if d   is not None else None
        iv  = float(iv)  if iv  is not None else None

        if mid is None and bid is not None and ask is not None:
            mid = (bid + ask) / 2.0
        if mid is None:
            continue

        vol = _sg(marketdata_json.get("volume", []), i, None) if False else None
        # volume is passed separately — handled in build_quotes call
        out[k] = {"strike": k, "mid": mid, "bid": bid, "ask": ask,
                  "oi": oi, "delta": d, "iv": iv,
                  "warnings": leg_warnings(bid, ask, oi),
                  "volume": None}  # populated by build_quotes_with_volume
    return out


def pick_atm_iv(md: Dict, spot: float, side: str) -> Optional[float]:
    strikes = md.get("strike", [])
    sides   = md.get("side", [])
    ivs     = md.get("iv", None)
    if not isinstance(ivs, list):
        return None
    n = min(len(strikes), len(sides), len(ivs))
    idxs = [i for i in range(n)
            if normalize_side(_sg(sides, i)) == side and _sg(ivs, i) is not None]
    if not idxs:
        return None
    bi = min(idxs, key=lambda i: abs(float(strikes[i]) - spot))
    try:
        return float(ivs[bi])
    except Exception:
        return None


# ─────────────────────────────────────────────────────────
# DIRECTION BIAS  (replaces naïve midpoint check)
# ─────────────────────────────────────────────────────────

def compute_direction_bias(
    spot, call_wall, put_wall, net_gex, skew_bias, iv_rank=None
) -> Tuple[str, int, str]:
    """
    Multi-factor direction score.
    Returns (direction, confidence_0_to_100, notes).
    """
    score = 0
    notes = []

    if call_wall is not None and put_wall is not None:
        wall_mid = (call_wall + put_wall) / 2.0
        dist_pct = (spot - wall_mid) / wall_mid * 100.0
        if dist_pct > 0.5:
            score += 2
            notes.append(f"Above wall mid (+{dist_pct:.1f}%)")
        elif dist_pct < -0.5:
            score -= 2
            notes.append(f"Below wall mid ({dist_pct:.1f}%)")
        else:
            notes.append("Pinned at wall mid")

        if abs(call_wall - spot) < abs(spot - put_wall):
            score -= 1
            notes.append("Near call wall (resistance)")
        else:
            score += 1
            notes.append("Near put wall (support)")

    if net_gex >= 0:
        notes.append("+GEX (range-bound)")
    else:
        score += (1 if score >= 0 else -1)   # amplify trend direction
        notes.append("-GEX (trending)")

    if skew_bias == "bull_skew":
        score += 2
        notes.append("Call IV premium → bull skew")
    elif skew_bias == "bear_skew":
        score -= 2
        notes.append("Put IV premium → bear skew")

    if iv_rank is not None and iv_rank > 70:
        notes.append(f"High IVR {iv_rank:.0f} (lower directional edge)")

    direction  = "bull" if score >= 0 else "bear"
    raw_conf   = min(abs(score), 5)
    confidence = 35 + int(raw_conf * 10)
    return direction, confidence, " | ".join(notes)


# ─────────────────────────────────────────────────────────
# STRATEGY SELECTOR
# ─────────────────────────────────────────────────────────

def choose_spread_type(
    direction, iv_atm, iv_rank=None, net_gex=None, skew_bias="neutral", prefer="debit", hv20=None
) -> Tuple[str, str]:
    """
    Returns (spread_type, option_side).

    Correct convention:
      bull debit  → call spread  (buy lower call, sell higher call)
      bull credit → put spread   (sell lower put, buy even-lower put)
      bear debit  → put spread   (buy higher put, sell lower put)
      bear credit → call spread  (sell higher call, buy even-higher call)
    """
    prefer = (prefer or "debit").strip().lower()
    stype = prefer

    stype = prefer

    # HV20 ratio — strongest signal, checked first
    if hv20 is not None and hv20 > 0 and iv_atm is not None:
        ratio = iv_atm / hv20
        if ratio >= 1.30 and prefer == "debit":
            stype = "credit"   # IV rich vs realized → sell premium
        elif ratio <= 0.75:
            stype = "debit"    # IV cheap vs realized → buy premium

    if iv_rank is not None:
        if iv_rank >= IVR_HIGH_FOR_CREDIT:
            stype = "credit"
        elif iv_rank <= IVR_LOW_FOR_DEBIT:
            stype = "debit"
    elif iv_atm is not None:
        if iv_atm >= IV_HIGH_FOR_CREDIT:
            stype = "credit"
        elif iv_atm <= IV_LOW_FOR_DEBIT:
            stype = "debit"

    # GEX refinement when IV rank is neutral
    if net_gex is not None and iv_rank is not None and 30 < iv_rank < 60:
        stype = "credit" if net_gex >= 0 else "debit"

    # Skew alignment
    if skew_bias == "bear_skew" and direction == "bear":
        stype = "credit"
    elif skew_bias == "bull_skew" and direction == "bull":
        stype = "credit"

    side = ("call" if stype == "debit" else "put") if direction == "bull" \
           else ("put" if stype == "debit" else "call")

    return stype, side


# ─────────────────────────────────────────────────────────
# CANDIDATE GENERATION
# ─────────────────────────────────────────────────────────

def build_candidates(
    quotes, spot, iv, dte, direction, spread_type,
    max_width=10.0, max_debit_pct=0.60,
    min_credit_pct=MIN_CREDIT_PCT_WIDTH, min_debit_ror=MIN_DEBIT_ROR,
) -> Tuple[float, List[Dict]]:

    move    = implied_move(spot, iv, dte)
    strikes = sorted(quotes.keys())
    if not strikes:
        return move, []

    inc       = detect_increment(strikes)
    max_steps = max(1, int(round(max_width / inc)))
    out       = []

    if spread_type == "credit":
        k_sigma = K_SIGMA_CREDIT
        # Short strike: OTM by k_sigma * move
        if direction == "bull":
            tgt = spot - k_sigma * move
        else:
            tgt = spot + k_sigma * move

        short_k = nearest_strike(strikes, tgt)
        short_q = quotes.get(short_k)
        if not short_q:
            return move, []

        # Delta gate: walk outward if too close to ATM
        if short_q.get("delta") is not None:
            for k in sorted(strikes, key=lambda x: abs(x - tgt)):
                q = quotes.get(k)
                if q and q.get("delta") is not None:
                    if abs(q["delta"]) <= MAX_SHORT_DELTA_CREDIT:
                        short_k, short_q = k, q
                        break

        for steps in range(1, max_steps + 1):
            w      = round(steps * inc, 4)
            long_k = (short_k - w) if direction == "bull" else (short_k + w)
            long_k = nearest_strike(strikes, long_k)
            long_q = quotes.get(long_k)
            if not long_q:
                continue

            credit = short_q["mid"] - long_q["mid"]
            if credit < min_credit_pct * w:
                continue
            if credit < MIN_CREDIT_ABSOLUTE:
                continue

            mp, ml, ror = vertical_metrics(w, credit=credit)
            if ror is None:
                continue

            pop = delta_to_pop(short_q.get("delta"), "credit")
            out.append({
                "type": "credit", "direction": direction,
                "short": short_k, "long": long_k,
                "side":  "put" if direction == "bull" else "call",
                "width": float(w), "price": float(credit),
                "maxProfit": float(mp), "maxLoss": float(ml),
                "RoR": float(ror), "pop": pop,
                "warnings": short_q["warnings"] + long_q["warnings"],
            })

    else:  # debit — BOTH legs ITM
        itm_depth_long  = 3.0 if spot >= 200 else 2.0
        itm_depth_short = 2.0  # short leg minimum ITM depth

        if direction == "bull":
            # Bull call debit: both legs ITM = both BELOW spot
            # Long leg: deeper ITM (further below spot)
            # Short leg: less deep ITM (closer to spot but still below)
            long_target  = spot - itm_depth_long
            short_target = spot - itm_depth_short

            # Get all ITM call strikes (below spot for calls = ITM)
            itm_strikes = sorted([k for k in strikes if k < spot], reverse=True)
            if len(itm_strikes) < 2:
                return move, []

            # Long leg = deepest ITM strike nearest to target
            long_k = nearest_strike(itm_strikes, long_target)
            long_q = quotes.get(long_k)
            if not long_q:
                return move, []

            # Short leg = less deep ITM, must be between long_k and spot
            short_candidates = [k for k in itm_strikes if k > long_k and k < spot]
            if not short_candidates:
                return move, []

        else:  # bear
            # Bear put debit: both legs ITM = both ABOVE spot
            # Long leg: deeper ITM (further above spot)
            # Short leg: less deep ITM (closer to spot but still above)
            long_target  = spot + itm_depth_long
            short_target = spot + itm_depth_short

            # Get all ITM put strikes (above spot for puts = ITM)
            itm_strikes = sorted([k for k in strikes if k > spot])
            if len(itm_strikes) < 2:
                return move, []

            # Long leg = deepest ITM strike nearest to target
            long_k = nearest_strike(itm_strikes, long_target)
            long_q = quotes.get(long_k)
            if not long_q:
                return move, []

            # Short leg = less deep ITM, must be between long_k and spot
            short_candidates = [k for k in itm_strikes if k < long_k and k > spot]
            if not short_candidates:
                return move, []

        # Build width candidates from short leg options
        out = []
        for short_k in short_candidates:
            short_q = quotes.get(short_k)
            if not short_q:
                continue

            w = abs(long_k - short_k)
            if w <= 0:
                continue

            # Debit = cost to enter
            long_mid  = as_float(long_q.get("mid"),  0)
            short_mid = as_float(short_q.get("mid"), 0)
            debit     = long_mid - short_mid

            if debit <= 0:
                continue

            # 70% cost rule — hard limit
            if debit > 0.70 * w:
                continue

            mp  = w - debit
            ml  = debit
            ror = round(mp / ml, 4) if ml > 0 else 0

            # Minimum RoR gate (adjusted for lower-priced tickers)
            min_ror = adj_min_debit_ror if 'adj_min_debit_ror' in dir() else MIN_DEBIT_ROR
            if ror < min_ror:
                continue

            pop        = delta_to_pop(long_q.get("delta"), "debit")
            long_delta = long_q.get("delta")
            itm_amount = abs(spot - long_k)
            cost_pct   = round(debit / w * 100, 1)

            # ITM depth of short leg
            short_itm  = abs(spot - short_k)

            out.append({
                "type":        "debit",
                "direction":   direction,
                "short":       short_k,
                "long":        long_k,
                "side":        "call" if direction == "bull" else "put",
                "width":       float(w),
                "price":       float(debit),
                "maxProfit":   float(mp),
                "maxLoss":     float(ml),
                "RoR":         float(ror),
                "pop":         pop,
                "long_delta":  long_delta,
                "itm_amount":  round(itm_amount, 2),
                "short_itm":   round(short_itm, 2),
                "cost_pct":    cost_pct,
                "warnings":    long_q["warnings"] + short_q["warnings"],
            })

        return move, out

        # Verify it's actually ITM (not just nearest)
        if direction == "bull" and long_k > spot:
            # Walked OTM — find next strike below spot
            itm_strikes = [k for k in strikes if k < spot]
            if not itm_strikes:
                return move, []
            long_k = max(itm_strikes)
            long_q = quotes.get(long_k)
            if not long_q:
                return move, []
        elif direction == "bear" and long_k < spot:
            itm_strikes = [k for k in strikes if k > spot]
            if not itm_strikes:
                return move, []
            long_k = min(itm_strikes)
            long_q = quotes.get(long_k)
            if not long_q:
                return move, []

        for steps in range(1, max_steps + 1):
            w       = round(steps * inc, 4)
            short_k = (long_k + w) if direction == "bull" else (long_k - w)
            short_k = nearest_strike(strikes, short_k)
            short_q = quotes.get(short_k)
            if not short_q or short_k == long_k:
                continue

            debit = long_q["mid"] - short_q["mid"]
            if debit <= 0 or debit > 0.70 * w:
                continue

            mp, ml, ror = vertical_metrics(w, debit=debit)
            if ror is None or ror < min_debit_ror:
                continue

            pop       = delta_to_pop(long_q.get("delta"), "debit")
            long_delta = long_q.get("delta")
            itm_amount = abs(spot - long_k)
            cost_pct   = round(debit / w * 100, 1)
            out.append({
                "type":       "debit",
                "direction":  direction,
                "short":      short_k,
                "long":       long_k,
                "side":       "call" if direction == "bull" else "put",
                "width":      float(w),
                "price":      float(debit),
                "maxProfit":  float(mp),
                "maxLoss":    float(ml),
                "RoR":        float(ror),
                "pop":        pop,
                "long_delta": long_delta,
                "itm_amount": round(itm_amount, 2),
                "cost_pct":   cost_pct,
                "warnings":   long_q["warnings"] + short_q["warnings"],
            })

    return move, out


# ─────────────────────────────────────────────────────────
# RANKING
# ─────────────────────────────────────────────────────────

def _rank_key(c: Dict) -> float:
    ror   = float(c.get("RoR")   or 0)
    width = float(c.get("width") or 1)
    pop   = float(c.get("pop")   or 0.5)
    price = float(c.get("price") or 0)
    ror_n = ror / max(width, 1.0)
    tie   = -price if c.get("type") == "debit" else price
    return (ROR_WEIGHT * ror_n
            + WIDTH_BONUS_WEIGHT * width
            + POP_WEIGHT * pop
            + PRICE_WEIGHT * tie)


def pick_best(cands, preferred_type="debit"):
    pt = (preferred_type or "debit").lower()
    debit  = [c for c in cands if c.get("type") == "debit"  and c.get("RoR") is not None]
    credit = [c for c in cands if c.get("type") == "credit" and c.get("RoR") is not None]
    bd = max(debit,  key=_rank_key, default=None)
    bc = max(credit, key=_rank_key, default=None)
    return (bd or bc, bc) if pt == "debit" else (bc or bd, bd)


# ─────────────────────────────────────────────────────────
# CONFIDENCE SCORE
# ─────────────────────────────────────────────────────────

def trade_confidence_score(trade, direction_conf, iv_rank, skew_bias, net_gex) -> Tuple[int, List[str]]:
    score   = direction_conf
    reasons = []
    ror     = trade.get("RoR")   or 0
    pop     = trade.get("pop")   or 0
    stype   = trade.get("type",  "")
    tdir    = trade.get("direction", "")

    if ror >= 0.50:
        score += 10; reasons.append(f"Strong RoR {ror:.2f}")
    elif ror >= 0.25:
        score += 5;  reasons.append(f"OK RoR {ror:.2f}")
    else:
        score -= 5;  reasons.append(f"Weak RoR {ror:.2f}")

    if pop >= 0.65:
        score += 10; reasons.append(f"High POP {pop:.0%}")
    elif pop >= 0.50:
        score += 5;  reasons.append(f"Moderate POP {pop:.0%}")

    if iv_rank is not None:
        if stype == "credit" and iv_rank >= IVR_HIGH_FOR_CREDIT:
            score += 10; reasons.append(f"IVR {iv_rank:.0f} supports credit")
        elif stype == "debit" and iv_rank <= IVR_LOW_FOR_DEBIT:
            score += 10; reasons.append(f"IVR {iv_rank:.0f} supports debit")
        elif stype == "credit" and iv_rank < IVR_LOW_FOR_DEBIT:
            score -= 15; reasons.append(f"Low IVR {iv_rank:.0f} — bad for credit")
        elif stype == "debit" and iv_rank > IVR_HIGH_FOR_CREDIT:
            score -= 10; reasons.append(f"High IVR {iv_rank:.0f} — vol crush risk on debit")

    if (skew_bias == "bear_skew" and tdir == "bear") or (skew_bias == "bull_skew" and tdir == "bull"):
        score += 5; reasons.append("Skew aligned")
    elif skew_bias != "neutral":
        score -= 5; reasons.append("Skew opposes trade")

    wc = len(trade.get("warnings") or [])
    if wc:
        score -= wc * 5; reasons.append(f"{wc} liquidity warning(s)")

    return int(min(max(score, 0), 100)), reasons


# ─────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────

def recommend_from_marketdata(
    marketdata_json:  Dict[str, Any],
    direction:        str,
    dte:              int,
    spot:             float,
    net_gex:          Optional[float] = None,
    iv_rank:          Optional[float] = None,
    iv_52w_high:      Optional[float] = None,
    iv_52w_low:       Optional[float] = None,
    prefer:           str             = "debit",
    account_size:     float           = DEFAULT_ACCOUNT_SIZE,
    max_risk_pct:     float           = DEFAULT_MAX_RISK_PCT,
    max_risk_usd:     float           = DEFAULT_MAX_RISK_USD,
    min_confidence:   int             = MIN_CONFIDENCE,
) -> Dict[str, Any]:
    """
    Main entry point. Returns full trade recommendation with position sizing,
    confidence scoring, skew analysis, and IV rank-aware spread selection.
    """
    direction = (direction or "").strip().lower()
    if direction not in ("bull", "bear"):
        return {"ok": False, "reason": "direction must be 'bull' or 'bear'"}

    call_iv = pick_atm_iv(marketdata_json, spot, "call")
    put_iv  = pick_atm_iv(marketdata_json, spot, "put")
    iv_atm  = put_iv or call_iv
    if iv_atm is None:
        return {"ok": False, "reason": "No IV found in chain data"}

    if iv_rank is None and iv_52w_high is not None and iv_52w_low is not None:
        iv_rank = compute_iv_rank(iv_atm, iv_52w_high, iv_52w_low)

    skew_val, skew_bias = skew_score(call_iv, put_iv)

    call_wall = marketdata_json.get("_call_wall")
    put_wall  = marketdata_json.get("_put_wall")
    gex       = net_gex or 0.0

    dir_str, dir_conf, dir_notes = compute_direction_bias(
        spot, call_wall, put_wall, gex, skew_bias, iv_rank
    )

    direction_conflict = (direction != dir_str)

    stype, side = choose_spread_type(
        direction, iv_atm, iv_rank, net_gex, skew_bias, prefer,
        hv20=marketdata_json.get("hv20"),
    )

    quotes = build_quotes(marketdata_json, side)
    if not quotes:
        return {"ok": False, "reason": f"No usable {side} quotes"}

    # Dynamically loosen quality filters for lower-priced tickers
    # (fewer strikes, lower absolute premium = harder to meet fixed thresholds)
    if spot < 50:
        adj_min_credit_pct = 0.08   # 8% floor (vs 15% default)
        adj_min_debit_ror  = 0.15   # 15% RoR floor (vs 25% default)
        adj_max_debit_pct  = 0.75   # allow slightly more expensive debits
    elif spot < 100:
        adj_min_credit_pct = 0.10   # 10% floor
        adj_min_debit_ror  = 0.20   # 20% RoR floor
        adj_max_debit_pct  = 0.70
    else:
        adj_min_credit_pct = MIN_CREDIT_PCT_WIDTH   # default 15%
        adj_min_debit_ror  = MIN_DEBIT_ROR          # default 25%
        adj_max_debit_pct  = float(marketdata_json.get("max_debit_pct", 0.60))

    move, cands = build_candidates(
        quotes, spot, iv_atm, dte, direction, stype,
        max_width      = float(marketdata_json.get("max_width", 10.0)),
        max_debit_pct  = adj_max_debit_pct,
        min_credit_pct = adj_min_credit_pct,
        min_debit_ror  = adj_min_debit_ror,
    )

    if not cands:
        return {
            "ok": False,
            "reason": f"No valid {stype} {side} candidates (check strike range / delta / quality filters)",
            "iv_atm": iv_atm, "iv_rank": iv_rank,
            "skew_bias": skew_bias, "spread_type": stype, "side": side,
        }

    best, alt = pick_best(cands, preferred_type=stype)
    if not best:
        return {"ok": False, "reason": "No spreads passed ranking"}

    confidence, conf_reasons = trade_confidence_score(
        best, dir_conf, iv_rank, skew_bias, gex
    )

    if confidence < min_confidence:
        return {
            "ok": False,
            "reason": f"Confidence {confidence}/100 below threshold {min_confidence}",
            "confidence": confidence, "conf_reasons": conf_reasons,
            "iv_rank": iv_rank, "skew_bias": skew_bias,
            "direction_bias": dir_str, "dir_notes": dir_notes,
        }

    ml_per_contract = best.get("maxLoss", 0) * 100
    contracts, dollar_risk, sizing_note = position_size_contracts(
        ml_per_contract, account_size, max_risk_pct, max_risk_usd
    )

    return {
        "ok":                  True,
        "direction":           direction,
        "direction_bias":      dir_str,
        "direction_conflict":  direction_conflict,
        "dir_notes":           dir_notes,
        "dte":                 dte,
        "spot":                spot,
        "iv_atm":              iv_atm,
        "iv_rank":             iv_rank,
        "skew_val":            round(skew_val, 4),
        "skew_bias":           skew_bias,
        "implied_move_1sigma": move,
        "spread_type":         stype,
        "side":                side,
        "confidence":          confidence,
        "conf_reasons":        conf_reasons,
        "trade":               best,
        "best_alt":            alt,
        "all_candidates":      cands,   # ← add this
        "candidate_count":     len(cands),
        "contracts_suggested": contracts,
        "dollar_risk":         dollar_risk,
        "sizing_note":         sizing_note,
    }
