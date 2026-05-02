# options_map.py
# ═══════════════════════════════════════════════════════════════════
# Phase 3.0-A — EM / Intraday Options Map sidecar
#
# Display-only presentation layer. It consumes existing EM/dealer/flow data
# and formats a compact map for diagnostic/intraday review. It does NOT
# change V1/V2 cards, momentum/burst cards, scorer decisions, entries, exits,
# managed-trade registration, or backtests.
# ═══════════════════════════════════════════════════════════════════

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


def _as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        f = float(value)
        return f if math.isfinite(f) else default
    except Exception:
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def _money(value: Any, digits: int = 2) -> str:
    f = _as_float(value)
    if f is None:
        return "?"
    if abs(f) >= 1000:
        return f"${f:,.{digits}f}"
    return f"${f:.{digits}f}"


def _strike(value: Any) -> str:
    f = _as_float(value)
    if f is None:
        return "?"
    if abs(f - round(f)) < 0.005:
        return f"${f:.0f}"
    if abs(f * 2 - round(f * 2)) < 0.005:
        return f"${f:.1f}"
    return f"${f:.2f}"


def _compact_int(value: Any) -> str:
    n = _as_int(value, 0)
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if abs(n) >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _pct(value: Any, digits: int = 1) -> str:
    f = _as_float(value)
    if f is None:
        return "?"
    return f"{f:+.{digits}f}%"


def _side_label(side: str) -> str:
    s = str(side or "").lower()
    return "CALL" if s.startswith("c") else "PUT" if s.startswith("p") else s.upper()


def calc_calendar_dte(expiry: str, as_of: Optional[date] = None) -> Optional[int]:
    try:
        exp = date.fromisoformat(str(expiry)[:10])
        base = as_of or date.today()
        return max((exp - base).days, 0)
    except Exception:
        return None


def _contract_key(row: dict, expiry: str) -> Tuple[str, float, str, str]:
    return (
        str(row.get("ticker") or "").upper(),
        float(_as_float(row.get("strike"), 0.0) or 0.0),
        str(row.get("side") or row.get("right") or "").lower(),
        str(expiry or "")[:10],
    )


def _confirmation_tag(conf: Optional[dict]) -> str:
    if not isinstance(conf, dict) or not conf:
        return ""
    tags: List[str] = []
    tag = str(conf.get("tag") or conf.get("flow_type") or "").replace("confirmed_", "").strip()
    if tag:
        tags.append(tag)
    if conf.get("divergence"):
        tags.append("divergence")
    if conf.get("stalk_type"):
        st = str(conf.get("stalk_type") or "").replace("_", " ").lower()
        tags.append(st)
    if conf.get("roll"):
        role = str(conf.get("roll_role") or "roll").replace("_", " ")
        tags.append(role)
    # Preserve order while de-duping.
    clean: List[str] = []
    seen = set()
    for t in tags:
        t = str(t or "").strip()
        if t and t not in seen:
            clean.append(t)
            seen.add(t)
    return f" [{', '.join(clean)}]" if clean else ""


def _score_row_for_ladder(row: dict, spot: float, side: str) -> float:
    strike_val = _as_float(row.get("strike"), 0.0) or 0.0
    oi = _as_int(row.get("openInterest") or row.get("oi"), 0)
    vol = _as_int(row.get("volume") or row.get("vol"), 0)
    dist_pct = abs(strike_val - spot) / spot if spot else 1.0
    if dist_pct > 0.15:
        distance_weight = 0.20
    else:
        distance_weight = max(0.25, 1.0 - (dist_pct * 5.0))
    # OI anchors matter most; volume helps identify fresh attention.
    return (oi * 1.0 + vol * 1.35) * distance_weight


