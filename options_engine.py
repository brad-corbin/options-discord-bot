# options_engine.py
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.

import math
from typing import Dict, Any, List, Tuple, Optional

# ----------------------------
# CONFIG / TUNING (edit as desired)
# ----------------------------
DEFAULT_WARN_OI = 500
DEFAULT_WARN_BA = 0.30

# Strategy selection thresholds
IV_HIGH_FOR_CREDIT = 0.45      # above this, credit spreads tend to be favored
IV_LOW_FOR_DEBIT = 0.22        # below this, debit spreads tend to be favored
NEG_GAMMA_PREFERS_DEBIT = True # negative gamma -> debit bias
POS_GAMMA_PREFERS_CREDIT = True# positive gamma -> credit bias

# Strike selection
K_SIGMA = 0.8                  # target short strike at ~0.8 * 1-sigma move

# Trade selection weighting
# (Used only for ranking candidates within the chosen strategy)
ROR_WEIGHT = 1.0
PRICE_WEIGHT = 0.05            # small tie-breaker preference (cheaper debit / bigger credit)

# ----------------------------
# CORE MATH
# ----------------------------
def implied_move(underlying_price: float, iv: float, dte: int) -> float:
    """1-sigma implied move based on annualized IV and time."""
    return underlying_price * iv * math.sqrt(max(dte, 1) / 365.0)

def vertical_metrics(width: float, debit: float = None, credit: float = None) -> Tuple[float, float, Optional[float]]:
    """
    Returns (max_profit, max_loss, return_on_risk).
    For debit: max_loss = debit, max_profit = width - debit
    For credit: max_profit = credit, max_loss = width - credit
    """
    if debit is not None:
        max_profit = width - debit
        max_loss = debit
        ror = (max_profit / max_loss) if max_loss > 0 else None
        return max_profit, max_loss, ror

    if credit is not None:
        max_profit = credit
        max_loss = width - credit
        ror = (max_profit / max_loss) if max_loss > 0 else None
        return max_profit, max_loss, ror

    raise ValueError("Provide debit or credit")

# ----------------------------
# HELPERS
# ----------------------------
def _safe_get(lst: Any, i: int, default=None):
    if not isinstance(lst, list):
        return default
    if i < 0 or i >= len(lst):
        return default
    v = lst[i]
    return default if v is None else v

def leg_warnings(
    bid: Optional[float],
    ask: Optional[float],
    oi: Optional[int],
    warn_oi: int = DEFAULT_WARN_OI,
    warn_spread: float = DEFAULT_WARN_BA,
) -> List[str]:
    warnings = []
    if oi is not None and oi < warn_oi:
        warnings.append(f"Low OI (<{warn_oi})")
    if bid is not None and ask is not None and (ask - bid) > warn_spread:
        warnings.append(f"Wide B/A (>{warn_spread:.2f})")
    return warnings

def nearest_strike(strikes: List[float], target: float) -> float:
    return min(strikes, key=lambda x: abs(x - target))

def target_short_strike(spot: float, move: float, direction: str, k_sigma: float = K_SIGMA) -> float:
    """
    For bull spreads, short strike is below spot.
    For bear spreads, short strike is above spot.
    """
    if direction == "bull":
        return spot - k_sigma * move
    if direction == "bear":
        return spot + k_sigma * move
    raise ValueError("direction must be 'bull' or 'bear'")

def normalize_side(x: Any) -> str:
    """Normalize 'c'/'p'/'call'/'put' to 'call'/'put'."""
    s = (str(x) if x is not None else "").strip().lower()
    if s in ("c", "call"):
        return "call"
    if s in ("p", "put"):
        return "put"
    return s

# ----------------------------
# DATA BUILDERS
# ----------------------------
def build_quotes_by_strike(marketdata_json: Dict[str, Any], side: str) -> Dict[float, Dict[str, Any]]:
    """
    Build dict strike -> {mid,bid,ask,oi,warnings} for a given side ('call' or 'put').

    Robust if 'mid' missing (uses (bid+ask)/2 when possible).
    """
    strikes = marketdata_json.get("strike", [])
    sides   = marketdata_json.get("side", [])
    mids    = marketdata_json.get("mid", None)
    bids    = marketdata_json.get("bid", [])
    asks    = marketdata_json.get("ask", [])
    ois     = marketdata_json.get("openInterest", [])

    out: Dict[float, Dict[str, Any]] = {}

    # Use minimum common length; mid may be missing
    n = min(len(strikes), len(sides), len(bids), len(asks))
    if n <= 0:
        return out

    for i in range(n):
        if normalize_side(sides[i]) != side:
            continue

        k_raw = strikes[i]
        if k_raw is None:
            continue
        k = float(k_raw)

        bid = _safe_get(bids, i, None)
        ask = _safe_get(asks, i, None)
        mid = _safe_get(mids, i, None) if isinstance(mids, list) else None
        oi  = _safe_get(ois, i, None)
        oi  = int(oi) if oi is not None else None

        bid = float(bid) if bid is not None else None
        ask = float(ask) if ask is not None else None
        mid = float(mid) if mid is not None else None

        if mid is None and bid is not None and ask is not None:
            mid = (bid + ask) / 2.0

        if mid is None:
            continue

        warnings = leg_warnings(bid, ask, oi)
        out[k] = {
            "strike": k,
            "mid": mid,
            "bid": bid,
            "ask": ask,
            "oi": oi,
            "warnings": warnings,
        }

    return out

