# app_bias.py
# 14-signal institutional dealer-flow bias engine
# Extracted from app.py for clean import by the v4 integration.
#
# ─────────────────────────────────────────────────────────────────────────────
# Patch history
# ─────────────────────────────────────────────────────────────────────────────
# v9 (Patch 1): app_bias.py hardening — Walk 1 audit follow-up.
#   - Score clamped to ±14 (raw was reachable to ±15; preserved old display contract).
#   - S1 spot==flip now scores 0 (was -2 with self-contradicting "0.0% BELOW" text).
#   - S6 explicit zero-OI handling (was silently treated as balanced via ratio=1.0 fallback).
#   - S7 zero-midpoint guard (prevents div-by-zero when both walls are 0).
#   - S13/S14 TERM signal always emits an n/a placeholder when vix9d or term is missing
#     (was silently dropped, causing n_signals to fall to 13 inconsistently).
#   - S14 term string is normalized so CONTANGO/BACKWARDATION/FLAT from upstream still map
#     to normal/inverted/flat (defensive — current upstream already lowercases).
#   - All numeric inputs run through as_float() to prevent string-from-Redis crashes.
# v9 (Patch 1.1): post-review fix-ups for Patch 1 — close as_float() coercion gaps.
#   as_float(x, 0.0) returns 0.0 for any non-numeric string. Three branches in P1
#   were treating that 0.0 as a meaningful value, turning bad data into directional
#   scores. Patch 1.1 fixes:
#   - S13 VIX: v <= 0 after coercion → emit [VIX n/a] + [TERM n/a] (was: scored +2).
#   - S10 Skew: call_iv or put_iv <= 0 → emit [SKEW n/a] (was: scored ±1).
#   - S5 regime field: isinstance(regime, dict) guard (was: AttributeError on string).
#   - S1 GEX fallback (no flip): tgex == 0 → no score (was: scored -1 with "-$0.0M").
# Released: 2026-05-XX (Friday shakedown)

from options_engine_v3 import as_float