def build_contract_ladders(
    rows: Iterable[dict],
    *,
    ticker: str,
    spot: float,
    expiry: str,
    dte: Optional[int],
    lookup_fn: Optional[Callable[[str, float, str, str], Optional[dict]]] = None,
    top_n: int = 3,
) -> dict:
    """Return top resistance/support option rows with exact expiry/DTE.

    The ladder is display-only. It intentionally requires a real expiry so the
    map never shows aggregate/unactionable contracts.
    """
    if not expiry or dte is None:
        return {"resistance": [], "support": [], "skipped_missing_expiry": 0}

    calls: List[dict] = []
    puts: List[dict] = []
    skipped = 0
    for raw in rows or []:
        row = dict(raw or {})
        side = str(row.get("side") or row.get("right") or "").lower()
        strike_val = _as_float(row.get("strike"))
        if strike_val is None or side not in ("call", "put"):
            skipped += 1
            continue
        row["ticker"] = ticker.upper()
        row["expiry"] = str(expiry)[:10]
        row["dte"] = dte
        row["score"] = _score_row_for_ladder(row, spot, side)
        if lookup_fn:
            try:
                row["confirmation"] = lookup_fn(ticker.upper(), strike_val, side, str(expiry)[:10])
            except Exception:
                row["confirmation"] = None
        if side == "call" and strike_val >= spot * 0.995:
            calls.append(row)
        elif side == "put" and strike_val <= spot * 1.005:
            puts.append(row)

    # Prefer the highest relevance score, then closest-to-spot.
    calls.sort(key=lambda r: (-_as_float(r.get("score"), 0.0), abs((_as_float(r.get("strike"), spot) or spot) - spot)))
    puts.sort(key=lambda r: (-_as_float(r.get("score"), 0.0), abs((_as_float(r.get("strike"), spot) or spot) - spot)))
    return {
        "resistance": calls[:max(1, top_n)],
        "support": puts[:max(1, top_n)],
        "skipped_missing_expiry": skipped,
    }


def _contract_line(row: dict) -> str:
    side = _side_label(row.get("side") or row.get("right"))
    exp = str(row.get("expiry") or "")[:10]
    dte = row.get("dte")
    oi = row.get("openInterest", row.get("oi", 0))
    vol = row.get("volume", row.get("vol", 0))
    strike = _strike(row.get("strike"))
    tag = _confirmation_tag(row.get("confirmation"))
    return f"{strike} {side} — Exp {exp}, {dte}DTE — OI {_compact_int(oi)} / Vol {_compact_int(vol)}{tag}"


def _level_candidates(direction: str, spot: float, em: dict, structure: dict) -> List[float]:
    keys_up = (
        "local_resistance_1", "vp_resistance", "fib_resistance", "r1", "r2",
        "call_wall", "gamma_wall", "gamma_flip", "max_pain", "pin_zone_high",
    )
    keys_down = (
        "local_support_1", "vp_support", "fib_support", "s1", "s2",
        "put_wall", "gamma_wall", "gamma_flip", "max_pain", "pin_zone_low",
    )
    values: List[float] = []
    if direction == "up":
        values += [_as_float(em.get("bull_1sd")), _as_float(em.get("bull_2sd"))]
        values += [_as_float(structure.get(k)) for k in keys_up]
        return sorted({round(v, 2) for v in values if v is not None and v > spot})
    values += [_as_float(em.get("bear_1sd")), _as_float(em.get("bear_2sd"))]
    values += [_as_float(structure.get(k)) for k in keys_down]
    return sorted({round(v, 2) for v in values if v is not None and v < spot}, reverse=True)


def build_target_ladder(spot: float, em: dict, structure: dict, bias_score: int = 0) -> dict:
    """Translate existing EM/dealer levels into conservative/primary/aggressive context."""
    up = _level_candidates("up", spot, em or {}, structure or {})
    down = _level_candidates("down", spot, em or {}, structure or {})
    direction = "bullish" if bias_score >= 2 else "bearish" if bias_score <= -2 else "neutral"

    def _pick(vals: List[float], fallback: Any = None) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        if not vals:
            f = _as_float(fallback)
            return f, f, f
        if len(vals) == 1:
            return vals[0], vals[0], vals[0]
        if len(vals) == 2:
            return vals[0], vals[1], vals[1]
        return vals[0], vals[min(1, len(vals)-1)], vals[-1]

    if direction == "bullish":
        con, pri, agg = _pick(up, (em or {}).get("bull_1sd"))
        return {"direction": "bullish", "conservative": con, "primary": pri, "aggressive": agg}
    if direction == "bearish":
        con, pri, agg = _pick(down, (em or {}).get("bear_1sd"))
        return {"direction": "bearish", "conservative": con, "primary": pri, "aggressive": agg}
    # Neutral maps should show range boundaries rather than forced targets.
    return {
        "direction": "neutral",
        "support": down[0] if down else _as_float((em or {}).get("bear_1sd")),
        "resistance": up[0] if up else _as_float((em or {}).get("bull_1sd")),
        "range_low": _as_float((em or {}).get("bear_1sd")),
        "range_high": _as_float((em or {}).get("bull_1sd")),
    }