def pick_atm_iv(marketdata_json: Dict[str, Any], spot: float, side: str) -> Optional[float]:
    """
    Pick IV from the strike closest to spot for the given side.
    """
    strikes = marketdata_json.get("strike", [])
    sides   = marketdata_json.get("side", [])
    ivs     = marketdata_json.get("iv", None)

    if not isinstance(ivs, list) or not ivs:
        return None

    n = min(len(strikes), len(sides), len(ivs))
    idxs = [i for i in range(n) if normalize_side(sides[i]) == side and ivs[i] is not None]
    if not idxs:
        return None

    best_i = min(idxs, key=lambda i: abs(float(strikes[i]) - spot))
    try:
        return float(ivs[best_i])
    except Exception:
        return None

# ----------------------------
# STRATEGY SELECTOR
# ----------------------------
def choose_spread_type(
    direction: str,
    iv_atm: float,
    net_gex: Optional[float] = None,
    prefer: str = "debit",
) -> str:
    """
    Returns "debit" or "credit".

    Heuristics:
    - User preference default: debit
    - Negative gamma (net_gex < 0) -> debit bias
    - Positive gamma (net_gex > 0) -> credit bias
    - Very high IV -> credit
    - Very low IV -> debit
    """
    prefer = (prefer or "debit").strip().lower()
    if prefer not in ("debit", "credit"):
        prefer = "debit"

    # IV overrides (strong signals)
    if iv_atm is not None:
        if iv_atm >= IV_HIGH_FOR_CREDIT:
            return "credit"
        if iv_atm <= IV_LOW_FOR_DEBIT:
            return "debit"

    # Gamma regime preference
    if net_gex is not None:
        if net_gex < 0 and NEG_GAMMA_PREFERS_DEBIT:
            return "debit"
        if net_gex > 0 and POS_GAMMA_PREFERS_CREDIT:
            return "credit"

    return prefer

# ----------------------------
# CANDIDATE GENERATION
# ----------------------------
def build_vertical_candidates(
    quotes_by_strike: Dict[float, Dict[str, Any]],
    spot: float,
    iv: float,
    dte: int,
    direction: str,
    max_width: float = 5.0,
    max_debit_pct: float = 0.70,
) -> Tuple[float, List[Dict[str, Any]]]:

    move = implied_move(spot, iv, dte)
    tgt = target_short_strike(spot, move, direction, k_sigma=K_SIGMA)

    strikes = sorted(quotes_by_strike.keys())
    if not strikes:
        return move, []

    short_k = nearest_strike(strikes, tgt)

    widths = [1, 2, 3, 4, 5]
    out: List[Dict[str, Any]] = []

    for w in widths:
        if w > max_width:
            continue

        if direction == "bull":
            long_k = short_k - w
        else:
            long_k = short_k + w

        if long_k not in quotes_by_strike:
            continue

        short_q = quotes_by_strike[short_k]
        long_q  = quotes_by_strike[long_k]

        credit_mid = short_q["mid"] - long_q["mid"]
        debit_mid  = long_q["mid"] - short_q["mid"]

        # CREDIT candidate
        if credit_mid > 0:
            mp, ml, ror = vertical_metrics(w, credit=credit_mid)
            out.append({
                "type": "credit",
                "direction": direction,
                "short": short_k,
                "long": long_k,
                "width": float(w),
                "price": float(credit_mid),
                "maxProfit": float(mp),
                "maxLoss": float(ml),
                "RoR": float(ror) if ror is not None else None,
                "warnings": short_q["warnings"] + long_q["warnings"],
            })

        # DEBIT candidate
        if debit_mid > 0 and debit_mid <= (max_debit_pct * w):
            mp, ml, ror = vertical_metrics(w, debit=debit_mid)
            out.append({
                "type": "debit",
                "direction": direction,
                "short": short_k,
                "long": long_k,
                "width": float(w),
                "price": float(debit_mid),
                "maxProfit": float(mp),
                "maxLoss": float(ml),
                "RoR": float(ror) if ror is not None else None,
                "warnings": short_q["warnings"] + long_q["warnings"],
            })

    return move, out

