import math
import time
from typing import Dict, List, Optional


def _as_float(value, default=0.0):
    try:
        if value is None:
            return default
        if isinstance(value, list):
            value = value[0] if value else default
        return float(value)
    except Exception:
        return default


def _ema(values, length: int):
    vals = [float(v) for v in (values or []) if v is not None]
    if not vals:
        return None
    alpha = 2.0 / (length + 1.0)
    ema = vals[0]
    for v in vals[1:]:
        ema = alpha * v + (1 - alpha) * ema
    return ema


def _rsi(values, length: int = 14):
    vals = [float(v) for v in (values or []) if v is not None]
    if len(vals) < length + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(vals)):
        d = vals[i] - vals[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length
    for i in range(length, len(gains)):
        avg_gain = (avg_gain * (length - 1) + gains[i]) / length
        avg_loss = (avg_loss * (length - 1) + losses[i]) / length
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _mfi(rows, length: int = 14):
    rows = rows or []
    if len(rows) < length + 1:
        return None
    pos = []
    neg = []
    prev_tp = None
    for r in rows:
        tp = (_as_float(r.get("high")) + _as_float(r.get("low")) + _as_float(r.get("close"))) / 3.0
        mf = tp * _as_float(r.get("volume"))
        if prev_tp is not None:
            if tp > prev_tp:
                pos.append(mf)
                neg.append(0.0)
            elif tp < prev_tp:
                pos.append(0.0)
                neg.append(mf)
            else:
                pos.append(0.0)
                neg.append(0.0)
        prev_tp = tp
    if len(pos) < length:
        return None
    pmf = sum(pos[-length:])
    nmf = sum(neg[-length:])
    if nmf == 0:
        return 100.0
    ratio = pmf / nmf
    return 100.0 - (100.0 / (1.0 + ratio))


def _fmt_money(x) -> str:
    try:
        return f"${float(x):.2f}"
    except Exception:
        return "—"


def _calc_ann_rv_from_closes(closes: list, window: int = 20) -> float | None:
    try:
        vals = [float(x) for x in closes if x is not None and float(x) > 0]
        if len(vals) < window + 1:
            return None
        vals = vals[-(window + 1):]
        rets = []
        for i in range(1, len(vals)):
            prev = vals[i - 1]
            cur = vals[i]
            if prev > 0 and cur > 0:
                rets.append(math.log(cur / prev))
        if len(rets) < max(3, window - 1):
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / max(len(rets) - 1, 1)
        return math.sqrt(var) * math.sqrt(252) * 100.0
    except Exception:
        return None


def build_canonical_vol_regime(
    ticker: str = "SPY",
    candle_closes: Optional[list] = None,
    market: Optional[dict] = None,
    fetch_vix9d_fn=None,
    get_vix_ma200_fn=None,
    get_vvix_value_fn=None,
    now_ts: Optional[float] = None,
) -> dict:
    ticker = (ticker or "SPY").upper()
    now_ts = now_ts or time.time()
    market = market or {}

    vix = _as_float(market.get("vix"), None)
    vix9d = _as_float(market.get("vix9d"), 0.0)
    source = str(market.get("source") or "unknown")
    inferred = bool(market.get("inferred"))
    if not vix9d and fetch_vix9d_fn:
        try:
            vix9d = _as_float(fetch_vix9d_fn("^VIX9D"), 0.0)
        except Exception:
            vix9d = 0.0
    term = (market.get("term") or "unknown").lower()
    if term == "unknown" and vix is not None and vix9d:
        slope = vix9d - vix
        if slope < -0.75:
            term = "normal"
        elif slope > 0.75:
            term = "inverted"
        else:
            term = "flat"

    vix_ma200 = None
    if get_vix_ma200_fn:
        try:
            vix_ma200 = get_vix_ma200_fn()
        except Exception:
            vix_ma200 = None
    above_ma200 = bool(vix_ma200 and vix is not None and vix > vix_ma200)

    vvix = None
    if get_vvix_value_fn:
        try:
            vvix = get_vvix_value_fn()
        except Exception:
            vvix = None

    closes = candle_closes or []
    rv5 = _calc_ann_rv_from_closes(closes, 5)
    rv20 = _calc_ann_rv_from_closes(closes, 20)

    if vix is None or vix <= 0:
        label = "UNKNOWN"
        base = "UNKNOWN"
        caution = 0
        size_mult = 0.75
        posture = "Volatility regime unavailable. Reduce size and use defined-risk setups until live vol is confirmed."
        confidence = "LOW"
        vvix_warning = False
        rv_spike = bool(rv5 and rv20 and rv5 > (rv20 * 1.35))
        transition_warning = False
        emoji = "❓"
    else:
        if vix < 15:
            base = "LOW"
            caution = 0
        elif vix < 20:
            base = "NORMAL"
            caution = 1
        elif vix < 30:
            base = "ELEVATED"
            caution = 3
        else:
            base = "CRISIS"
            caution = 5

        if above_ma200:
            caution += 1
        if term == "flat":
            caution += 1
        elif term == "inverted":
            caution += 2

        vvix_warning = bool(vvix and vvix >= 120)
        if vvix_warning:
            caution += 1

        rv_spike = bool(rv5 and rv20 and rv5 > (rv20 * 1.35))
        if rv_spike:
            caution += 1

        transition_warning = False
        if base in ("LOW", "NORMAL") and (above_ma200 or term in ("flat", "inverted") or vvix_warning or rv_spike):
            transition_warning = True
        if base == "ELEVATED" and term == "inverted" and vvix_warning:
            transition_warning = True

        if base == "CRISIS" or caution >= 6:
            label = "CRISIS"
            size_mult = 0.35
            posture = "Capital preservation. Only best defined-risk setups."
            confidence = "HIGH"
        elif base == "ELEVATED" or caution >= 4:
            label = "ELEVATED"
            size_mult = 0.60
            posture = "Reduce size. Favor defined-risk and cleaner directional setups."
            confidence = "HIGH" if caution >= 5 else "MODERATE"
        elif transition_warning:
            label = "TRANSITION"
            size_mult = 0.75
            posture = "Transition warning. Smaller size and stricter setup quality."
            confidence = "MODERATE"
        elif base == "LOW":
            label = "LOW"
            size_mult = 1.00
            posture = "Calm conditions. Directional setups okay if dealer/structure agrees."
            confidence = "MODERATE"
        else:
            label = "NORMAL"
            size_mult = 0.90
            posture = "Balanced environment. Defined-risk preferred."
            confidence = "MODERATE"

        if label in ("TRANSITION", "ELEVATED", "CRISIS"):
            emoji = "⚠️" if label == "TRANSITION" else "🔶" if label == "ELEVATED" else "🚨"
        else:
            emoji = "🟢" if label == "LOW" else "🟡"

    term_slope = (vix9d - vix) if (vix9d and vix is not None) else None
    return {
        "ticker": ticker,
        "label": label,
        "base": base,
        "emoji": emoji,
        "vix": vix,
        "vix9d": vix9d if vix9d > 0 else None,
        "term_structure": term,
        "term_slope": round(term_slope, 2) if term_slope is not None else None,
        "vix_ma200": round(vix_ma200, 2) if vix_ma200 else None,
        "above_ma200": above_ma200,
        "vvix": round(vvix, 1) if vvix else None,
        "vvix_warning": vvix_warning,
        "rv5": round(rv5, 1) if rv5 else None,
        "rv20": round(rv20, 1) if rv20 else None,
        "rv_spike": rv_spike,
        "transition_warning": transition_warning,
        "caution_score": int(caution),
        "size_mult": size_mult,
        "posture": posture,
        "description": posture,
        "confidence": confidence,
        "source": source,
        "inferred": inferred,
        "has_live_vix": bool(vix is not None and vix > 0),
        "ts": now_ts,
    }



def format_canonical_vol_line(vol_regime: dict) -> str:
    if not vol_regime:
        return ""
    bits = [f"{vol_regime.get('emoji', '🌡️')} {vol_regime.get('label', 'UNKNOWN')}"]
    vix = vol_regime.get("vix")
    if vix is not None and float(vix) > 0:
        bits.append(f"VIX {float(vix):.1f}")
    else:
        bits.append("VIX unavailable")
    if vol_regime.get("above_ma200"):
        bits.append("> MA200")
    term = vol_regime.get("term_structure")
    if term and term != "unknown":
        bits.append(f"term {term}")
    vvix = vol_regime.get("vvix")
    if vvix:
        bits.append(f"VVIX {vvix:.0f}")
    if vol_regime.get("transition_warning"):
        bits.append("transition warning")
    src = vol_regime.get("source")
    if src and src != "unknown":
        bits.append(f"src {src}")
    if vol_regime.get("inferred"):
        bits.append("inferred")
    return " | ".join(bits)



def resolve_canonical_dealer_regime(
    eng: Optional[dict] = None,
    cagf: Optional[dict] = None,
    spot: float = 0,
    v4_flow: Optional[dict] = None,
) -> dict:
    """
    Canonical dealer regime resolver used by EM/scalp/swing shared snapshots.
    This mirrors the prior card formatter behavior but lives in the shared model
    layer so all engines can consume the same regime object.
    """
    eng = eng or {}
    cagf = cagf or {}
    v4_flow = v4_flow or {}

    gex = _as_float(v4_flow.get("gex"), _as_float(eng.get("gex"), 0.0))
    flip = v4_flow.get("gamma_flip") or eng.get("flip_price") or eng.get("gamma_flip")
    gex_negative = gex < 0

    result = {
        "label": "UNKNOWN",
        "source": "none",
        "description": "Insufficient data to determine regime.",
        "allows_debit_spreads": True,
        "allows_credit_spreads": True,
        "gex_raw_negative": gex_negative,
        "flip_price": flip,
        "spot_vs_flip": "unknown",
    }

    if flip and spot > 0:
        result["spot_vs_flip"] = "above" if spot > float(flip) else "below"

    cagf_regime = str(cagf.get("regime") or "").upper()
    v4_regime = str(v4_flow.get("composite_regime") or "").upper()
    trend_prob = _as_float(cagf.get("trend_day_probability"), 0.0)

    if cagf_regime and cagf_regime != "UNKNOWN":
        if cagf_regime == "TRENDING":
            result.update({
                "label": "TRENDING",
                "source": "institutional flow model",
                "description": "Dealers are positioned for directional moves. Price trends tend to accelerate.",
                "allows_debit_spreads": True,
                "allows_credit_spreads": False,
            })
        elif cagf_regime == "SUPPRESSING":
            desc = "Dealers suppress price moves in this zone. Ranges tend to hold."
            label = "SUPPRESSING"
            if gex_negative:
                label = "MIXED"
                desc = (
                    "The flow model leans suppressing (above gamma flip), but raw dealer gamma is still negative. "
                    "Moves may be dampened but breakout risk remains."
                )
            result.update({
                "label": label,
                "source": "institutional flow model",
                "description": desc,
                "allows_debit_spreads": trend_prob >= 0.45,
                "allows_credit_spreads": True,
            })
        else:
            result.update({
                "label": "NEUTRAL",
                "source": "institutional flow model",
                "description": "No strong regime signal. Mixed conditions.",
                "allows_debit_spreads": True,
                "allows_credit_spreads": True,
            })
    elif v4_regime and v4_regime != "UNKNOWN":
        raw_label = v4_regime
        mapped = raw_label
        desc = "v4 composite dealer regime."
        allows_debit = True
        allows_credit = True

        if "PIN" in raw_label or raw_label.startswith("SUPPRESSING"):
            desc = "v4 composite says price is pinned / suppressing near dealer structure."
            allows_debit = False
            allows_credit = True
        elif "TREND" in raw_label or "EXPLOSIVE" in raw_label:
            desc = "v4 composite says dealer positioning is amplifying moves."
            allows_debit = True
            allows_credit = False
        elif raw_label == "MIXED":
            desc = "v4 composite shows mixed dealer conditions."
        elif raw_label == "NEUTRAL":
            desc = "v4 composite shows no strong dealer regime edge."

        result.update({
            "label": mapped,
            "source": "v4 composite",
            "description": desc,
            "allows_debit_spreads": allows_debit,
            "allows_credit_spreads": allows_credit,
        })
    elif eng:
        if gex_negative:
            result.update({
                "label": "TRENDING",
                "source": "raw GEX",
                "description": "Negative dealer gamma — moves can accelerate. Directional strategies are favored.",
                "allows_debit_spreads": True,
                "allows_credit_spreads": False,
            })
        else:
            result.update({
                "label": "SUPPRESSING",
                "source": "raw GEX",
                "description": "Positive dealer gamma — moves tend to be dampened. Range-bound strategies may work better.",
                "allows_debit_spreads": False,
                "allows_credit_spreads": True,
            })

    if result.get("label") in ("TRENDING", "SUPPRESSING") and v4_regime and v4_regime not in (result.get("label"), "UNKNOWN"):
        result["description"] = f"{result.get('description', '')} v4 composite={v4_regime}.".strip()

    return result


def build_canonical_dealer_snapshot(
    ticker: str,
    spot: float,
    eng: Optional[dict] = None,
    cagf: Optional[dict] = None,
    v4_flow: Optional[dict] = None,
    walls: Optional[dict] = None,
    dealer_regime: Optional[dict] = None,
) -> dict:
    """Owns raw dealer-structure creation for shared-model consumers."""
    eng = eng or {}
    cagf = cagf or {}
    v4_flow = v4_flow or {}
    walls = walls or {}

    regime = dict(dealer_regime or {})
    if not regime or not regime.get("label"):
        regime = resolve_canonical_dealer_regime(eng=eng, cagf=cagf, spot=spot, v4_flow=v4_flow)
    else:
        regime.setdefault("flip_price", v4_flow.get("gamma_flip") or eng.get("flip_price") or eng.get("gamma_flip"))
        if regime.get("flip_price") and spot > 0 and regime.get("spot_vs_flip") not in ("above", "below"):
            regime["spot_vs_flip"] = "above" if spot > float(regime["flip_price"]) else "below"
        regime.setdefault("source", "precomputed")
        regime.setdefault("description", "")

    flip = regime.get("flip_price") or v4_flow.get("gamma_flip") or eng.get("flip_price") or eng.get("gamma_flip")
    max_pain = walls.get("max_pain")
    if max_pain is None:
        max_pain = eng.get("max_pain")

    call_wall = walls.get("call_wall")
    if call_wall is None:
        call_wall = eng.get("call_wall")
    put_wall = walls.get("put_wall")
    if put_wall is None:
        put_wall = eng.get("put_wall")
    gamma_wall = walls.get("gamma_wall")
    if gamma_wall is None:
        gamma_wall = eng.get("gamma_wall") or flip

    pin_zone_low = put_wall if put_wall is not None else max_pain
    pin_zone_high = call_wall if call_wall is not None else max_pain

    out = {
        "ticker": ticker,
        "spot": spot,
        "label": str(regime.get("label") or regime.get("regime") or "UNKNOWN").upper(),
        "regime": str(regime.get("label") or regime.get("regime") or "UNKNOWN").upper(),
        "source": regime.get("source") or "unknown",
        "description": regime.get("description") or "",
        "allows_debit_spreads": regime.get("allows_debit_spreads", True),
        "allows_credit_spreads": regime.get("allows_credit_spreads", True),
        "gex_raw_negative": bool(regime.get("gex_raw_negative", _as_float(v4_flow.get("gex"), _as_float(eng.get("gex"), 0.0)) < 0)),
        "flip_price": _as_float(flip, None),
        "spot_vs_flip": regime.get("spot_vs_flip") or "unknown",
        "distance_to_flip": round(abs(float(spot) - float(flip)), 2) if spot and flip is not None else None,
        "gex": _as_float(v4_flow.get("gex"), _as_float(eng.get("gex"), None)),
        "dex": _as_float(v4_flow.get("dex"), _as_float(eng.get("dex"), None)),
        "vanna": _as_float(v4_flow.get("vanna"), _as_float(eng.get("vanna"), None)),
        "charm": _as_float(v4_flow.get("charm"), _as_float(eng.get("charm"), None)),
        "bias": v4_flow.get("bias") or cagf.get("bias") or None,
        "bias_score": v4_flow.get("bias_score") if v4_flow.get("bias_score") is not None else cagf.get("bias_score"),
        "max_pain": _as_float(max_pain, None),
        "call_wall": _as_float(call_wall, None),
        "put_wall": _as_float(put_wall, None),
        "gamma_wall": _as_float(gamma_wall, None),
        "pin_zone_low": _as_float(pin_zone_low, None),
        "pin_zone_high": _as_float(pin_zone_high, None),
        "confidence_label": v4_flow.get("confidence_label") or None,
        "downgrades": list(v4_flow.get("downgrades") or []),
    }

    near_threshold = max(float(spot or 0) * 0.0025, 0.75) if spot else 0.75
    out["near_flip"] = bool(out.get("flip_price") is not None and out.get("distance_to_flip") is not None and out.get("distance_to_flip") <= near_threshold)
    out["near_max_pain"] = bool(out.get("max_pain") is not None and abs(float(spot or 0) - float(out.get("max_pain"))) <= near_threshold) if spot and out.get("max_pain") is not None else False
    return out


def _dealer_horizon_label(mode: str = "scalp", horizon_label: Optional[str] = None) -> str:
    if horizon_label:
        return str(horizon_label)
    mode = str(mode or "scalp").lower()
    mapping = {
        "em": "front expiry",
        "scalp": "front expiry",
        "swing": "7-14 DTE",
        "monitor_long": "~21 DTE",
        "monitor_short": "position expiry",
        "monitor": "position expiry",
    }
    return mapping.get(mode, mode.replace("_", " "))


def resolve_effective_trade_regime(
    dealer_snapshot: Optional[dict],
    structure: Optional[dict] = None,
    vol_regime: Optional[dict] = None,
    mode: str = "scalp",
    spot: float = 0.0,
    horizon_label: Optional[str] = None,
) -> dict:
    dealer_snapshot = dealer_snapshot or {}
    structure = structure or {}
    vol_regime = vol_regime or {}
    raw_label = str(dealer_snapshot.get("label") or dealer_snapshot.get("regime") or "UNKNOWN").upper()
    horizon = _dealer_horizon_label(mode, horizon_label)

    structure_score = structure.get("overlay_score")
    try:
        structure_score = int(structure_score) if structure_score is not None else None
    except Exception:
        structure_score = None

    balance_low = _as_float(structure.get("balance_zone_low"), None)
    balance_high = _as_float(structure.get("balance_zone_high"), None)
    outer_low = _as_float(structure.get("outer_bracket_low"), None)
    outer_high = _as_float(structure.get("outer_bracket_high"), None)
    confluence = structure.get("confluence")

    pin_low = _as_float(dealer_snapshot.get("pin_zone_low"), None)
    pin_high = _as_float(dealer_snapshot.get("pin_zone_high"), None)
    call_wall = _as_float(dealer_snapshot.get("call_wall"), None)
    put_wall = _as_float(dealer_snapshot.get("put_wall"), None)
    max_pain = _as_float(dealer_snapshot.get("max_pain"), None)

    near_threshold = max(float(spot or 0) * 0.0035, 0.75) if spot else 0.75
    inside_balance = bool(balance_low is not None and balance_high is not None and balance_low <= spot <= balance_high)
    inside_pin = bool(pin_low is not None and pin_high is not None and pin_low <= spot <= pin_high)
    near_call_wall = bool(call_wall is not None and abs(float(call_wall) - float(spot or 0)) <= near_threshold)
    near_put_wall = bool(put_wall is not None and abs(float(put_wall) - float(spot or 0)) <= near_threshold)
    near_wall = near_call_wall or near_put_wall
    near_max_pain = bool(max_pain is not None and abs(float(max_pain) - float(spot or 0)) <= near_threshold)

    reasons = []
    effective_label = raw_label
    entry_allowed = True
    requires_trigger = False
    posture_mult = 1.0
    confidence_cap = None

    pin_like_raw = any(tok in raw_label for tok in ("PIN", "SUPPRESSING"))
    trend_like_raw = any(tok in raw_label for tok in ("TREND", "EXPLOSIVE"))

    if inside_balance:
        reasons.append("inside local balance zone")
    if inside_pin:
        reasons.append("inside dealer pin zone")
    if near_max_pain:
        reasons.append("near max pain")
    if near_wall:
        reasons.append("near dealer wall")

    if inside_balance and (inside_pin or near_max_pain or near_wall):
        effective_label = "PIN / RANGE"
        entry_allowed = False
        requires_trigger = True
        posture_mult = 0.65 if mode == "scalp" else 0.75
        confidence_cap = 49 if (structure_score is not None and structure_score <= -4) else 52
    elif pin_like_raw:
        effective_label = "PIN / RANGE"
        requires_trigger = True
        posture_mult = 0.75 if mode == "scalp" else 0.85
        confidence_cap = 52
        if structure_score is not None and structure_score <= -4:
            entry_allowed = False
            confidence_cap = 49
    elif inside_balance and (structure_score is None or structure_score <= 0):
        effective_label = "RANGE"
        requires_trigger = True
        posture_mult = 0.80 if mode == "scalp" else 0.90
        confidence_cap = 54
    elif trend_like_raw:
        effective_label = raw_label
    elif raw_label == "UNKNOWN" and (inside_balance or inside_pin or near_max_pain):
        effective_label = "PIN / RANGE"
        requires_trigger = True
        posture_mult = 0.75
        confidence_cap = 52

    vol_label = str(vol_regime.get("label") or "").upper()
    if vol_label in ("ELEVATED", "CRISIS"):
        posture_mult *= 0.85 if mode in ("scalp", "swing") else 0.95
        if confidence_cap is None:
            confidence_cap = 60 if vol_label == "ELEVATED" else 55
        elif vol_label == "CRISIS":
            confidence_cap = min(confidence_cap, 50)

    if effective_label in ("PIN / RANGE", "RANGE"):
        description = "Spot is still trapped near balance / pin structure. Favor watchlist mode until a clean break confirms direction."
    elif trend_like_raw:
        description = dealer_snapshot.get("description") or "Dealer positioning still favors directional follow-through."
    else:
        description = dealer_snapshot.get("description") or "No strong trade-regime override."

    if reasons:
        description += " Context: " + ", ".join(dict.fromkeys(reasons)) + "."

    posture = dealer_snapshot.get("posture") or vol_regime.get("posture")
    if effective_label in ("PIN / RANGE", "RANGE"):
        posture = "Reduce size. Wait for a clean break beyond local structure before treating this as directional."

    return {
        "label": effective_label,
        "raw_label": raw_label,
        "description": description,
        "entry_allowed": entry_allowed,
        "requires_trigger": requires_trigger,
        "posture_mult": round(posture_mult, 2),
        "confidence_cap": confidence_cap,
        "reason_bits": reasons,
        "horizon_label": horizon,
        "inside_balance": inside_balance,
        "inside_pin": inside_pin,
        "near_max_pain": near_max_pain,
        "near_wall": near_wall,
        "structure_score": structure_score,
        "confluence": confluence,
        "outer_bracket_low": outer_low,
        "outer_bracket_high": outer_high,
        "posture": posture,
    }


def apply_effective_regime_gate_to_rec(
    rec: dict,
    shared_snapshot: Optional[dict],
    mode: str = "scalp",
    has_confirmed_trigger: bool = False,
) -> tuple[bool, str, dict]:
    rec = rec or {}
    snap = shared_snapshot or {}
    effective = snap.get("effective_regime") or {}
    if not effective:
        return True, "", rec

    rec["effective_regime"] = effective
    rec["dealer_horizon_label"] = effective.get("horizon_label")
    rec["effective_regime_label"] = effective.get("label")
    rec["effective_regime_requires_trigger"] = effective.get("requires_trigger")

    cap = effective.get("confidence_cap")
    current_conf = rec.get("confidence")
    if cap is not None and current_conf is not None:
        try:
            current_conf = int(current_conf)
            cap = int(cap)
            if current_conf > cap:
                rec.setdefault("confidence_pre_effective_regime", current_conf)
                rec["confidence"] = cap
        except Exception:
            pass

    posture_mult = _as_float(effective.get("posture_mult"), 1.0)
    if posture_mult > 0 and posture_mult < 1.0 and rec.get("contracts") is not None:
        try:
            orig_contracts = int(rec.get("contracts") or 0)
            adj = max(1, int(math.floor(orig_contracts * posture_mult)))
            if adj < orig_contracts:
                rec.setdefault("contracts_pre_effective_regime", orig_contracts)
                rec["contracts"] = adj
        except Exception:
            pass

    if effective.get("posture"):
        rec["posture"] = effective.get("posture")

    structure_score = effective.get("structure_score")
    if structure_score is None:
        structure_score = rec.get("structure_overlay_score")
    try:
        structure_score = int(structure_score) if structure_score is not None else None
    except Exception:
        structure_score = None

    conf_now = rec.get("confidence")
    try:
        conf_now = int(conf_now) if conf_now is not None else None
    except Exception:
        conf_now = None

    should_block = False
    block_reason = ""
    if mode in ("scalp", "swing"):
        if effective.get("requires_trigger") and not has_confirmed_trigger:
            if (structure_score is not None and structure_score <= -4) or (conf_now is not None and conf_now < 50):
                should_block = True
                block_reason = effective.get("description") or "Trigger required while price is pinned / range-bound."
        if not effective.get("entry_allowed", True) and not has_confirmed_trigger and not should_block:
            should_block = True
            block_reason = effective.get("description") or "Effective regime blocks fresh directional entry here."

    return (not should_block), block_reason, rec


def apply_vol_overlay_to_rec(rec: dict, vol_regime: dict, mode: str = "scalp") -> dict:
    rec = rec or {}
    if not vol_regime:
        return rec
    rec["canonical_vol_regime"] = vol_regime
    rec["posture"] = vol_regime.get("posture")
    base_conf = int(rec.get("confidence") or 0)
    penalty = 0
    if vol_regime.get("label") == "CRISIS":
        penalty = 10 if mode == "scalp" else 8
    elif vol_regime.get("label") == "ELEVATED":
        penalty = 6 if mode == "scalp" else 4
    elif vol_regime.get("label") == "TRANSITION":
        penalty = 4 if mode == "scalp" else 3
    if penalty:
        rec.setdefault("confidence_pre_vol_regime", base_conf)
        rec["confidence"] = max(0, base_conf - penalty)
        rec["vol_regime_penalty"] = penalty
    contracts = rec.get("contracts")
    try:
        if contracts is not None:
            adj = max(1, int(math.floor(float(contracts) * float(vol_regime.get("size_mult", 1.0)))))
            rec["contracts_pre_vol_regime"] = contracts
            rec["contracts"] = adj
    except Exception:
        pass
    note = format_canonical_vol_line(vol_regime)
    if note:
        rec["vol_regime_note"] = f"🌡️ Volatility overlay: {note}. Posture: {vol_regime.get('posture', '')}"
    return rec



def compute_price_structure_levels(rows: Optional[list], spot: float) -> dict:
    rows = rows or []
    out = {
        "pivot": None, "r1": None, "s1": None, "r2": None, "s2": None,
        "swing_high": None, "swing_low": None,
        "fib_support": None, "fib_resistance": None,
        "vp_support": None, "vp_resistance": None, "vpoc": None,
        "local_support_1": None, "local_resistance_1": None,
        "local_support_sources": None, "local_resistance_sources": None,
        "outer_support_1": None, "outer_resistance_1": None,
        "local_balance_zone_low": None, "local_balance_zone_high": None,
        "outer_bracket_low": None, "outer_bracket_high": None,
        "structure_confluence": 0,
    }
    if not rows or len(rows) < 8 or not spot:
        return out

    highs = [_as_float(r.get("high")) for r in rows]
    lows = [_as_float(r.get("low")) for r in rows]
    closes = [_as_float(r.get("close")) for r in rows]
    vols = [_as_float(r.get("volume")) for r in rows]

    prev = rows[-1]
    prev_high = _as_float(prev.get("high"))
    prev_low = _as_float(prev.get("low"))
    prev_close = _as_float(prev.get("close"))
    pivot = (prev_high + prev_low + prev_close) / 3.0
    r1 = 2 * pivot - prev_low
    s1 = 2 * pivot - prev_high
    rng = prev_high - prev_low
    r2 = pivot + rng
    s2 = pivot - rng
    out.update({"pivot": pivot, "r1": r1, "s1": s1, "r2": r2, "s2": s2})

    order = 3
    swing_highs = []
    swing_lows = []
    for i in range(order, len(rows) - order):
        h = highs[i]
        l = lows[i]
        if h >= max(highs[i - order:i + order + 1]):
            swing_highs.append(h)
        if l <= min(lows[i - order:i + order + 1]):
            swing_lows.append(l)
    out["swing_high"] = min([x for x in swing_highs if x > spot], default=None)
    out["swing_low"] = max([x for x in swing_lows if x < spot], default=None)

    lookback = min(len(rows), 34)
    hi = max(highs[-lookback:])
    lo = min(lows[-lookback:])
    if hi > lo:
        fib_ratios = [0.236, 0.382, 0.5, 0.618, 0.786]
        fib_levels = sorted({lo + (hi - lo) * r for r in fib_ratios})
        out["fib_support"] = max([lvl for lvl in fib_levels if lvl <= spot], default=None)
        out["fib_resistance"] = min([lvl for lvl in fib_levels if lvl >= spot], default=None)
        for ratio, key in [(0.382, "fib_382"), (0.5, "fib_500"), (0.618, "fib_618"), (0.786, "fib_786")]:
            out[key] = lo + (hi - lo) * ratio

    if vols and max(vols) > 0:
        pmin = min(lows)
        pmax = max(highs)
        if pmax > pmin:
            bin_count = 24
            step = (pmax - pmin) / bin_count
            if step > 0:
                profile = [0.0 for _ in range(bin_count)]
                mids = [pmin + (i + 0.5) * step for i in range(bin_count)]
                for c, v in zip(closes, vols):
                    idx = int(min(max((c - pmin) / step, 0), bin_count - 1))
                    profile[idx] += v
                if profile:
                    out["vpoc"] = mids[max(range(len(profile)), key=lambda i: profile[i])]
                    below = [(profile[i], mids[i]) for i in range(len(mids)) if mids[i] < spot]
                    above = [(profile[i], mids[i]) for i in range(len(mids)) if mids[i] > spot]
                    if below:
                        out["vp_support"] = max(below, key=lambda x: x[0])[1]
                    if above:
                        out["vp_resistance"] = max(above, key=lambda x: x[0])[1]

    supports = []
    resistances = []

    def add_level(kind: str, value):
        if value is None:
            return
        value = float(value)
        if value < spot:
            supports.append((value, kind))
        elif value > spot:
            resistances.append((value, kind))

    add_level("swing_low", out["swing_low"])
    add_level("s1", out["s1"])
    add_level("s2", out["s2"])
    add_level("fib", out["fib_support"])
    add_level("vp", out["vp_support"])
    add_level("pivot", out["pivot"])

    add_level("swing_high", out["swing_high"])
    add_level("r1", out["r1"])
    add_level("r2", out["r2"])
    add_level("fib", out["fib_resistance"])
    add_level("vp", out["vp_resistance"])
    add_level("pivot", out["pivot"])

    tol = max(spot * 0.0035, 0.75)
    if supports:
        supports.sort(key=lambda x: spot - x[0])
        primary = supports[0][0]
        srcs = [name for value, name in supports if abs(value - primary) <= tol]
        out["local_support_1"] = primary
        out["local_support_sources"] = " + ".join(sorted(set(srcs)))
        outer_supports = [value for value, _ in supports if value < (primary - tol)]
        if outer_supports:
            out["outer_support_1"] = max(outer_supports)
    if resistances:
        resistances.sort(key=lambda x: x[0] - spot)
        primary = resistances[0][0]
        srcs = [name for value, name in resistances if abs(value - primary) <= tol]
        out["local_resistance_1"] = primary
        out["local_resistance_sources"] = " + ".join(sorted(set(srcs)))
        outer_resistances = [value for value, _ in resistances if value > (primary + tol)]
        if outer_resistances:
            out["outer_resistance_1"] = min(outer_resistances)

    local_s = out.get("local_support_1")
    local_r = out.get("local_resistance_1")
    if local_s is not None and local_r is not None:
        out["local_balance_zone_low"] = local_s
        out["local_balance_zone_high"] = local_r
    if out.get("outer_support_1") is not None:
        out["outer_bracket_low"] = out.get("outer_support_1")
    elif local_s is not None:
        out["outer_bracket_low"] = local_s
    if out.get("outer_resistance_1") is not None:
        out["outer_bracket_high"] = out.get("outer_resistance_1")
    elif local_r is not None:
        out["outer_bracket_high"] = local_r

    out["structure_confluence"] = len([x for x in [out.get("local_support_sources"), out.get("local_resistance_sources")] if x])
    return out



def build_canonical_structure_context(ticker: str, spot: float, rows: Optional[list] = None) -> dict:
    ps = compute_price_structure_levels(rows or [], spot)
    return {
        "ticker": ticker,
        "spot": spot,
        "price_structure": ps,
        "local_balance_zone_low": ps.get("local_balance_zone_low"),
        "local_balance_zone_high": ps.get("local_balance_zone_high"),
        "outer_bracket_low": ps.get("outer_bracket_low"),
        "outer_bracket_high": ps.get("outer_bracket_high"),
    }



def score_structure_overlay(rec: dict, structure_ctx: Optional[dict], mode: str = "scalp") -> dict:
    ps = ((structure_ctx or {}).get("price_structure") or {})
    trade = (rec or {}).get("trade") or {}
    spot = float((rec or {}).get("spot") or (structure_ctx or {}).get("spot") or 0.0)
    direction = str((rec or {}).get("direction") or (rec or {}).get("bias") or "").lower()
    em_amt = float(((rec or {}).get("em_data") or {}).get("expected_move") or (((rec or {}).get("swing_em") or {}).get("em_1sd") or 0.0) or 0.0)
    local_r = ps.get("local_resistance_1")
    local_s = ps.get("local_support_1")
    pivot = ps.get("pivot")
    fib_sup = ps.get("fib_support")
    fib_res = ps.get("fib_resistance")
    vpoc = ps.get("vpoc")
    balance_low = ps.get("local_balance_zone_low")
    balance_high = ps.get("local_balance_zone_high")
    outer_low = ps.get("outer_bracket_low")
    outer_high = ps.get("outer_bracket_high")

    notes: List[str] = []
    delta = 0
    rejection_bucket = None
    opposing_level = None
    opposing_dist = None

    opp_near = max(spot * (0.007 if mode == "scalp" else 0.012), em_amt * (0.35 if mode == "scalp" else 0.50)) if spot else 0
    sup_near = max(spot * 0.006, em_amt * 0.30) if spot else 0
    near_balance = False
    if balance_low is not None and balance_high is not None and balance_low < spot < balance_high and em_amt > 0:
        near_balance = (balance_high - balance_low) <= (2.2 * em_amt if mode == "scalp" else 2.8 * em_amt)

    if direction == "bull":
        if local_r is not None and local_r > spot:
            opposing_level = local_r
            opposing_dist = local_r - spot
            if opposing_dist <= opp_near:
                delta -= 8 if mode == "scalp" else 10
                rejection_bucket = rejection_bucket or "structure_opposition_close"
                notes.append(f"Local resistance close ({_fmt_money(local_r)})")
            elif em_amt > 0 and opposing_dist <= em_amt * 0.9:
                delta -= 4
                notes.append(f"Resistance sits inside move path ({_fmt_money(local_r)})")
        if local_s is not None and spot > local_s and (spot - local_s) <= sup_near:
            delta += 3
            notes.append(f"Nearby structure support ({_fmt_money(local_s)})")
        if pivot is not None:
            if spot >= pivot:
                delta += 2
                notes.append("Holding above pivot")
            else:
                delta -= 2
                notes.append("Below pivot")
        if fib_sup is not None and spot >= fib_sup and (spot - fib_sup) <= sup_near:
            delta += 2
            notes.append(f"Near Fib support ({_fmt_money(fib_sup)})")
        if vpoc is not None:
            if vpoc < spot:
                delta += 1
                notes.append("Trading above acceptance")
            elif vpoc > spot:
                delta -= 1
                notes.append("Acceptance above price")
        if outer_high is not None and outer_high > spot and local_r is not None and outer_high > local_r:
            notes.append(f"Outer bracket above at {_fmt_money(outer_high)}")
    elif direction == "bear":
        if local_s is not None and local_s < spot:
            opposing_level = local_s
            opposing_dist = spot - local_s
            if opposing_dist <= opp_near:
                delta -= 8 if mode == "scalp" else 10
                rejection_bucket = rejection_bucket or "structure_opposition_close"
                notes.append(f"Local support close ({_fmt_money(local_s)})")
            elif em_amt > 0 and opposing_dist <= em_amt * 0.9:
                delta -= 4
                notes.append(f"Support sits inside move path ({_fmt_money(local_s)})")
        if local_r is not None and local_r > spot and (local_r - spot) <= sup_near:
            delta += 3
            notes.append(f"Nearby structure resistance ({_fmt_money(local_r)})")
        if pivot is not None:
            if spot <= pivot:
                delta += 2
                notes.append("Holding below pivot")
            else:
                delta -= 2
                notes.append("Above pivot")
        if fib_res is not None and spot <= fib_res and (fib_res - spot) <= sup_near:
            delta += 2
            notes.append(f"Near Fib resistance ({_fmt_money(fib_res)})")
        if vpoc is not None:
            if vpoc > spot:
                delta += 1
                notes.append("Trading below acceptance")
            elif vpoc < spot:
                delta -= 1
                notes.append("Acceptance below price")
        if outer_low is not None and outer_low < spot and local_s is not None and outer_low < local_s:
            notes.append(f"Outer bracket below at {_fmt_money(outer_low)}")

    if near_balance:
        delta -= 4 if mode == "scalp" else 5
        rejection_bucket = rejection_bucket or "pin_risk"
        notes.append("Tight local balance zone / pin risk")

    return {
        "delta": int(delta),
        "notes": notes,
        "local_support": local_s,
        "local_resistance": local_r,
        "pivot": pivot,
        "vpoc": vpoc,
        "balance_zone_low": balance_low,
        "balance_zone_high": balance_high,
        "outer_bracket_low": outer_low,
        "outer_bracket_high": outer_high,
        "structure_confluence": ps.get("structure_confluence"),
        "rejection_bucket": rejection_bucket,
        "opposing_level": opposing_level,
        "opposing_distance": round(opposing_dist, 2) if opposing_dist is not None else None,
    }



def apply_structure_overlay_to_rec(rec: dict, structure_ctx: Optional[dict], mode: str = "scalp") -> dict:
    rec = rec or {}
    ps = ((structure_ctx or {}).get("price_structure") or {})
    if not ps:
        return rec
    overlay = score_structure_overlay(rec, structure_ctx, mode=mode)
    delta = overlay.get("delta", 0)
    notes = overlay.get("notes") or []

    conf_base = rec.get("confidence")
    if conf_base is not None:
        base = int(conf_base or 0)
        rec.setdefault("confidence_pre_structure", base)
        rec["confidence"] = max(0, min(100, base + int(delta)))
    contracts = rec.get("contracts")
    if contracts is not None and delta <= -8:
        try:
            rec["contracts_pre_structure"] = contracts
            rec["contracts"] = max(1, int(math.floor(float(contracts) * 0.85)))
        except Exception:
            pass
    if notes:
        existing = list(rec.get("conf_reasons") or [])
        rec["conf_reasons"] = existing + notes[:3]
        rec["structure_note"] = "🧱 Structure: " + " | ".join(notes[:3])
    rec["structure_overlay_score"] = int(delta)
    rec["structure_local_support"] = overlay.get("local_support")
    rec["structure_local_resistance"] = overlay.get("local_resistance")
    rec["structure_confluence"] = overlay.get("structure_confluence")
    rec["structure_balance_zone_low"] = overlay.get("balance_zone_low")
    rec["structure_balance_zone_high"] = overlay.get("balance_zone_high")
    rec["structure_outer_bracket_low"] = overlay.get("outer_bracket_low")
    rec["structure_outer_bracket_high"] = overlay.get("outer_bracket_high")
    rec["structure_rejection_bucket"] = overlay.get("rejection_bucket")
    rec["structure_opposing_level"] = overlay.get("opposing_level")
    rec["structure_opposing_distance"] = overlay.get("opposing_distance")
    return rec



def classify_rejection_bucket(reason: str) -> str:
    r = (reason or "").lower()
    if not r:
        return "unknown"
    if "pin risk" in r or "balance zone" in r:
        return "pin_risk"
    if "support close" in r or "resistance close" in r or "move path" in r:
        return "structure_opposition_close"
    if "confidence" in r and "below" in r:
        return "below_threshold"
    if "win prob" in r and "below" in r:
        return "win_prob_failure"
    if "slippage" in r or "negative ev" in r or "fair value" in r:
        return "pricing_ev_failure"
    if "no valid spreads" in r:
        return "no_valid_spreads"
    if "no expirations" in r or "not enough" in r or "no options chain" in r:
        return "data_or_chain_failure"
    return "other"



def build_manual_swing_signal_context(ticker: str, spot: float, rows: list, direction: str, structure_ctx: Optional[dict] = None) -> dict:
    rows = rows or []
    closes = [r.get("close") for r in rows if r.get("close") is not None]
    vols = [_as_float(r.get("volume")) for r in rows]

    daily_fast = _ema(closes[-34:], 8) if len(closes) >= 8 else None
    daily_slow = _ema(closes[-55:], 21) if len(closes) >= 21 else None
    daily_prev_fast = _ema(closes[-35:-1], 8) if len(closes) >= 9 else daily_fast
    daily_prev_slow = _ema(closes[-56:-1], 21) if len(closes) >= 22 else daily_slow
    daily_bull = bool(daily_fast is not None and daily_slow is not None and daily_fast > daily_slow)
    daily_gap = abs((daily_fast or 0) - (daily_slow or 0))
    daily_prev_gap = abs((daily_prev_fast or 0) - (daily_prev_slow or 0))
    htf_confirmed = daily_gap >= (daily_prev_gap * 0.98) if daily_fast is not None and daily_slow is not None else False
    htf_converging = daily_gap < daily_prev_gap if daily_fast is not None and daily_slow is not None else False

    weekly_closes = [closes[i] for i in range(4, len(closes), 5)] if len(closes) >= 10 else closes[::5]
    weekly_fast = _ema(weekly_closes[-20:], 5) if len(weekly_closes) >= 5 else None
    weekly_slow = _ema(weekly_closes[-40:], 20) if len(weekly_closes) >= 20 else None
    weekly_bull = bool(weekly_fast is not None and weekly_slow is not None and weekly_fast > weekly_slow)
    weekly_bear = bool(weekly_fast is not None and weekly_slow is not None and weekly_fast < weekly_slow)

    vol_contracting = False
    if len(vols) >= 20:
        recent = sum(vols[-5:]) / max(1, len(vols[-5:]))
        base = sum(vols[-20:]) / 20.0
        vol_contracting = recent < (base * 0.96)

    rsi_val = _rsi(closes, 14)
    mfi_val = _mfi(rows, 14)
    rsi_mfi_bull = ((_as_float(rsi_val, 50.0) + _as_float(mfi_val, 50.0)) / 2.0) >= 50.0

    ps = (structure_ctx or {}).get("price_structure") or {}
    fib_level = "61.8"
    fib_distance_pct = 2.0
    fib_map = []
    for lbl, key in [("38.2", "fib_382"), ("50.0", "fib_500"), ("61.8", "fib_618"), ("78.6", "fib_786")]:
        val = ps.get(key)
        if val:
            fib_map.append((lbl, float(val)))
    if fib_map:
        lbl, val = min(fib_map, key=lambda x: abs(x[1] - spot))
        fib_level = lbl
        fib_distance_pct = abs(val - spot) / max(spot, 0.01) * 100.0

    structure_seed = {
        "spot": spot,
        "direction": direction,
        "trade": {},
        "em_data": {"expected_move": max(spot * 0.015, 1.0)},
        "swing_em": {"em_1sd": max(spot * 0.04, 1.5)},
        "confidence": 50,
        "conf_reasons": [],
    }
    overlay = score_structure_overlay(structure_seed, structure_ctx, mode="swing")
    structure_bias_score = int(max(-12, min(12, overlay.get("delta", 0))))
    structure_reasons = list(overlay.get("notes") or [])[:3]

    trend_align = (direction == "bull" and daily_bull) or (direction == "bear" and not daily_bull)
    weekly_align = (direction == "bull" and weekly_bull) or (direction == "bear" and weekly_bear)
    quality = 0
    if fib_distance_pct <= 0.8:
        quality += 2
    elif fib_distance_pct <= 1.5:
        quality += 1
    if trend_align:
        quality += 1
    if weekly_align:
        quality += 1
    if structure_bias_score >= 4:
        quality += 1
    if vol_contracting:
        quality += 1

    tier = "1" if quality >= 4 else "2"
    scoreable = bool(len(closes) >= 25 and ps and spot > 0)

    return {
        "type": "swing",
        "source": "check",
        "manual_mode": True,
        "manual_scoreable": scoreable,
        "bias": direction,
        "tier": tier,
        "fib_level": fib_level,
        "fib_distance_pct": round(fib_distance_pct, 3),
        "weekly_bull": weekly_bull,
        "weekly_bear": weekly_bear,
        "htf_confirmed": bool(htf_confirmed),
        "htf_converging": bool(htf_converging),
        "daily_bull": daily_bull,
        "rsi_mfi_bull": bool(rsi_mfi_bull),
        "vol_contracting": bool(vol_contracting),
        "structure_bias_score": structure_bias_score,
        "structure_reasons": structure_reasons,
        "manual_quality": int(quality),
        "pivot_state": "above" if ps.get("pivot") is not None and spot >= ps.get("pivot") else "below" if ps.get("pivot") is not None else "unknown",
        "local_support": ps.get("local_support_1"),
        "local_resistance": ps.get("local_resistance_1"),
        "local_balance_zone_low": ps.get("local_balance_zone_low"),
        "local_balance_zone_high": ps.get("local_balance_zone_high"),
    }



def build_shared_model_snapshot(
    ticker: str,
    spot: float,
    dealer_regime: Optional[dict] = None,
    vol_regime: Optional[dict] = None,
    structure_ctx: Optional[dict] = None,
    rec: Optional[dict] = None,
    eng: Optional[dict] = None,
    cagf: Optional[dict] = None,
    v4_flow: Optional[dict] = None,
    walls: Optional[dict] = None,
    mode: str = "scalp",
    horizon_label: Optional[str] = None,
) -> dict:
    rec = rec or {}
    ps = ((structure_ctx or {}).get("price_structure") or {})
    structure = {
        "overlay_score": rec.get("structure_overlay_score"),
        "local_support": rec.get("structure_local_support") if rec.get("structure_local_support") is not None else ps.get("local_support_1"),
        "local_resistance": rec.get("structure_local_resistance") if rec.get("structure_local_resistance") is not None else ps.get("local_resistance_1"),
        "balance_zone_low": rec.get("structure_balance_zone_low") if rec.get("structure_balance_zone_low") is not None else ps.get("local_balance_zone_low"),
        "balance_zone_high": rec.get("structure_balance_zone_high") if rec.get("structure_balance_zone_high") is not None else ps.get("local_balance_zone_high"),
        "outer_bracket_low": rec.get("structure_outer_bracket_low") if rec.get("structure_outer_bracket_low") is not None else ps.get("outer_bracket_low"),
        "outer_bracket_high": rec.get("structure_outer_bracket_high") if rec.get("structure_outer_bracket_high") is not None else ps.get("outer_bracket_high"),
        "confluence": rec.get("structure_confluence") if rec.get("structure_confluence") is not None else ps.get("structure_confluence"),
        "rejection_bucket": rec.get("structure_rejection_bucket"),
    }
    dealer_snapshot = build_canonical_dealer_snapshot(
        ticker=ticker,
        spot=spot,
        eng=eng,
        cagf=cagf,
        v4_flow=v4_flow,
        walls=walls,
        dealer_regime=dealer_regime,
    )
    effective_regime = resolve_effective_trade_regime(
        dealer_snapshot=dealer_snapshot,
        structure=structure,
        vol_regime=vol_regime or {},
        mode=mode,
        spot=spot,
        horizon_label=horizon_label,
    )
    posture = (rec or {}).get("posture") or effective_regime.get("posture") or (vol_regime or {}).get("posture")
    return {
        "ticker": ticker,
        "spot": spot,
        "mode": mode,
        "horizon_label": effective_regime.get("horizon_label") or _dealer_horizon_label(mode, horizon_label),
        "dealer_regime": dealer_snapshot,
        "effective_regime": effective_regime,
        "vol_regime": vol_regime or {},
        "posture": posture,
        "structure": structure,
    }



def format_shared_snapshot_lines(shared_snapshot: Optional[dict]) -> List[str]:
    snap = shared_snapshot or {}
    lines: List[str] = []
    dealer = snap.get("dealer_regime") or {}
    effective = snap.get("effective_regime") or {}
    horizon = effective.get("horizon_label") or snap.get("horizon_label") or "front expiry"
    if effective:
        label = str(effective.get("label") or dealer.get("label") or dealer.get("regime") or "UNKNOWN").upper()
        desc = effective.get("description") or dealer.get("description") or dealer.get("source") or ""
        line = f"⚙️ Dealer Regime ({horizon}): {label}"
        if desc:
            line += f" — {desc}"
        lines.append(line)
        raw_label = str(effective.get("raw_label") or dealer.get("label") or dealer.get("regime") or "UNKNOWN").upper()
        if raw_label and raw_label != label:
            raw_source = dealer.get("source") or "unknown"
            lines.append(f"🧭 Raw Dealer Flow: {raw_label} ({raw_source})")
        if effective.get("requires_trigger"):
            lines.append("🚦 Entry filter: trigger required beyond local structure before treating this as directional.")
    elif dealer:
        label = str(dealer.get("label") or dealer.get("regime") or "UNKNOWN").upper()
        desc = dealer.get("description") or dealer.get("source") or ""
        line = f"⚙️ Dealer Regime ({horizon}): {label}"
        if desc:
            line += f" — {desc}"
        lines.append(line)

    if dealer:
        dealer_bits: List[str] = []
        if dealer.get("spot_vs_flip") in ("above", "below"):
            dealer_bits.append(f"spot {dealer.get('spot_vs_flip')} flip")
        if dealer.get("flip_price") is not None:
            dealer_bits.append(f"flip {_fmt_money(dealer.get('flip_price'))}")
        if dealer.get("max_pain") is not None:
            dealer_bits.append(f"max pain {_fmt_money(dealer.get('max_pain'))}")
        if dealer.get("put_wall") is not None or dealer.get("call_wall") is not None:
            pw = _fmt_money(dealer.get("put_wall")) if dealer.get("put_wall") is not None else "—"
            cw = _fmt_money(dealer.get("call_wall")) if dealer.get("call_wall") is not None else "—"
            dealer_bits.append(f"walls {pw}/{cw}")
        if dealer_bits:
            lines.append("🏦 Dealer Structure: " + " | ".join(dealer_bits))
    vol = snap.get("vol_regime") or {}
    if vol:
        lines.append(f"🌡️ Volatility Regime: {format_canonical_vol_line(vol)}")
    posture = snap.get("posture")
    if posture:
        lines.append(f"🪖 Posture: {posture}")
    structure = snap.get("structure") or {}
    if structure:
        bits = []
        overlay = structure.get("overlay_score")
        if overlay is not None:
            bits.append(f"score {overlay:+d}")
        ls = structure.get("local_support")
        lr = structure.get("local_resistance")
        if ls is not None:
            bits.append(f"S {_fmt_money(ls)}")
        if lr is not None:
            bits.append(f"R {_fmt_money(lr)}")
        bz_low = structure.get("balance_zone_low")
        bz_high = structure.get("balance_zone_high")
        if bz_low is not None and bz_high is not None:
            bits.append(f"balance {_fmt_money(bz_low)}–{_fmt_money(bz_high)}")
        ob_low = structure.get("outer_bracket_low")
        ob_high = structure.get("outer_bracket_high")
        if ob_low is not None and ob_high is not None:
            bits.append(f"outer {_fmt_money(ob_low)}–{_fmt_money(ob_high)}")
        confluence = structure.get("confluence")
        if confluence is not None:
            bits.append(f"confluence {confluence}")
        if bits:
            lines.append("🧱 Structure: " + " | ".join(bits))
    return lines