def _dealer_regime_line(eng: dict, structure: dict, spot: float) -> str:
    eng = eng or {}
    structure = structure or {}
    gex = _as_float(eng.get("gex"), 0.0) or 0.0
    flip = _as_float(structure.get("gamma_flip") or eng.get("flip_price"))
    if gex < 0:
        base = "Negative GEX / trend can expand"
    elif gex > 0:
        base = "Positive GEX / moves can compress"
    else:
        base = "GEX neutral / no strong dealer read"
    if flip is not None:
        rel = "above flip" if spot > flip else "below flip" if spot < flip else "at flip"
        base += f" ({rel})"
    return base


def _plain_read(spot: float, structure: dict, targets: dict, bias_score: int) -> List[str]:
    structure = structure or {}
    flip = _as_float(structure.get("gamma_flip"))
    call_wall = _as_float(structure.get("call_wall"))
    put_wall = _as_float(structure.get("put_wall"))
    lines: List[str] = []

    if flip is not None:
        if spot >= flip:
            lines.append(f"Above gamma flip {_money(flip)} keeps upside path cleaner; losing it weakens the read.")
        else:
            lines.append(f"Below gamma flip {_money(flip)} means rallies can stall until reclaimed.")
    if bias_score >= 2:
        if targets.get("primary"):
            lines.append(f"Bullish path points first toward primary target {_money(targets.get('primary'))}.")
        if call_wall and call_wall > spot:
            lines.append(f"Call wall/resistance starts near {_money(call_wall)}.")
        if put_wall and put_wall < spot:
            lines.append(f"Failure below put support {_money(put_wall)} changes the intraday read.")
    elif bias_score <= -2:
        if targets.get("primary"):
            lines.append(f"Bearish path points first toward primary target {_money(targets.get('primary'))}.")
        if put_wall and put_wall < spot:
            lines.append(f"Put wall/support sits near {_money(put_wall)}.")
        if call_wall and call_wall > spot:
            lines.append(f"Reclaiming toward call wall {_money(call_wall)} weakens the bearish read.")
    else:
        support = targets.get("support") or put_wall
        resistance = targets.get("resistance") or call_wall
        if support and resistance:
            lines.append(f"Neutral map: trade location matters between support {_money(support)} and resistance {_money(resistance)}.")
        elif support:
            lines.append(f"Neutral map with nearby support around {_money(support)}.")
        elif resistance:
            lines.append(f"Neutral map with nearby resistance around {_money(resistance)}.")

    return lines[:3]



def _uniq_levels(levels: Iterable[Tuple[Any, str]], spot: float) -> List[Tuple[float, str]]:
    out: List[Tuple[float, str]] = []
    seen = set()
    for value, label in levels or []:
        f = _as_float(value)
        if f is None:
            continue
        key = round(f, 2)
        if key in seen:
            # Merge labels if the same level was already added.
            for i, (old_v, old_label) in enumerate(out):
                if round(old_v, 2) == key and label and label not in old_label:
                    out[i] = (old_v, f"{old_label} / {label}")
                    break
            continue
        seen.add(key)
        out.append((key, label))
    return out