# ----------------------------
# RANKING / SELECTION
# ----------------------------
def _rank_key(c: Dict[str, Any]) -> Tuple[float, float]:
    """
    Higher is better.
    - Primary: RoR (Return on Risk)
    - Secondary: for debit prefer cheaper (lower price), for credit prefer larger credit (higher price)
    """
    ror = c.get("RoR")
    ror_v = float(ror) if isinstance(ror, (int, float)) else -1e9

    price = c.get("price")
    price_v = float(price) if isinstance(price, (int, float)) else 0.0

    if c.get("type") == "debit":
        # cheaper debit is better for tie-breaks
        tie = -price_v
    else:
        # higher credit is better for tie-breaks
        tie = price_v

    return (ROR_WEIGHT * ror_v, PRICE_WEIGHT * tie)

def pick_best_trade(
    cands: List[Dict[str, Any]],
    preferred_type: str = "debit",
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Returns (best_trade, best_alt_other_type)
    - Always prefers preferred_type if it exists.
    - If none exist, falls back to the other type.
    """
    preferred_type = (preferred_type or "debit").lower().strip()
    if preferred_type not in ("debit", "credit"):
        preferred_type = "debit"

    debit = [c for c in cands if c.get("type") == "debit" and c.get("RoR") is not None]
    credit = [c for c in cands if c.get("type") == "credit" and c.get("RoR") is not None]

    best_debit = max(debit, key=_rank_key, default=None)
    best_credit = max(credit, key=_rank_key, default=None)

    if preferred_type == "debit":
        return (best_debit or best_credit), best_credit
    else:
        return (best_credit or best_debit), best_debit

# ----------------------------
# PUBLIC API
# ----------------------------
def recommend_from_marketdata(
    marketdata_json: Dict[str, Any],
    direction: str,  # 'bull' or 'bear'
    dte: int,
    spot: float,
    net_gex: Optional[float] = None,   # optional: pass from app.py for smarter selection
    prefer: str = "debit",             # default user preference
) -> Dict[str, Any]:
    """
    Recommends a vertical spread that fits your rules.
    - Bulls: put spreads (bull put credit or bull put debit depending on choice)
    - Bears: call spreads (bear call credit or bear call debit depending on choice)

    NOTE: In practice, "bull debit put" and "bear debit call" are less common than debit call/put
    in the same direction. Here we keep your original framework (bull->puts, bear->calls)
    and simply choose debit vs credit based on regime/IV.
    """
    direction = (direction or "").strip().lower()
    if direction not in ("bull", "bear"):
        return {"ok": False, "reason": "direction must be 'bull' or 'bear'"}

    side = "put" if direction == "bull" else "call"
    quotes_by_strike = build_quotes_by_strike(marketdata_json, side=side)

    if not quotes_by_strike:
        return {"ok": False, "reason": f"No usable {side} quotes (need bid/ask or mid)"}

    iv = pick_atm_iv(marketdata_json, spot=spot, side=side)
    if iv is None:
        return {"ok": False, "reason": "No IV found in response (expected key: 'iv')"}

    move, cands = build_vertical_candidates(
        quotes_by_strike=quotes_by_strike,
        spot=spot,
        iv=iv,
        dte=dte,
        direction=direction,
        max_width=float(marketdata_json.get("max_width", 5.0)) if marketdata_json.get("max_width") is not None else 5.0,
        max_debit_pct=float(marketdata_json.get("max_debit_pct", 0.70)) if marketdata_json.get("max_debit_pct") is not None else 0.70,
    )

    if not cands:
        return {"ok": False, "reason": "No vertical candidates generated (missing strikes/quotes)"}

    preferred_type = choose_spread_type(
        direction=direction,
        iv_atm=iv,
        net_gex=net_gex,
        prefer=prefer,
    )

    best, alt_other = pick_best_trade(cands, preferred_type=preferred_type)
    if not best:
        return {"ok": False, "reason": "No spreads matched rules"}

    return {
        "ok": True,
        "direction": direction,
        "dte": dte,
        "spot": spot,
        "iv_atm": iv,
        "implied_move_1sigma": move,
        "preferred_spread_type": preferred_type,  # "debit" or "credit"
        "trade": best,
        "best_alt_other_type": alt_other,
        "candidate_count": len(cands),
    }
