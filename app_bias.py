# app_bias.py
# 14-signal institutional dealer-flow bias engine
# Extracted from app.py for clean import by the v4 integration.
# This function is UNCHANGED — same inputs, same outputs, same scoring.

from options_engine_v3 import as_float


def _calc_bias(spot: float, em: dict, walls: dict, skew: dict,
               eng: dict, pcr: dict, vix: dict) -> dict:
    """
    Institutional dealer-flow bias engine — v3.

    SIGNAL MAP (max possible score: ±14)
    ─────────────────────────────────────────────────────────────────────
    GROUP 1 — DEALER MECHANICS (highest reliability, price-forcing flows)
      S1  GEX flip position       ±2
      S2  DEX direction           ±2
      S3  Vanna flow              ±1
      S4  Charm flow              ±1
      S5  GEX regime context       0   informational only
    GROUP 2 — OPTIONS POSITIONING (institutional OI commitment)
      S6  OI wall asymmetry       ±1
      S7  Spot vs wall midpoint   ±1
      S8  Gamma wall magnet       ±1
      S9  Secondary wall cluster   0   informational only
    GROUP 3 — SENTIMENT FLOW (real-time conviction)
      S10 IV skew                 ±1
      S11 PCR by OI               ±1
      S12 PCR by Volume           ±1
    GROUP 4 — MACRO BACKDROP (context, size management)
      S13 VIX level               ±2
      S14 VIX term structure      ±1
    ─────────────────────────────────────────────────────────────────────
    VERDICT THRESHOLDS (out of ±14):
      ≥ +7  STRONG BULLISH       ≤ -7  STRONG BEARISH
      ≥ +3  BULLISH              ≤ -3  BEARISH
      ≥ +1  SLIGHT BULLISH       ≤ -1  SLIGHT BEARISH
       = 0  NEUTRAL
    """
    score   = 0
    signals = []

    # ══════════════════════════════════════════════════════════════════
    # GROUP 1 — DEALER MECHANICS
    # ══════════════════════════════════════════════════════════════════

    # S1 — GEX Flip Position (weight ±2)
    if eng and eng.get("flip_price"):
        fp       = eng["flip_price"]
        dist_pct = ((spot - fp) / fp) * 100
        tgex     = eng.get("gex", 0)
        if spot > fp:
            score += 2
            ctx = "range-bound bias" if tgex > 0 else "above flip but still negative GEX — trending likely"
            signals.append(("▲▲", f"[FLIP +2] Price ${spot:.2f} is {abs(dist_pct):.1f}% ABOVE gamma flip ${fp:.2f} — {ctx}. Dealers suppress volatility above this level."))
        else:
            score -= 2
            signals.append(("▼▼", f"[FLIP -2] Price ${spot:.2f} is {abs(dist_pct):.1f}% BELOW gamma flip ${fp:.2f} — dealers AMPLIFY every move from here. Momentum and breakout setups favored."))
    elif eng and "gex" in eng:
        tgex = eng["gex"]
        if tgex > 0:
            score += 1
            signals.append(("▲", f"[GEX +1] No flip found but net GEX is positive (${tgex:.1f}M) — dealers are net long gamma, suppressing moves."))
        else:
            score -= 1
            signals.append(("▼", f"[GEX -1] No flip found and net GEX is negative (-${abs(tgex):.1f}M) — dealers are net short gamma, amplifying moves."))

    # S2 — DEX Direction (weight ±2)
    if eng and "dex" in eng:
        dex  = eng["dex"]
        adex = abs(dex)
        if dex < -1.0:
            score += 2
            signals.append(("▲▲", f"[DEX +2] Dealers are net SHORT delta (DEX -${adex:.1f}M) — they MUST BUY shares as price rises. Every rally gets mechanical buying fuel added."))
        elif dex < -0.25:
            score += 1
            signals.append(("▲", f"[DEX +1] Dealers mildly short delta (DEX -${adex:.1f}M) — some buying fuel on upside moves."))
        elif dex > 1.0:
            score -= 2
            signals.append(("▼▼", f"[DEX -2] Dealers are net LONG delta (DEX +${dex:.1f}M) — they MUST SELL shares as price falls. Every drop gets mechanical selling added."))
        elif dex > 0.25:
            score -= 1
            signals.append(("▼", f"[DEX -1] Dealers mildly long delta (DEX +${dex:.1f}M) — some selling pressure on downside moves."))
        else:
            signals.append(("◆", f"[DEX  0] Dealers near delta-neutral (DEX ${dex:+.1f}M) — no strong forced re-hedging flow in either direction."))

    # S3 — Vanna Flow (weight ±1)
    if eng and "vanna" in eng:
        vanna_m = eng["vanna"]
        if vanna_m > 0.5:
            score += 1
            signals.append(("▲", f"[VANNA +1] Net Vanna ${vanna_m:+.1f}M — if IV rises today, dealer re-hedging adds BUYING pressure. Vol spikes will support price."))
        elif vanna_m < -0.5:
            score -= 1
            signals.append(("▼", f"[VANNA -1] Net Vanna ${vanna_m:+.1f}M — if IV rises today, dealer re-hedging adds SELLING pressure. Vol spikes will pressure price."))
        else:
            signals.append(("◆", f"[VANNA  0] Net Vanna ${vanna_m:+.1f}M — minimal IV-driven dealer flow expected."))

    # S4 — Charm Flow (weight ±1)
    if eng and "charm" in eng:
        charm_m = eng["charm"]
        if charm_m > 0.5:
            score += 1
            signals.append(("▲", f"[CHARM +1] Net Charm ${charm_m:+.1f}M — as today's session progresses, time-decay removes dealer short hedges. Watch for afternoon drift UP (classic 3:30 PM move)."))
        elif charm_m < -0.5:
            score -= 1
            signals.append(("▼", f"[CHARM -1] Net Charm ${charm_m:+.1f}M — as today's session progresses, time-decay ADDS dealer short hedges. Watch for afternoon drift DOWN (charm headwind into close)."))
        else:
            signals.append(("◆", f"[CHARM  0] Net Charm ${charm_m:+.1f}M — time decay has minimal directional effect on dealer hedges today."))

    # S5 — GEX Regime Context (informational, no score)
    if eng and "gex" in eng:
        tgex = eng["gex"]
        regime = eng.get("regime", {})
        preferred = regime.get("preferred", "")
        avoid     = regime.get("avoid", "")
        if tgex >= 0:
            signals.append(("◆", f"[GEX REGIME] POSITIVE ${tgex:.1f}M — MM suppress moves. Favors: {preferred}. Avoid: {avoid}."))
        else:
            signals.append(("⚡", f"[GEX REGIME] NEGATIVE -${abs(tgex):.1f}M — MM amplify moves. Favors: {preferred}. Avoid: {avoid}."))

    # ══════════════════════════════════════════════════════════════════
    # GROUP 2 — OPTIONS POSITIONING
    # ══════════════════════════════════════════════════════════════════

    # S6 — OI Wall Asymmetry (weight ±1)
    if walls and "call_wall_oi" in walls and "put_wall_oi" in walls:
        cw_oi = walls["call_wall_oi"]
        pw_oi = walls["put_wall_oi"]
        ratio = pw_oi / cw_oi if cw_oi > 0 else 1.0
        if ratio >= 1.25:
            score += 1
            signals.append(("▲", f"[OI ASYM +1] Put wall OI ({pw_oi:,}) dominates call wall OI ({cw_oi:,}) by {ratio:.1f}x — heavy downside protection paid for. Strong buy-the-dip positioning."))
        elif ratio >= 1.10:
            signals.append(("◆", f"[OI ASYM  0] Slight put OI edge ({pw_oi:,} vs {cw_oi:,}, {ratio:.1f}x) — mild downside protection lean. Not conclusive."))
        elif ratio <= 0.80:
            score -= 1
            signals.append(("▼", f"[OI ASYM -1] Call wall OI ({cw_oi:,}) dominates put wall OI ({pw_oi:,}) by {1/ratio:.1f}x — heavy upside hedging. Strong sell-the-rip positioning."))
        elif ratio <= 0.90:
            signals.append(("◆", f"[OI ASYM  0] Slight call OI edge ({cw_oi:,} vs {pw_oi:,}, {1/ratio:.1f}x) — mild upside hedging lean. Not conclusive."))
        else:
            signals.append(("◆", f"[OI ASYM  0] OI is balanced (put {pw_oi:,} vs call {cw_oi:,}, {ratio:.2f}x) — no dominant institutional lean."))

    # S7 — Spot vs Wall Midpoint (weight ±1)
    if walls and "call_wall" in walls and "put_wall" in walls:
        mid      = (walls["call_wall"] + walls["put_wall"]) / 2
        dist_pct = ((spot - mid) / mid) * 100
        if dist_pct >= 0.30:
            score += 1
            signals.append(("▲", f"[MIDPOINT +1] Price ${spot:.2f} is {dist_pct:.1f}% above midpoint ${mid:.2f} — positioned in upper half of the dealer range. Bullish bias within structure."))
        elif dist_pct <= -0.30:
            score -= 1
            signals.append(("▼", f"[MIDPOINT -1] Price ${spot:.2f} is {abs(dist_pct):.1f}% below midpoint ${mid:.2f} — positioned in lower half of the dealer range. Bearish bias within structure."))
        else:
            signals.append(("◆", f"[MIDPOINT  0] Price ${spot:.2f} near midpoint ${mid:.2f} ({dist_pct:+.1f}%) — no positional edge within the range."))

    # S8 — Gamma Wall as Price Magnet (weight ±1)
    if walls and "gamma_wall" in walls:
        gw           = walls["gamma_wall"]
        gw_dist_pct  = ((gw - spot) / spot) * 100
        if abs(gw_dist_pct) <= 0.30:
            signals.append(("◆", f"[GAMMA WALL  0] Gamma wall ${gw:.0f} is very close to spot ({gw_dist_pct:+.1f}%) — price is PINNED. Expect tight chop around this strike today."))
        elif gw > spot:
            score += 1
            signals.append(("▲", f"[GAMMA WALL +1] Gamma wall ${gw:.0f} is {gw_dist_pct:.1f}% ABOVE spot — acts as upside magnet. Price may drift toward it during the session."))
        else:
            score -= 1
            signals.append(("▼", f"[GAMMA WALL -1] Gamma wall ${gw:.0f} is {abs(gw_dist_pct):.1f}% BELOW spot — acts as downside magnet. Price may drift toward it during the session."))

    # S9 — Secondary Wall Clusters (informational, no score)
    if walls and "call_top3" in walls and "put_top3" in walls:
        ct3 = " → ".join(f"${x:.0f}" for x in walls["call_top3"])
        pt3 = " → ".join(f"${x:.0f}" for x in walls["put_top3"])
        signals.append(("◆", f"[CLUSTERS] Resistance stack: {ct3} | Support stack: {pt3}"))

    # ══════════════════════════════════════════════════════════════════
    # GROUP 3 — SENTIMENT FLOW
    # ══════════════════════════════════════════════════════════════════

    # S10 — IV Skew (weight ±1)
    if skew and "call_iv" in skew and "put_iv" in skew:
        diff = skew["put_iv"] - skew["call_iv"]
        if diff >= 2.5:
            score -= 1
            signals.append(("▼", f"[SKEW -1] Strong fear skew: puts {skew['put_iv']}% vs calls {skew['call_iv']}% (+{diff:.1f}pp) — market paying heavy premium to hedge downside. Genuine fear."))
        elif diff >= 1.0:
            signals.append(("◆", f"[SKEW  0] Mild fear skew: puts {skew['put_iv']}% vs calls {skew['call_iv']}% (+{diff:.1f}pp) — normal put premium, no strong signal."))
        elif diff <= -2.5:
            score += 1
            signals.append(("▲", f"[SKEW +1] Greed skew: calls {skew['call_iv']}% vs puts {skew['put_iv']}% ({abs(diff):.1f}pp) — market paying heavy premium to chase upside. Genuine momentum."))
        elif diff <= -1.0:
            signals.append(("◆", f"[SKEW  0] Mild greed skew: calls {skew['call_iv']}% vs puts {skew['put_iv']}% ({abs(diff):.1f}pp) — slight call premium, no strong signal."))
        else:
            signals.append(("◆", f"[SKEW  0] IV balanced: calls {skew.get('call_iv','?')}% / puts {skew.get('put_iv','?')}% ({diff:+.1f}pp) — no directional conviction from skew."))

    # S11 — PCR by OI (weight ±1)
    if pcr and pcr.get("pcr_oi") is not None:
        p = pcr["pcr_oi"]
        if p > 1.35:
            score -= 1
            signals.append(("▼", f"[PCR OI -1] PCR(OI) {p:.2f} — very high put skew. Market is structurally positioned defensively. Bearish sentiment is baked into existing positions."))
        elif p > 1.1:
            signals.append(("◆", f"[PCR OI  0] PCR(OI) {p:.2f} — mildly elevated puts. Defensive lean but not extreme."))
        elif p < 0.65:
            score += 1
            signals.append(("▲", f"[PCR OI +1] PCR(OI) {p:.2f} — very low, call-dominant positioning. Market is structurally bullish in existing positions."))
        elif p < 0.85:
            signals.append(("◆", f"[PCR OI  0] PCR(OI) {p:.2f} — mildly elevated calls. Bullish lean but not extreme."))
        else:
            signals.append(("◆", f"[PCR OI  0] PCR(OI) {p:.2f} — balanced positioning. No strong structural sentiment."))

    # S12 — PCR by Volume (weight ±1)
    if pcr and pcr.get("pcr_vol") is not None:
        pv = pcr["pcr_vol"]
        if pv > 1.35:
            score -= 1
            signals.append(("▼", f"[PCR VOL -1] PCR(Vol) {pv:.2f} — traders are ACTIVELY buying puts today. Real-time bearish flow — more urgent signal than OI."))
        elif pv > 1.1:
            signals.append(("◆", f"[PCR VOL  0] PCR(Vol) {pv:.2f} — slightly more put volume today. Mild defensive flow."))
        elif pv < 0.65:
            score += 1
            signals.append(("▲", f"[PCR VOL +1] PCR(Vol) {pv:.2f} — traders are ACTIVELY buying calls today. Real-time bullish flow — more urgent signal than OI."))
        elif pv < 0.85:
            signals.append(("◆", f"[PCR VOL  0] PCR(Vol) {pv:.2f} — slightly more call volume today. Mild bullish flow."))
        else:
            signals.append(("◆", f"[PCR VOL  0] PCR(Vol) {pv:.2f} — balanced today's flow. No real-time directional conviction."))

    # ══════════════════════════════════════════════════════════════════
    # GROUP 4 — MACRO BACKDROP
    # ══════════════════════════════════════════════════════════════════

    # S13 — VIX Level (weight ±2)
    if vix and vix.get("vix"):
        v    = vix["vix"]
        v9d  = vix.get("vix9d")
        term = vix.get("term", "unknown")

        if v >= 40:
            score -= 2
            signals.append(("▼▼", f"[VIX -2] VIX {v} — EXTREME fear/crisis. EM ranges may be too small. Dealer hedging breaks down at these levels. Consider sitting out or minimum size."))
        elif v >= 28:
            score -= 1
            signals.append(("▼", f"[VIX -1] VIX {v} — elevated fear. Market is unstable. Use wider stops, smaller size. EM may understate risk."))
        elif v >= 18:
            signals.append(("◆", f"[VIX  0] VIX {v} — above-average uncertainty. Normal risk management. EM ranges are appropriate."))
        elif v >= 12:
            score += 1
            signals.append(("▲", f"[VIX +1] VIX {v} — calm environment. Low fear, orderly market. Dealer hedging is predictable. EM ranges are reliable."))
        else:
            score += 2
            signals.append(("▲▲", f"[VIX +2] VIX {v} — extremely low fear. Market is complacent. Dealer flows are very predictable. EM ranges highly reliable."))

        # S14 — VIX Term Structure (weight ±1)
        if v9d and term == "inverted":
            score -= 1
            delta_v = round(v9d - v, 1)
            signals.append(("▼", f"[TERM -1] VIX term INVERTED — VIX9D {v9d} is {delta_v}pt ABOVE VIX {v}. Near-term fear exceeds 30-day average. Something is breaking down RIGHT NOW. High urgency warning."))
        elif v9d and term == "normal":
            score += 1
            delta_v = round(v - v9d, 1)
            signals.append(("▲", f"[TERM +1] VIX term normal — VIX9D {v9d} is {delta_v}pt BELOW VIX {v}. Near-term is calmer than the 30-day average. Today should be relatively stable."))
        elif v9d and term == "flat":
            signals.append(("◆", f"[TERM  0] VIX term flat — VIX9D {v9d} ≈ VIX {v}. Consistent fear across timeframes, no term structure edge."))

    # ══════════════════════════════════════════════════════════════════
    # VERDICT
    # ══════════════════════════════════════════════════════════════════
    up_count   = sum(1 for e, _ in signals if e in ("▲", "▲▲"))
    down_count = sum(1 for e, _ in signals if e in ("▼", "▼▼"))
    neu_count  = len(signals) - up_count - down_count

    if score >= 7:
        direction = "STRONG BULLISH"
        strength  = "High Conviction"
        verdict   = (
            "High-conviction bullish setup. Dealer mechanics, positioning, and sentiment all align. "
            "Favor ITM call debit spreads or bull setups. Size normally with standard stops."
        )
    elif score >= 3:
        direction = "BULLISH"
        strength  = "Moderate"
        verdict   = (
            "Solid bullish lean. Multiple independent signals favor upside. "
            "Prefer bull setups. Size normally but confirm with price action before entry."
        )
    elif score >= 1:
        direction = "SLIGHT BULLISH"
        strength  = "Weak"
        verdict   = (
            "Marginal bullish edge. More signals favor upside than down but conviction is low. "
            "Take bull setups only on clean entries. Tighter stops than normal."
        )
    elif score == 0:
        direction = "NEUTRAL"
        strength  = ""
        verdict   = (
            "Signals are genuinely split. No structural edge in either direction. "
            "Range-bound or unpredictable chop likely. Reduce size significantly or wait for a cleaner setup."
        )
    elif score >= -2:
        direction = "SLIGHT BEARISH"
        strength  = "Weak"
        verdict   = (
            "Marginal bearish edge. More signals favor downside than up but conviction is low. "
            "Take bear setups only on clean entries. Tighter stops than normal."
        )
    elif score >= -6:
        direction = "BEARISH"
        strength  = "Moderate"
        verdict   = (
            "Solid bearish lean. Multiple independent signals favor downside. "
            "Prefer bear setups. Size normally but confirm with price action before entry."
        )
    else:
        direction = "STRONG BEARISH"
        strength  = "High Conviction"
        verdict   = (
            "High-conviction bearish setup. Dealer mechanics, positioning, and sentiment all align. "
            "Favor ITM put debit spreads or bear setups. Size normally with standard stops."
        )

    return {
        "direction":  direction,
        "strength":   strength,
        "score":      score,
        "max_score":  14,
        "up_count":   up_count,
        "down_count": down_count,
        "neu_count":  neu_count,
        "n_signals":  len(signals),
        "signals":    signals,
        "verdict":    verdict,
    }