def _calc_bias(spot: float, em: dict, walls: dict, skew: dict,
               eng: dict, pcr: dict, vix: dict) -> dict:
    """
    Institutional dealer-flow bias engine — v3.

    SIGNAL MAP (max possible score: ±14)
    ─────────────────────────────────────────────────────────────────────
    GROUP 1 — DEALER MECHANICS (highest reliability, price-forcing flows)
      S1  GEX flip position                       ±2
      S2  DEX direction                            ±2
      S3  Vanna flow                               ±1
      S4  Charm flow                               ±1
      S5  GEX regime context                        0   informational only

    GROUP 2 — OPTIONS POSITIONING (institutional OI commitment)
      S6  OI wall asymmetry                        ±1
      S7  Spot vs wall midpoint                    ±1
      S8  Gamma wall magnet                        ±1
      S9  Secondary wall cluster                    0   informational only

    GROUP 3 — SENTIMENT FLOW (real-time conviction)
      S10 IV skew                                  ±1
      S11 PCR by OI                                ±1
      S12 PCR by Volume                            ±1

    GROUP 4 — MACRO BACKDROP (context, size management)
      S13 VIX level                                ±2
      S14 VIX term structure                       ±1
    ─────────────────────────────────────────────────────────────────────

    VERDICT THRESHOLDS (out of ±14):
      ≥ +7   STRONG BULLISH       ≤ -7   STRONG BEARISH
      ≥ +3   BULLISH               ≤ -3   BEARISH
      ≥ +1   SLIGHT BULLISH        ≤ -1   SLIGHT BEARISH
      = 0    NEUTRAL

    NOTE: signals can sum to ±15 in extreme cases; final score is clamped to
    ±14 to preserve the verdict-tier contract above. Pre-clamp value is
    returned as ``raw_score`` for diagnostic use.
    """
    score = 0
    signals = []

    # v9 (Patch 1): Coerce spot once at the top so all downstream math is float-safe.
    spot = as_float(spot, 0.0)

    # ══════════════════════════════════════════════════════════════════
    # GROUP 1 — DEALER MECHANICS
    # ══════════════════════════════════════════════════════════════════

    # S1 — GEX Flip Position (weight ±2)
    if eng and eng.get("flip_price"):
        # v9 (Patch 1): coerce flip_price + tgex defensively.
        fp = as_float(eng["flip_price"], 0.0)
        tgex = as_float(eng.get("gex"), 0.0)
        if fp > 0:
            dist_pct = ((spot - fp) / fp) * 100
            # v9 (Patch 1): at-flip neutral branch — spot exactly at flip used to score
            # -2 with self-contradicting "0.0% BELOW" text. Treat |dist|<0.05% as pinned.
            if abs(dist_pct) < 0.05:
                signals.append(("◆", f"[FLIP 0] Price ${spot:.2f} is at gamma flip ${fp:.2f} ({dist_pct:+.2f}%) — pin/chop expected, no directional edge."))
            elif spot > fp:
                score += 2
                # Patch 9 (convention flip): raw tgex now agrees with geometric above flip
                # by construction. The pre-Patch-9 "tgex > 0 ? range-bound : trending likely"
                # split papered over the inverted convention; it's no longer needed.
                signals.append(("▲▲", f"[FLIP +2] Price ${spot:.2f} is {abs(dist_pct):.1f}% ABOVE gamma flip ${fp:.2f} — range-bound bias. Dealers suppress volatility above this level."))
            else:
                score -= 2
                signals.append(("▼▼", f"[FLIP -2] Price ${spot:.2f} is {abs(dist_pct):.1f}% BELOW gamma flip ${fp:.2f} — dealers AMPLIFY every move from here. Momentum and breakout setups favored."))
        else:
            # v9 (Patch 1): flip_price was truthy but coerced to 0 (bad input). Treat as missing.
            signals.append(("—", "[FLIP n/a] flip_price could not be coerced to a positive number."))
    elif eng and "gex" in eng:
        tgex = as_float(eng["gex"], 0.0)
        # v9 (Patch 1.1): old P1 said `if tgex > 0: +1 else: -1` — which scores
        # bad-coerced-to-zero data as "[GEX -1] -$0.0M". Treat exact zero as neutral.
        if tgex > 0:
            score += 1
            signals.append(("▲", f"[GEX +1] No flip found but net GEX is positive (${tgex:.1f}M) — dealers are net long gamma, suppressing moves."))
        elif tgex < 0:
            score -= 1
            signals.append(("▼", f"[GEX -1] No flip found and net GEX is negative (-${abs(tgex):.1f}M) — dealers are net short gamma, amplifying moves."))
        else:
            signals.append(("◆", "[GEX 0] No flip found and net GEX is near zero — no directional GEX edge."))
    else:
        signals.append(("—", "[FLIP n/a] No dealer GEX data available for this chain."))

    # S2 — DEX Direction (weight ±2)
    if not eng or "dex" not in eng:
        signals.append(("—", "[DEX n/a] No dealer delta exposure data available."))
    elif eng and "dex" in eng:
        # v9 (Patch 1): coerce dex.
        dex = as_float(eng["dex"], 0.0)
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
            signals.append(("◆", f"[DEX 0] Dealers near delta-neutral (DEX ${dex:+.1f}M) — no strong forced re-hedging flow in either direction."))

    # S3 — Vanna Flow (weight ±1)
    if not eng or "vanna" not in eng:
        signals.append(("—", "[VANNA n/a] No vanna flow data available."))
    elif eng and "vanna" in eng:
        # v9 (Patch 1): coerce vanna.
        vanna_m = as_float(eng["vanna"], 0.0)
        if vanna_m > 0.5:
            score += 1
            signals.append(("▲", f"[VANNA +1] Net Vanna ${vanna_m:+.1f}M — if IV rises today, dealer re-hedging adds BUYING pressure. Vol spikes will support price."))
        elif vanna_m < -0.5:
            score -= 1
            signals.append(("▼", f"[VANNA -1] Net Vanna ${vanna_m:+.1f}M — if IV rises today, dealer re-hedging adds SELLING pressure. Vol spikes will pressure price."))
        else:
            signals.append(("◆", f"[VANNA 0] Net Vanna ${vanna_m:+.1f}M — minimal IV-driven dealer flow expected."))

    # S4 — Charm Flow (weight ±1)
    if not eng or "charm" not in eng:
        signals.append(("—", "[CHARM n/a] No charm flow data available."))
    elif eng and "charm" in eng:
        # v9 (Patch 1): coerce charm.
        charm_m = as_float(eng["charm"], 0.0)
        if charm_m > 0.5:
            score += 1
            signals.append(("▲", f"[CHARM +1] Net Charm ${charm_m:+.1f}M — as today's session progresses, time-decay removes dealer short hedges. Watch for afternoon drift UP (classic 3:30 PM move)."))
        elif charm_m < -0.5:
            score -= 1
            signals.append(("▼", f"[CHARM -1] Net Charm ${charm_m:+.1f}M — as today's session progresses, time-decay ADDS dealer short hedges. Watch for afternoon drift DOWN (charm headwind into close)."))
        else:
            signals.append(("◆", f"[CHARM 0] Net Charm ${charm_m:+.1f}M — time decay has minimal directional effect on dealer hedges today."))

    # S5 — GEX Regime Context (informational, no score)
    if not eng or "gex" not in eng:
        signals.append(("—", "[GEX REGIME n/a] No GEX regime data."))
    elif eng and "gex" in eng:
        # v9 (Patch 1): coerce tgex (already done in S1 if flip_price; redo here for safety).
        tgex = as_float(eng["gex"], 0.0)
        # v9 (Patch 1.1): defend against eng["regime"] being a string or other non-dict.
        # Old P1 had `regime = eng.get("regime", {}) or {}` which only handled None/falsy;
        # a string like "MODERATE TREND" would pass that and crash on .get().
        regime = eng.get("regime", {}) or {}
        if not isinstance(regime, dict):
            regime = {}
        preferred = regime.get("preferred", "")
        avoid = regime.get("avoid", "")
        if tgex >= 0:
            signals.append(("◆", f"[GEX REGIME] POSITIVE ${tgex:.1f}M — MM suppress moves. Favors: {preferred}. Avoid: {avoid}."))
        else:
            signals.append(("⚡", f"[GEX REGIME] NEGATIVE -${abs(tgex):.1f}M — MM amplify moves. Favors: {preferred}. Avoid: {avoid}."))

    # ══════════════════════════════════════════════════════════════════
    # GROUP 2 — OPTIONS POSITIONING
    # ══════════════════════════════════════════════════════════════════

    # S6 — OI Wall Asymmetry (weight ±1)
    if not walls or "call_wall_oi" not in walls or "put_wall_oi" not in walls:
        signals.append(("—", "[OI ASYM n/a] Insufficient wall data for OI comparison."))
    elif walls and "call_wall_oi" in walls and "put_wall_oi" in walls:
        # v9 (Patch 1): coerce + explicit zero-OI handling.
        # Old code did `ratio = pw_oi / cw_oi if cw_oi > 0 else 1.0` which silently
        # treated extreme one-sided OI (e.g. cw_oi=0) as balanced. Now the zero-OI
        # cases are scored explicitly.
        cw_oi = as_float(walls["call_wall_oi"], 0.0)
        pw_oi = as_float(walls["put_wall_oi"], 0.0)
        if cw_oi <= 0 and pw_oi > 0:
            score += 1
            signals.append(("▲", f"[OI ASYM +1] Put wall has {int(pw_oi):,} OI but call wall is empty — extreme one-sided put protection. Strong buy-the-dip positioning."))
        elif pw_oi <= 0 and cw_oi > 0:
            score -= 1
            signals.append(("▼", f"[OI ASYM -1] Call wall has {int(cw_oi):,} OI but put wall is empty — extreme one-sided call hedging. Strong sell-the-rip positioning."))
        elif cw_oi <= 0 and pw_oi <= 0:
            signals.append(("—", "[OI ASYM n/a] Both walls report zero OI — chain liquidity issue."))
        else:
            ratio = pw_oi / cw_oi
            if ratio >= 1.25:
                score += 1
                signals.append(("▲", f"[OI ASYM +1] Put wall OI ({int(pw_oi):,}) dominates call wall OI ({int(cw_oi):,}) by {ratio:.1f}x — heavy downside protection paid for. Strong buy-the-dip positioning."))
            elif ratio >= 1.10:
                signals.append(("◆", f"[OI ASYM 0] Slight put OI edge ({int(pw_oi):,} vs {int(cw_oi):,}, {ratio:.1f}x) — mild downside protection lean. Not conclusive."))
            elif ratio <= 0.80:
                score -= 1
                signals.append(("▼", f"[OI ASYM -1] Call wall OI ({int(cw_oi):,}) dominates put wall OI ({int(pw_oi):,}) by {1/ratio:.1f}x — heavy upside hedging. Strong sell-the-rip positioning."))
            elif ratio <= 0.90:
                signals.append(("◆", f"[OI ASYM 0] Slight call OI edge ({int(cw_oi):,} vs {int(pw_oi):,}, {1/ratio:.1f}x) — mild upside hedging lean. Not conclusive."))
            else:
                signals.append(("◆", f"[OI ASYM 0] OI is balanced (put {int(pw_oi):,} vs call {int(cw_oi):,}, {ratio:.2f}x) — no dominant institutional lean."))

    # S7 — Spot vs Wall Midpoint (weight ±1)
    if not walls or "call_wall" not in walls or "put_wall" not in walls:
        signals.append(("—", "[MIDPOINT n/a] No call/put wall to compute midpoint."))
    elif walls and "call_wall" in walls and "put_wall" in walls:
        # v9 (Patch 1): coerce + zero-midpoint guard.
        cw = as_float(walls["call_wall"], 0.0)
        pw = as_float(walls["put_wall"], 0.0)
        mid = (cw + pw) / 2
        if mid <= 0:
            signals.append(("—", "[MIDPOINT n/a] Wall midpoint is zero or negative — bad wall data."))
        else:
            dist_pct = ((spot - mid) / mid) * 100
            if dist_pct >= 0.30:
                score += 1
                signals.append(("▲", f"[MIDPOINT +1] Price ${spot:.2f} is {dist_pct:.1f}% above midpoint ${mid:.2f} — positioned in upper half of the dealer range. Bullish bias within structure."))
            elif dist_pct <= -0.30:
                score -= 1
                signals.append(("▼", f"[MIDPOINT -1] Price ${spot:.2f} is {abs(dist_pct):.1f}% below midpoint ${mid:.2f} — positioned in lower half of the dealer range. Bearish bias within structure."))
            else:
                signals.append(("◆", f"[MIDPOINT 0] Price ${spot:.2f} near midpoint ${mid:.2f} ({dist_pct:+.1f}%) — no positional edge within the range."))

    # S8 — Gamma Wall as Price Magnet (weight ±1)
    if not walls or "gamma_wall" not in walls:
        signals.append(("—", "[GAMMA WALL n/a] No gamma wall identified."))
    elif walls and "gamma_wall" in walls:
        # v9 (Patch 1): coerce + guard.
        gw = as_float(walls["gamma_wall"], 0.0)
        if spot <= 0 or gw <= 0:
            signals.append(("—", "[GAMMA WALL n/a] Spot or gamma_wall non-positive."))
        else:
            gw_dist_pct = ((gw - spot) / spot) * 100
            if abs(gw_dist_pct) <= 0.30:
                signals.append(("◆", f"[GAMMA WALL 0] Gamma wall ${gw:.0f} is very close to spot ({gw_dist_pct:+.1f}%) — price is PINNED. Expect tight chop around this strike today."))
            elif gw > spot:
                score += 1
                signals.append(("▲", f"[GAMMA WALL +1] Gamma wall ${gw:.0f} is {gw_dist_pct:.1f}% ABOVE spot — acts as upside magnet. Price may drift toward it during the session."))
            else:
                score -= 1
                signals.append(("▼", f"[GAMMA WALL -1] Gamma wall ${gw:.0f} is {abs(gw_dist_pct):.1f}% BELOW spot — acts as downside magnet. Price may drift toward it during the session."))

    # S9 — Secondary Wall Clusters (informational, no score)
    if not walls or "call_top3" not in walls or "put_top3" not in walls:
        signals.append(("—", "[CLUSTERS n/a] No wall cluster data."))
    elif walls and "call_top3" in walls and "put_top3" in walls:
        # v9 (Patch 1): coerce list members defensively.
        try:
            ct3 = " → ".join(f"${as_float(x, 0.0):.0f}" for x in walls["call_top3"])
            pt3 = " → ".join(f"${as_float(x, 0.0):.0f}" for x in walls["put_top3"])
            signals.append(("◆", f"[CLUSTERS] Resistance stack: {ct3} | Support stack: {pt3}"))
        except (TypeError, ValueError):
            signals.append(("—", "[CLUSTERS n/a] Wall cluster data malformed."))

    # ══════════════════════════════════════════════════════════════════
    # GROUP 3 — SENTIMENT FLOW
    # ══════════════════════════════════════════════════════════════════

    # S10 — IV Skew (weight ±1)
    if not skew or "call_iv" not in skew or "put_iv" not in skew:
        signals.append(("—", "[SKEW n/a] No ATM IV skew data available."))
    elif skew and "call_iv" in skew and "put_iv" in skew:
        # v9 (Patch 1): coerce.
        call_iv = as_float(skew["call_iv"], 0.0)
        put_iv = as_float(skew["put_iv"], 0.0)
        # v9 (Patch 1.1): if either IV coerces to ≤0 (bad string from Redis), the
        # diff is meaningless. Old P1 would compute e.g. diff = put_iv - 0.0 and
        # emit a directional skew score from invalid data.
        if call_iv <= 0 or put_iv <= 0:
            signals.append(("—", "[SKEW n/a] ATM IV skew data invalid (non-positive after coercion)."))
        else:
            diff = put_iv - call_iv
            if diff >= 2.5:
                score -= 1
                signals.append(("▼", f"[SKEW -1] Strong fear skew: puts {put_iv}% vs calls {call_iv}% (+{diff:.1f}pp) — market paying heavy premium to hedge downside. Genuine fear."))
            elif diff >= 1.0:
                signals.append(("◆", f"[SKEW 0] Mild fear skew: puts {put_iv}% vs calls {call_iv}% (+{diff:.1f}pp) — normal put premium, no strong signal."))
            elif diff <= -2.5:
                score += 1
                signals.append(("▲", f"[SKEW +1] Greed skew: calls {call_iv}% vs puts {put_iv}% ({abs(diff):.1f}pp) — market paying heavy premium to chase upside. Genuine momentum."))
            elif diff <= -1.0:
                signals.append(("◆", f"[SKEW 0] Mild greed skew: calls {call_iv}% vs puts {put_iv}% ({abs(diff):.1f}pp) — slight call premium, no strong signal."))
            else:
                signals.append(("◆", f"[SKEW 0] IV balanced: calls {call_iv}% / puts {put_iv}% ({diff:+.1f}pp) — no directional conviction from skew."))

    # S11 — PCR by OI (weight ±1)
    if not pcr or pcr.get("pcr_oi") is None:
        signals.append(("—", "[PCR OI n/a] No put/call ratio data by OI."))
    elif pcr and pcr.get("pcr_oi") is not None:
        # v9 (Patch 1): coerce.
        p = as_float(pcr["pcr_oi"], 0.0)
        if p > 1.35:
            score -= 1
            signals.append(("▼", f"[PCR OI -1] PCR(OI) {p:.2f} — very high put skew. Market is structurally positioned defensively. Bearish sentiment is baked into existing positions."))
        elif p > 1.1:
            signals.append(("◆", f"[PCR OI 0] PCR(OI) {p:.2f} — mildly elevated puts. Defensive lean but not extreme."))
        elif p < 0.65 and p > 0:
            score += 1
            signals.append(("▲", f"[PCR OI +1] PCR(OI) {p:.2f} — very low, call-dominant positioning. Market is structurally bullish in existing positions."))
        elif p < 0.85:
            signals.append(("◆", f"[PCR OI 0] PCR(OI) {p:.2f} — mildly elevated calls. Bullish lean but not extreme."))
        else:
            signals.append(("◆", f"[PCR OI 0] PCR(OI) {p:.2f} — balanced positioning. No strong structural sentiment."))

    # S12 — PCR by Volume (weight ±1)
    if not pcr or pcr.get("pcr_vol") is None:
        signals.append(("—", "[PCR VOL n/a] No put/call ratio data by volume."))
    elif pcr and pcr.get("pcr_vol") is not None:
        # v9 (Patch 1): coerce.
        pv = as_float(pcr["pcr_vol"], 0.0)
        if pv > 1.35:
            score -= 1
            signals.append(("▼", f"[PCR VOL -1] PCR(Vol) {pv:.2f} — traders are ACTIVELY buying puts today. Real-time bearish flow — more urgent signal than OI."))
        elif pv > 1.1:
            signals.append(("◆", f"[PCR VOL 0] PCR(Vol) {pv:.2f} — slightly more put volume today. Mild defensive flow."))
        elif pv < 0.65 and pv > 0:
            score += 1
            signals.append(("▲", f"[PCR VOL +1] PCR(Vol) {pv:.2f} — traders are ACTIVELY buying calls today. Real-time bullish flow — more urgent signal than OI."))
        elif pv < 0.85:
            signals.append(("◆", f"[PCR VOL 0] PCR(Vol) {pv:.2f} — slightly more call volume today. Mild bullish flow."))
        else:
            signals.append(("◆", f"[PCR VOL 0] PCR(Vol) {pv:.2f} — balanced today's flow. No real-time directional conviction."))

    # ══════════════════════════════════════════════════════════════════
    # GROUP 4 — MACRO BACKDROP
    # ══════════════════════════════════════════════════════════════════

    # S13 — VIX Level (weight ±2)
    if not vix or not vix.get("vix"):
        signals.append(("—", "[VIX n/a] VIX data unavailable."))
        signals.append(("—", "[TERM n/a] VIX term structure unavailable."))
    elif vix and vix.get("vix"):
        # v9 (Patch 1): coerce v + v9d.
        v = as_float(vix["vix"], 0.0)
        v9d = as_float(vix.get("vix9d"), 0.0)

        # v9 (Patch 1.1): if vix["vix"] was a non-numeric string ("abc", "n/a", etc.)
        # it would pass the truthy check above but coerce to 0.0, then trigger the
        # `v < 12 → score += 2` branch and emit "[VIX +2] VIX 0.0". Reject post-coercion.
        if v <= 0:
            signals.append(("—", "[VIX n/a] VIX data unavailable or invalid."))
            signals.append(("—", "[TERM n/a] VIX term structure unavailable."))
        else:
            # v9 (Patch 1): normalize term string. Upstream may emit either
            # "normal"/"inverted"/"flat" (lowercase, current convention) or
            # "CONTANGO"/"BACKWARDATION"/"FLAT"/"SEVERE_BACKWARDATION" (vix_term_structure.py
            # canonical labels). Map both into the lowercase convention this function expects.
            raw_term = str(vix.get("term") or vix.get("term_structure") or "").strip().upper()
            if raw_term in ("NORMAL", "CONTANGO"):
                term = "normal"
            elif raw_term in ("INVERTED", "BACKWARDATION", "SEVERE_BACKWARDATION"):
                term = "inverted"
            elif raw_term == "FLAT":
                term = "flat"
            else:
                term = "unknown"

            if v >= 40:
                score -= 2
                signals.append(("▼▼", f"[VIX -2] VIX {v} — EXTREME fear/crisis. EM ranges may be too small. Dealer hedging breaks down at these levels. Consider sitting out or minimum size."))
            elif v >= 28:
                score -= 1
                signals.append(("▼", f"[VIX -1] VIX {v} — elevated fear. Market is unstable. Use wider stops, smaller size. EM may understate risk."))
            elif v >= 18:
                signals.append(("◆", f"[VIX 0] VIX {v} — above-average uncertainty. Normal risk management. EM ranges are appropriate."))
            elif v >= 12:
                score += 1
                signals.append(("▲", f"[VIX +1] VIX {v} — calm environment. Low fear, orderly market. Dealer hedging is predictable. EM ranges are reliable."))
            else:
                score += 2
                signals.append(("▲▲", f"[VIX +2] VIX {v} — extremely low fear. Market is complacent. Dealer flows are very predictable. EM ranges highly reliable."))

            # S14 — VIX Term Structure (weight ±1)
            # v9 (Patch 1): always emit a TERM signal — never silently drop.
            # Old code only appended on ("inverted","normal","flat") and dropped on
            # ("unknown",None,anything else), causing n_signals to fall to 13 inconsistently.
            if v9d <= 0:
                signals.append(("—", "[TERM n/a] VIX9D unavailable."))
            elif term == "inverted":
                score -= 1
                delta_v = round(v9d - v, 1)
                signals.append(("▼", f"[TERM -1] VIX term INVERTED — VIX9D {v9d} is {delta_v}pt ABOVE VIX {v}. Near-term fear exceeds 30-day average. Something is breaking down RIGHT NOW. High urgency warning."))
            elif term == "normal":
                score += 1
                delta_v = round(v - v9d, 1)
                signals.append(("▲", f"[TERM +1] VIX term normal — VIX9D {v9d} is {delta_v}pt BELOW VIX {v}. Near-term is calmer than the 30-day average. Today should be relatively stable."))
            elif term == "flat":
                signals.append(("◆", f"[TERM 0] VIX term flat — VIX9D {v9d} ≈ VIX {v}. Consistent fear across timeframes, no term structure edge."))
            else:
                signals.append(("—", f"[TERM n/a] Unknown term classification: {raw_term!r}"))

    # ══════════════════════════════════════════════════════════════════
    # VERDICT
    # ══════════════════════════════════════════════════════════════════
    up_count = sum(1 for e, _ in signals if e in ("▲", "▲▲"))
    down_count = sum(1 for e, _ in signals if e in ("▼", "▼▼"))
    na_count = sum(1 for e, _ in signals if e == "—")
    neu_count = len(signals) - up_count - down_count - na_count

    # v9 (Patch 1): Clamp score to ±14 to preserve the verdict-tier contract.
    # Raw signals can sum to ±15 in extreme cases (S1 ±2 + S2 ±2 + S3 ±1 + S4 ±1 +
    # S6 ±1 + S7 ±1 + S8 ±1 + S10 ±1 + S11 ±1 + S12 ±1 + S13 ±2 + S14 ±1 = ±15).
    # Pre-clamp value preserved as raw_score for diagnostics.
    raw_score = score
    if score > 14:
        score = 14
    elif score < -14:
        score = -14

    if score >= 7:
        direction = "STRONG BULLISH"
        strength = "High Conviction"
        verdict = (
            "High-conviction bullish setup. Dealer mechanics, positioning, and sentiment all align. "
            "Favor ITM call debit spreads or bull setups. Size normally with standard stops."
        )
    elif score >= 3:
        direction = "BULLISH"
        strength = "Moderate"
        verdict = (
            "Solid bullish lean. Multiple independent signals favor upside. "
            "Prefer bull setups. Size normally but confirm with price action before entry."
        )
    elif score >= 1:
        direction = "SLIGHT BULLISH"
        strength = "Weak"
        verdict = (
            "Marginal bullish edge. More signals favor upside than down but conviction is low. "
            "Take bull setups only on clean entries. Tighter stops than normal."
        )
    elif score == 0:
        direction = "NEUTRAL"
        strength = ""
        verdict = (
            "Signals are genuinely split. No structural edge in either direction. "
            "Range-bound or unpredictable chop likely. Reduce size significantly or wait for a cleaner setup."
        )
    elif score >= -2:
        direction = "SLIGHT BEARISH"
        strength = "Weak"
        verdict = (
            "Marginal bearish edge. More signals favor downside than up but conviction is low. "
            "Take bear setups only on clean entries. Tighter stops than normal."
        )
    elif score >= -6:
        direction = "BEARISH"
        strength = "Moderate"
        verdict = (
            "Solid bearish lean. Multiple independent signals favor downside. "
            "Prefer bear setups. Size normally but confirm with price action before entry."
        )
    else:
        direction = "STRONG BEARISH"
        strength = "High Conviction"
        verdict = (
            "High-conviction bearish setup. Dealer mechanics, positioning, and sentiment all align. "
            "Favor ITM put debit spreads or bear setups. Size normally with standard stops."
        )

    return {
        "direction": direction,
        "strength": strength,
        "score": score,
        "raw_score": raw_score,           # v9 (Patch 1): pre-clamp value for diagnostics
        "max_score": 14,
        "up_count": up_count,
        "down_count": down_count,
        "neu_count": neu_count,
        "na_count": na_count,
        "n_signals": len(signals),
        "signals": signals,
        "verdict": verdict,
    }
