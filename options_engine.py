# options_engine.py
import math
from typing import Dict, Any, List, Tuple, Optional

def implied_move(underlying_price: float, iv: float, dte: int) -> float:
    return underlying_price * iv * math.sqrt(dte / 365.0)

def leg_warnings(bid: float, ask: float, oi: int, warn_oi=500, warn_spread=0.30) -> List[str]:
    warnings = []
    if oi is not None and oi < warn_oi:
        warnings.append(f"Low OI (<{warn_oi})")
    if bid is not None and ask is not None and (ask - bid) > warn_spread:
        warnings.append(f"Wide B/A (>{warn_spread:.2f})")
    return warnings

def vertical_metrics(width: float, debit: float = None, credit: float = None) -> Tuple[float, float, Optional[float]]:
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

def choose_credit_if_better(ror_debit: Optional[float], ror_credit: Optional[float], edge=0.05) -> bool:
    if ror_debit is None or ror_credit is None:
        return False
    return ror_credit >= ror_debit * (1.0 + edge)

def nearest_strike(strikes: List[float], target: float) -> float:
    return min(strikes, key=lambda x: abs(x - target))

def target_short_strike(spot: float, move: float, direction: str, k_sigma: float=0.8) -> float:
    if direction == "bull":
        return spot - k_sigma * move
    if direction == "bear":
        return spot + k_sigma * move
    raise ValueError("direction must be 'bull' or 'bear'")

def build_quotes_by_strike(marketdata_json: Dict[str, Any], side: str) -> Dict[float, Dict[str, Any]]:
    strikes = marketdata_json["strike"]
    sides = marketdata_json["side"]
    mids = marketdata_json.get("mid", [])
    bids = marketdata_json.get("bid", [])
    asks = marketdata_json.get("ask", [])
    ois  = marketdata_json.get("openInterest", [])

    out: Dict[float, Dict[str, Any]] = {}

    for i in range(len(strikes)):
        if sides[i] != side:
            continue

        k = float(strikes[i])
        bid = float(bids[i]) if bids[i] is not None else None
        ask = float(asks[i]) if asks[i] is not None else None
        mid = float(mids[i]) if mids[i] is not None else None
        oi  = int(ois[i]) if (i < len(ois) and ois[i] is not None) else None

        if mid is None and bid is not None and ask is not None:
            mid = (bid + ask) / 2.0

        if mid is None:
            continue

        warnings = leg_warnings(bid or 0.0, ask or 0.0, oi or 0)
        out[k] = {"strike": k, "mid": mid, "bid": bid, "ask": ask, "oi": oi, "warnings": warnings}

    return out

def pick_atm_iv(marketdata_json: Dict[str, Any], spot: float, side: str) -> Optional[float]:
    strikes = marketdata_json["strike"]
    sides = marketdata_json["side"]
    ivs = marketdata_json.get("iv")

    if not ivs:
        return None

    idxs = [i for i in range(len(strikes)) if sides[i] == side and ivs[i] is not None]
    if not idxs:
        return None

    best_i = min(idxs, key=lambda i: abs(float(strikes[i]) - spot))
    return float(ivs[best_i])

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
    tgt = target_short_strike(spot, move, direction, k_sigma=0.8)

    strikes = sorted(quotes_by_strike.keys())
    short_k = nearest_strike(strikes, tgt)

    widths = [1, 2, 3, 4, 5]
    out = []

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

        if credit_mid > 0:
            mp, ml, ror = vertical_metrics(w, credit=credit_mid)
            out.append({
                "type": "credit", "direction": direction,
                "short": short_k, "long": long_k, "width": float(w),
                "price": float(credit_mid), "maxProfit": float(mp), "maxLoss": float(ml),
                "RoR": float(ror) if ror is not None else None,
                "warnings": short_q["warnings"] + long_q["warnings"],
            })

        if debit_mid > 0 and debit_mid <= (max_debit_pct * w):
            mp, ml, ror = vertical_metrics(w, debit=debit_mid)
            out.append({
                "type": "debit", "direction": direction,
                "short": short_k, "long": long_k, "width": float(w),
                "price": float(debit_mid), "maxProfit": float(mp), "maxLoss": float(ml),
                "RoR": float(ror) if ror is not None else None,
                "warnings": short_q["warnings"] + long_q["warnings"],
            })

    return move, out

def pick_best_trade(cands: List[Dict[str, Any]], credit_edge: float = 0.05):
    debit = [c for c in cands if c["type"] == "debit" and c["RoR"] is not None]
    credit = [c for c in cands if c["type"] == "credit" and c["RoR"] is not None]

    best_debit = max(debit, key=lambda c: (c["RoR"], -c["price"]), default=None)
    best_credit = max(credit, key=lambda c: c["RoR"], default=None)

    if best_debit and best_credit:
        if choose_credit_if_better(best_debit["RoR"], best_credit["RoR"], edge=credit_edge):
            return best_credit, best_debit

    return (best_debit or best_credit), best_debit

def recommend_from_marketdata(
    marketdata_json: Dict[str, Any],
    direction: str,  # 'bull' or 'bear'
    dte: int,
    spot: float,
) -> Dict[str, Any]:

    side = "put" if direction == "bull" else "call"
    quotes_by_strike = build_quotes_by_strike(marketdata_json, side=side)

    if not quotes_by_strike:
        return {"ok": False, "reason": f"No usable {side} quotes (missing mids?)"}

    iv = pick_atm_iv(marketdata_json, spot=spot, side=side)
    if iv is None:
        return {"ok": False, "reason": "No IV found in response (expected key: 'iv')"}

    move, cands = build_vertical_candidates(
        quotes_by_strike=quotes_by_strike,
        spot=spot,
        iv=iv,
        dte=dte,
        direction=direction,
        max_width=5.0,
        max_debit_pct=0.70,
    )

    best, best_debit_alt = pick_best_trade(cands, credit_edge=0.05)
    if not best:
        return {"ok": False, "reason": "No spreads matched rules"}

    return {
        "ok": True,
        "direction": direction,
        "dte": dte,
        "spot": spot,
        "iv_atm": iv,
        "implied_move_1sigma": move,
        "trade": best,
        "best_debit_alt": best_debit_alt,
        "candidate_count": len(cands),
    }