def build_watch_levels(spot: float, em: dict, structure: dict, targets: dict, bias_score: int = 0) -> dict:
    """Build simple above/below watch levels from existing EM/dealer levels.

    This is intentionally plain. It does not select trades; it only explains
    what levels matter next.
    """
    em = em or {}
    structure = structure or {}
    targets = targets or {}
    direction = targets.get("direction") or ("bullish" if bias_score >= 2 else "bearish" if bias_score <= -2 else "neutral")

    flip = _as_float(structure.get("gamma_flip"))
    call_wall = _as_float(structure.get("call_wall"))
    put_wall = _as_float(structure.get("put_wall"))
    max_pain = _as_float(structure.get("max_pain"))
    em_high = _as_float(em.get("bull_1sd"))
    em_low = _as_float(em.get("bear_1sd"))

    above_candidates: List[Tuple[Any, str]] = []
    below_candidates: List[Tuple[Any, str]] = []

    if direction == "bullish":
        above_candidates += [
            (targets.get("conservative"), "next target"),
            (targets.get("primary"), "primary"),
            (targets.get("aggressive"), "stretch"),
            (call_wall, "call wall"),
            (em_high, "EM high"),
        ]
        below_candidates += [
            (flip, "key hold / flip"),
            (put_wall, "put wall"),
            (em_low, "EM low"),
            (max_pain, "pin"),
        ]
    elif direction == "bearish":
        below_candidates += [
            (targets.get("conservative"), "next target"),
            (targets.get("primary"), "primary"),
            (targets.get("aggressive"), "stretch"),
            (put_wall, "put wall"),
            (em_low, "EM low"),
        ]
        above_candidates += [
            (flip, "reclaim / flip"),
            (call_wall, "call wall"),
            (em_high, "EM high"),
            (max_pain, "pin"),
        ]
    else:
        above_candidates += [
            (targets.get("resistance"), "range high"),
            (call_wall, "call wall"),
            (em_high, "EM high"),
            (flip if flip and flip > spot else None, "flip"),
        ]
        below_candidates += [
            (targets.get("support"), "range low"),
            (put_wall, "put wall"),
            (em_low, "EM low"),
            (flip if flip and flip < spot else None, "flip"),
        ]

    above = [(v, l) for v, l in _uniq_levels(above_candidates, spot) if v > spot]
    below = [(v, l) for v, l in _uniq_levels(below_candidates, spot) if v < spot]
    above.sort(key=lambda x: abs(x[0] - spot))
    below.sort(key=lambda x: abs(x[0] - spot))
    return {"above": above[:4], "below": below[:4]}


def build_watch_triggers(spot: float, structure: dict, targets: dict, watch_levels: dict, bias_score: int = 0) -> List[str]:
    """Return simple watch-trigger lines. Context only."""
    structure = structure or {}
    targets = targets or {}
    above = list((watch_levels or {}).get("above") or [])
    below = list((watch_levels or {}).get("below") or [])
    direction = targets.get("direction") or ("bullish" if bias_score >= 2 else "bearish" if bias_score <= -2 else "neutral")

    lines: List[str] = []
    if direction == "bullish":
        hold = below[0][0] if below else _as_float(structure.get("gamma_flip") or structure.get("put_wall"))
        trigger = above[0][0] if above else _as_float(targets.get("conservative"))
        target = above[1][0] if len(above) > 1 else _as_float(targets.get("primary") or targets.get("aggressive"))
        chase = above[-1][0] if above else _as_float(targets.get("aggressive") or structure.get("call_wall"))
        if hold:
            lines.append(f"Hold above {_money(hold)} → upside path stays open")
        if trigger and target and abs(trigger - target) > 0.01:
            lines.append(f"Reclaim {_money(trigger)} → watch {_money(target)}")
        elif trigger:
            lines.append(f"Reclaim {_money(trigger)} → confirms strength")
        if hold:
            lines.append(f"Lose {_money(hold)} → map weakens")
        if chase:
            lines.append(f"Do not chase into {_money(chase)} without momentum")
    elif direction == "bearish":
        hold = above[0][0] if above else _as_float(structure.get("gamma_flip") or structure.get("call_wall"))
        trigger = below[0][0] if below else _as_float(targets.get("conservative"))
        target = below[1][0] if len(below) > 1 else _as_float(targets.get("primary") or targets.get("aggressive"))
        chase = below[-1][0] if below else _as_float(targets.get("aggressive") or structure.get("put_wall"))
        if hold:
            lines.append(f"Stay below {_money(hold)} → downside path stays open")
        if trigger and target and abs(trigger - target) > 0.01:
            lines.append(f"Lose {_money(trigger)} → watch {_money(target)}")
        elif trigger:
            lines.append(f"Lose {_money(trigger)} → confirms weakness")
        if hold:
            lines.append(f"Reclaim {_money(hold)} → bearish map weakens")
        if chase:
            lines.append(f"Do not chase into {_money(chase)} without momentum")
    else:
        low = below[0][0] if below else targets.get("support")
        high = above[0][0] if above else targets.get("resistance")
        if high:
            lines.append(f"Above {_money(high)} → range can open higher")
        if low:
            lines.append(f"Below {_money(low)} → range can open lower")
        if low and high:
            lines.append(f"Between {_money(low)} and {_money(high)} → location matters more than bias")
    # Preserve order while de-duping.
    clean: List[str] = []
    seen = set()
    for line in lines:
        if line and line not in seen:
            clean.append(line)
            seen.add(line)
    return clean[:4]

def format_options_map_card(context: dict) -> str:
    """Format the user-facing Watch Map card.

    The name is intentionally simple: this is a watch card, not a signal card
    and not a trade recommendation.
    """
    ticker = str(context.get("ticker") or "?").upper()
    spot = _as_float(context.get("spot"), 0.0) or 0.0
    expiry = str(context.get("expiry") or "")[:10]
    dte = context.get("dte")
    em = context.get("em") or {}
    structure = context.get("structure") or {}
    eng = context.get("eng") or {}
    bias = context.get("bias") or {}
    bias_score = _as_int(bias.get("score"), 0)
    bias_direction = str(bias.get("direction") or ("BULLISH" if bias_score >= 2 else "BEARISH" if bias_score <= -2 else "NEUTRAL")).upper()
    generated_at = context.get("generated_at") or datetime.now().strftime("%Y-%m-%d %H:%M")
    compact = bool(context.get("compact"))
    show_triggers = context.get("show_triggers", True) is not False

    ladders = context.get("ladders") or {}
    targets = build_target_ladder(spot, em, structure, bias_score)
    watch_levels = build_watch_levels(spot, em, structure, targets, bias_score)
    triggers = build_watch_triggers(spot, structure, targets, watch_levels, bias_score) if show_triggers else []

    em_low = _as_float(em.get("bear_1sd"))
    em_high = _as_float(em.get("bull_1sd"))
    flip = _as_float(structure.get("gamma_flip") or eng.get("flip_price"))

    if bias_score >= 2:
        bias_line = f"Bullish while above {_money(flip)}" if flip is not None and spot >= flip else "Bullish, but needs key levels to hold"
    elif bias_score <= -2:
        bias_line = f"Bearish while below {_money(flip)}" if flip is not None and spot <= flip else "Bearish, but needs key levels to reject"
    else:
        bias_line = "Neutral / range watch"

    lines = [
        f"🧭 {ticker} WATCH MAP",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Spot: {_money(spot)}",
    ]
    if em_low is not None and em_high is not None:
        lines.append(f"Range: {_money(em_low)} – {_money(em_high)}")
    lines.append(f"Bias: {bias_line}")
    if not compact:
        lines.append(f"Exp: {expiry or '?'} ({dte if dte is not None else '?'}DTE) | Generated: {generated_at}")
        lines.append(f"Regime: {_dealer_regime_line(eng, structure, spot)}")

    above = watch_levels.get("above") or []
    below = watch_levels.get("below") or []
    lines += ["", "Above:"]
    if above:
        for level, label in above[:3 if compact else 4]:
            lines.append(f"{_money(level)} — {label}")
    else:
        lines.append("No clean upside level above spot.")

    lines += ["", "Below:"]
    if below:
        for level, label in below[:3 if compact else 4]:
            lines.append(f"{_money(level)} — {label}")
    else:
        lines.append("No clean downside level below spot.")

    resistance = ladders.get("resistance") or []
    support = ladders.get("support") or []
    flow_rows = []
    if resistance:
        flow_rows.extend(resistance[:1 if compact else 2])
    if support:
        flow_rows.extend(support[:1 if compact else 2])
    lines += ["", "Flow/OI:"]
    if flow_rows:
        for row in flow_rows:
            lines.append("• " + _contract_line(row))
    else:
        lines.append("• No clean Flow/OI contracts found with expiry/DTE.")

    if not compact:
        lines += ["", "Targets:"]
        if targets.get("direction") == "neutral":
            lines.append(f"Support: {_money(targets.get('support'))} | Resistance: {_money(targets.get('resistance'))}")
        else:
            lines.append(f"Conservative: {_money(targets.get('conservative'))}")
            lines.append(f"Primary: {_money(targets.get('primary'))}")
            lines.append(f"Stretch: {_money(targets.get('aggressive'))}")

    if triggers:
        lines += ["", "Watch:"]
        for item in triggers:
            lines.append(f"• {item}")

    skipped = _as_int(ladders.get("skipped_missing_expiry"), 0)
    if skipped and not compact:
        lines += ["", f"Diagnostic: skipped {skipped} malformed contract rows."]

    if not compact:
        lines += ["", "Context only — watch card, not a trade signal."]
    return "\n".join(lines)
