# backtest_guidance.py
# ═══════════════════════════════════════════════════════════════════
# Backtest-Derived Guidance — v7 Results (April 2026)
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Provides per-ticker, per-regime, per-direction guidance blocks
# for trade cards based on the v7 corrected backtest results.
#
# Used by: ticker_rules.build_alert_message(), format_trade_card(),
#          format_swing_card(), income_scanner
# ═══════════════════════════════════════════════════════════════════

# ── Active Scanner Guidance ──────────────────────────────────────
# From bt_active.py v7 corrected rerun (July 2025 – April 2026, Schwab data)

ACTIVE_GUIDANCE = {
    # Tier 1: Core — highest confidence edge
    "GOOGL": {
        "tier": "core",
        "best_bias": "bull",
        "optimal_hold": 5,
        "regime_note": "Best in both BULL and TRANSITION",
        "wr_3d": 65.7, "wr_5d": 62.3, "pf_5d": 3.11, "n": 548,
        "ev_5d": 1.717,
        "htf_note": "CONFIRMED and CONVERGING both work",
        "phase_note": "Midday 3d: 75% WR, PF 4.9 — strongest phase",
        "hold_warning": None,
        "conviction_eligible": True, "conviction_wr_5d": 66.7, "conviction_ev_5d": 2.49,
        "card_lines": [
            "Hold: 5 days (WR improves 3d→5d)",
            "GOOGL bull is the cleanest active-scanner edge (PF 3.11)",
            "Midday signals are premium quality — prioritize over afternoon",
        ],
        "never_lines": [],
    },
    "SLV": {
        "tier": "core",
        "best_bias": "bull",
        "optimal_hold": 5,
        "regime_note": "BULL regime only — inactive in TRANSITION/BEAR",
        "wr_3d": 62.3, "wr_5d": 68.0, "pf_5d": 1.65, "n": 228,
        "ev_5d": 1.619,
        "htf_note": "CONFIRMED required (98% of signals)",
        "phase_note": "Afternoon slightly better than midday",
        "hold_warning": None,
        "conviction_eligible": False,
        "card_lines": [
            "Hold: 5 days — WR increases from 62% at 3d to 68% at 5d",
            "Bull-only in BULL regime",
        ],
        "never_lines": ["Take signals in TRANSITION or BEAR regime"],
    },
    "GLD": {
        "tier": "core",
        "best_bias": "bull",
        "optimal_hold": 5,
        "regime_note": "BULL regime only",
        "wr_3d": 65.3, "wr_5d": 61.2, "pf_5d": 1.94, "n": 170,
        "ev_5d": 1.119,
        "htf_note": "CONFIRMED required",
        "phase_note": "Midday 3d: 78.9% WR, PF 10.57 — exceptional",
        "hold_warning": None,
        "conviction_eligible": False,
        "card_lines": [
            "Hold: 5 days",
            "Bull-only in BULL regime",
            "Midday signals are 10x better than afternoon on GLD",
        ],
        "never_lines": ["Take signals in TRANSITION or BEAR regime"],
    },
    "MSFT": {
        "tier": "core",
        "best_bias": "bear",
        "optimal_hold": 5,
        "regime_note": "Bear-only in all regimes",
        "wr_3d": 59.9, "wr_5d": 54.9, "pf_5d": 1.89, "n": 175,
        "ev_5d": 0.783,
        "htf_note": "CONFIRMED only (100% of signals)",
        "phase_note": "Both midday and afternoon work",
        "hold_warning": None,
        "conviction_eligible": False,
        "card_lines": [
            "Hold: 3-5 days (3d WR 59.9%, 5d WR 54.9%)",
            "Bear-only — bull signals on MSFT lose in all regimes",
            "BULL regime bear 3d: PF 1.92 — counter-trend edge",
        ],
        "never_lines": ["Take bull signals on MSFT"],
    },

    # Tier 2: Strong — real edge but more conditional
    "SPY": {
        "tier": "strong",
        "best_bias": "bull",
        "optimal_hold": 5,
        "regime_note": "Bull-side only at 3-5d. Bear decays past EOD.",
        "wr_3d": 53.5, "wr_5d": 57.3, "pf_5d": 1.35, "n": 398,
        "ev_5d": 0.214,
        "htf_note": "CONVERGING 3d: PF 2.06 — better than CONFIRMED",
        "phase_note": "Midday EOD: 73% WR — strongest phase",
        "hold_warning": "Bear signals negative by 3d — EOD only if bear",
        "conviction_eligible": True, "conviction_wr_5d": 60.0, "conviction_ev_5d": 0.92,
        "card_lines": [
            "Hold: 3-5 days for bull, EOD-only for bear",
            "TRANSITION regime SPY 1d: 59.9% WR, PF 1.70",
            "CONVERGING HTF is premium on SPY (PF 2.06)",
        ],
        "never_lines": ["Hold bear signals past EOD — WR drops to 38% at 3d"],
    },
    "TSLA": {
        "tier": "strong",
        "best_bias": "any",
        "optimal_hold": 5,
        "regime_note": "Both bull and bear work — ticker-specific rules",
        "wr_3d": 50.4, "wr_5d": 57.1, "pf_5d": 1.45, "n": 343,
        "ev_5d": 0.788,
        "htf_note": "CONFIRMED is the edge — CONVERGING is poor on TSLA",
        "phase_note": "Midday 3d: 59.5% WR — better than afternoon",
        "hold_warning": "Must hold to 5d — 3d WR is near breakeven",
        "conviction_eligible": False,  # TSLA conviction is anti-signal
        "card_lines": [
            "Hold: 5 days — WR improves 50%→57% from 3d to 5d",
            "Bear signals in BULL regime: 69.7% WR at 5d, +1.04%",
            "CONFIRMED HTF only — CONVERGING is poor on TSLA",
        ],
        "never_lines": [
            "Exit at 3d — WR is near breakeven, must hold to 5d",
            "Trade CONVERGING signals on TSLA",
            "Trade conviction/volume-burst on TSLA — anti-signal",
        ],
    },
    "AMD": {
        "tier": "strong",
        "best_bias": "bull",
        "optimal_hold": 5,
        "regime_note": "Bull-only. TRANSITION CONVERGING is premium.",
        "wr_3d": 48.0, "wr_5d": 49.3, "pf_5d": 1.42, "n": 278,
        "ev_5d": 1.151,
        "htf_note": "CONVERGING 3d: 57.8% WR in TRANSITION",
        "phase_note": None,
        "hold_warning": None,
        "conviction_eligible": True, "conviction_wr_5d": 66.7, "conviction_ev_5d": 3.72,
        "card_lines": [
            "Hold: 5 days — bull BULL regime 5d: +1.27%",
            "Conviction signals are strong: 66.7% WR, +3.72% at 5d",
        ],
        "never_lines": ["Take bear signals on AMD"],
    },

    # Tier 3: Conditional — edge exists only in specific conditions
    "NVDA": {
        "tier": "conditional",
        "best_bias": "bull",
        "optimal_hold": 5,
        "regime_note": "TRANSITION-only — BULL regime is negative",
        "wr_3d": 46.4, "wr_5d": 45.9, "pf_5d": 0.78, "n": 432,
        "ev_5d": -0.483,
        "htf_note": "CONVERGING 1d: 63.6% WR, PF 2.23 — best HTF",
        "phase_note": None,
        "hold_warning": "BULL regime 5d: -0.78% — do NOT hold in BULL",
        "conviction_eligible": False,
        "card_lines": [
            "TRANSITION only — BULL regime is negative EV",
            "CONVERGING HTF is premium (1d: 63.6% WR)",
            "BULL regime 5d: -0.78% PF 0.68 — avoid",
        ],
        "never_lines": [
            "Hold NVDA in BULL regime past EOD",
            "Trade CONFIRMED signals in TRANSITION — use CONVERGING only",
        ],
    },
    "AVGO": {
        "tier": "conditional",
        "best_bias": "bull",
        "optimal_hold": 5,
        "regime_note": "TRANSITION-only — BULL regime is strongly negative",
        "wr_3d": 35.3, "wr_5d": 47.5, "pf_5d": 0.84, "n": 387,
        "ev_5d": -0.481,
        "htf_note": "CONVERGING 3d: +2.27% PF 2.60 — only edge",
        "phase_note": "Midday 1d: PF 1.84 — avoid afternoon",
        "hold_warning": "BULL regime 3d: -1.03% PF 0.59 — AVOID",
        "conviction_eligible": True, "conviction_wr_5d": 73.3, "conviction_ev_5d": 4.28,
        "card_lines": [
            "⚠️ TRANSITION-only — BULL regime 5d: -1.0% PF 0.68",
            "TRANSITION 5d: +5.67% PF 4.11 — massive edge",
            "CONVERGING HTF required in TRANSITION",
            "Conviction: 73.3% WR, +4.28% — strongest conviction ticker",
        ],
        "never_lines": [
            "Trade AVGO in BULL regime — negative EV at all horizons",
            "Use CONFIRMED HTF in TRANSITION — CONVERGING only",
        ],
    },
    "PLTR": {
        "tier": "conditional",
        "best_bias": "bull",
        "optimal_hold": 3,
        "regime_note": "TRANSITION CONVERGING only",
        "wr_3d": 54.2, "wr_5d": 47.7, "pf_5d": 0.93, "n": 310,
        "ev_5d": -0.182,
        "htf_note": "CONVERGING 3d: 82.4% WR, PF 6.24 — elite signal",
        "phase_note": "Midday 1d: 71.1% WR, PF 4.59",
        "hold_warning": "BULL regime is near-flat. CONVERGING 3d is the entire edge.",
        "conviction_eligible": False,  # PLTR conviction is anti-signal
        "card_lines": [
            "Hold: 3 days — CONVERGING 3d is 82.4% WR, PF 6.24",
            "TRANSITION CONVERGING only — everything else is flat/negative",
            "Midday: 71.1% WR at 1d — afternoon drops to 45%",
        ],
        "never_lines": [
            "Hold past 3d — edge fades",
            "Trade in BULL regime — near flat",
            "Trade conviction/volume-burst on PLTR — anti-signal",
            "Trade afternoon signals — midday only",
        ],
    },
    "COIN": {
        "tier": "conditional",
        "best_bias": "bear",
        "optimal_hold": 1,
        "regime_note": "Bear-only. Bull side is negative.",
        "wr_3d": 52.8, "wr_5d": 56.0, "pf_5d": 1.13, "n": 359,
        "ev_5d": 0.403,
        "htf_note": None,
        "phase_note": None,
        "hold_warning": "COIN bear edge peaks at 1d (+6.03%) — decays by 5d",
        "conviction_eligible": True, "conviction_wr_5d": 76.5, "conviction_ev_5d": 4.35,
        "card_lines": [
            "Bear-only — bull 5d: -1.31%",
            "Bear edge peaks at 1d (+6.03%) — consider short hold",
            "Conviction: 76.5% WR, +4.35% — second-best conviction ticker",
        ],
        "never_lines": [
            "Take bull signals on COIN — negative EV",
            "Hold bear past 1-3d without conviction confirmation",
        ],
    },

    # Tier 4: Marginal or shadow
    "AAPL": {
        "tier": "marginal",
        "best_bias": "bull",
        "optimal_hold": 5,
        "regime_note": "Marginal edge. CONVERGING is best HTF.",
        "wr_3d": 50.3, "wr_5d": 52.4, "pf_5d": 1.15, "n": 534,
        "ev_5d": 0.182,
        "htf_note": "CONVERGING 1d: 69.2% WR, PF 2.35",
        "phase_note": "Midday 3d: 60.6% WR — afternoon is breakeven",
        "hold_warning": "Bear side is negative at all horizons — bull only",
        "conviction_eligible": False,
        "card_lines": [
            "Marginal edge — CONVERGING is premium (1d: 69.2% WR)",
            "Midday only — afternoon 3d is breakeven",
            "Bull only — bear is negative at all horizons",
        ],
        "never_lines": ["Take bear signals on AAPL", "Afternoon entries unless CONVERGING"],
    },
    "IWM": {
        "tier": "marginal",
        "best_bias": "bull",
        "optimal_hold": 5,
        "regime_note": "Bull-only. Bear drops to 30.7% WR at 3d.",
        "wr_3d": 47.6, "wr_5d": 55.9, "pf_5d": 1.07, "n": 315,
        "ev_5d": 0.062,
        "htf_note": "CONFIRMED only — CONVERGING is 0% WR at 3d on IWM",
        "phase_note": None,
        "hold_warning": "Bull 5d: 63.6% WR PF 2.0. Bear 5d: 39.6% — never hold bear.",
        "conviction_eligible": True, "conviction_wr_5d": 81.2, "conviction_ev_5d": 0.93,
        "card_lines": [
            "Bull-only at 5d: 63.6% WR, PF 2.0",
            "Bear side is 39.6% WR at 5d — hard avoid",
            "CONVERGING is bad on IWM — CONFIRMED only",
            "Conviction: 81.2% WR — strong confirmation signal",
        ],
        "never_lines": [
            "Take bear signals on IWM — 39.6% WR at 5d",
            "Trade CONVERGING HTF on IWM",
        ],
    },

    # Tier 5: Remove — no edge
    "META": {
        "tier": "remove",
        "best_bias": None,
        "optimal_hold": None,
        "regime_note": "REMOVED — 34.9% WR at 5d, -1.52% EV, PF 0.40",
        "wr_3d": 35.1, "wr_5d": 34.9, "pf_5d": 0.40, "n": 238,
        "ev_5d": -1.519,
        "card_lines": ["⛔ META is REMOVED — no tradeable edge in any condition"],
        "never_lines": ["Trade META on the active scanner"],
    },
    "NFLX": {
        "tier": "remove",
        "best_bias": None,
        "optimal_hold": None,
        "regime_note": "REMOVED — 41.3% WR at 5d, -1.03% EV, PF 0.45",
        "wr_3d": 36.2, "wr_5d": 41.3, "pf_5d": 0.45, "n": 92,
        "ev_5d": -1.032,
        "card_lines": ["⛔ NFLX is REMOVED — no tradeable edge in any condition"],
        "never_lines": ["Trade NFLX on the active scanner"],
    },
    "QQQ": {
        "tier": "remove",
        "best_bias": None,
        "optimal_hold": None,
        "regime_note": "DOWNGRADED — 50% WR at 5d, -0.40% EV, PF 0.64",
        "wr_3d": 40.5, "wr_5d": 50.0, "pf_5d": 0.64, "n": 320,
        "ev_5d": -0.404,
        "hold_warning": "Negative EV from 1d through 5d even after regime filtering",
        "card_lines": [
            "⚠️ QQQ DOWNGRADED — negative EV at all hold periods",
            "EOD-only if at all — Midday EOD: 73.2% WR",
        ],
        "never_lines": ["Hold QQQ signals overnight — edge inverts"],
    },
}

# ── Swing Guidance ───────────────────────────────────────────────
SWING_GUIDANCE = {
    "trail": {
        "min_profit_pct": 0.5,
        "giveback_pct": 0.40,
        "note": "0.5% underlying move activates trail, keep 60% of peak",
        "backtest": "79.6% WR, +1.94% avg, PF 2.71 (vs current 49.3%, PF 1.24)",
    },
    "target_note": (
        "Target1 (1.272 ext) reached only 27% of the time. "
        "Trail stop is the primary exit — target is informational only."
    ),
    "hold_by_fib": {
        "50.0": {"optimal_hold": 25, "note": "50% fib bulls are standout — +7.97% at 20D, +11.12% at 30D"},
        "78.6": {"optimal_hold": 20, "note": "78.6% best in 10-20 day window"},
        "38.2": {"optimal_hold": 20, "note": "Selective, best around 20 days"},
        "61.8": {"optimal_hold": 15, "note": "Weakest and most inconsistent fib level"},
    },
    "direction_rules": {
        "bull": "21-30 day leash for best bull setups (50% fib + weekly bull + conf 70+)",
        "bear": "Tactical 1-5 day trades only — do NOT extend bear swings",
    },
    "remove_tickers": ["AAPL", "QQQ", "KR", "HD", "C", "UNH"],
    "best_tickers": ["BAC", "SLV", "GLD", "PEP", "PFE", "JPM", "NVDA"],
}

# ── Income Guidance ──────────────────────────────────────────────
INCOME_GUIDANCE = {
    "min_cushion_pct": 6.0,
    "cushion_note": "Cushion <4% WR is 22-65%. Cushion 6-8% is 65-97%. Cushion 8%+ is 91-100%.",
    "prefer_bull_puts": True,
    "bull_put_avg_wr": 87.1,
    "bear_call_avg_wr": 71.2,
    "skip_bear_calls": ["IWM", "NVDA", "AVGO", "SPY", "AMD", "GOOGL"],
    "keep_bear_calls": ["MSFT"],  # 91.8% WR
    "best_tickers": ["SPY", "MSFT", "QQQ", "GOOGL"],
    "regime_warning": "TRANSITION regime degrades income WR by 10-25% on volatile tickers",
}

# ── EM Guidance ──────────────────────────────────────────────────
EM_GUIDANCE = {
    "condor_wr": 69.2,
    "sigma_1_containment": 85.4,
    "sigma_2_containment": 98.1,
    "avg_move_ratio": 0.575,
    "best_condor_bias": [-1, 0, 1],  # Neutral bias days
    "neutral_condor_wr": 72.0,
    "tight_075_sigma_wr": 73.4,
    "note": "Sell condors at 0.75-1σ on neutral-bias days (bias -1 to +1)",
}

# ── Conviction Guidance ──────────────────────────────────────────
CONVICTION_GUIDANCE = {
    "top_tickers": ["AMZN", "COIN", "AVGO", "AMD", "GOOGL", "IWM"],
    "anti_signal_tickers": ["TSLA", "PLTR", "META"],
    "hold": "3-5 days",
    "trigger": "score ≥ 60 + volume burst 2x+",
    "best": {
        "AMZN": {"wr_5d": 90.9, "ev_5d": 3.835, "n": 12},
        "COIN": {"wr_5d": 76.5, "ev_5d": 4.345, "n": 17},
        "AVGO": {"wr_5d": 73.3, "ev_5d": 4.282, "n": 16},
        "AMD":  {"wr_5d": 66.7, "ev_5d": 3.718, "n": 20},
        "GOOGL":{"wr_5d": 66.7, "ev_5d": 2.490, "n": 16},
        "IWM":  {"wr_5d": 81.2, "ev_5d": 0.927, "n": 16},
    },
}


# ═══════════════════════════════════════════════════════════════════
# FORMATTING FUNCTIONS — generate card blocks from guidance data
# ═══════════════════════════════════════════════════════════════════

def format_active_guidance_block(ticker: str, bias: str, regime: str) -> str:
    """Generate the 📊 BACKTEST GUIDANCE block for active scanner trade cards."""
    g = ACTIVE_GUIDANCE.get(ticker.upper())
    if not g:
        return ""

    lines = ["", "📊 BACKTEST GUIDANCE (v7 backtest, corrected rerun)"]

    # Tier indicator
    tier_emoji = {"core": "🟢", "strong": "🟡", "conditional": "🟠",
                  "marginal": "⚪", "remove": "🔴"}.get(g["tier"], "⚪")
    lines.append(f"  Tier: {tier_emoji} {g['tier'].upper()} | n={g.get('n', '?')} trades")

    # Card lines
    for line in g.get("card_lines", []):
        lines.append(f"  • {line}")

    # Hold guidance
    oh = g.get("optimal_hold")
    if oh:
        lines.append(f"  📅 Optimal hold: {oh} days")

    # Regime warning
    hw = g.get("hold_warning")
    if hw:
        lines.append(f"  ⚠️ {hw}")

    # Conviction
    if g.get("conviction_eligible") and g.get("conviction_wr_5d"):
        lines.append(f"  ⭐ Conviction eligible: {g['conviction_wr_5d']:.0f}% WR, "
                      f"+{g['conviction_ev_5d']:.2f}% at 5d")

    # Never
    for line in g.get("never_lines", []):
        lines.append(f"  🚫 {line}")

    return "\n".join(lines)


def format_swing_guidance_block(ticker: str, direction: str, fib_level: str) -> str:
    """Generate the 📊 SWING GUIDANCE block for swing trade cards."""
    lines = ["", "📊 SWING GUIDANCE (v7 backtest)"]

    # Trail stop guidance
    t = SWING_GUIDANCE["trail"]
    lines.append(f"  Trail: activate after {t['min_profit_pct']}% move, "
                  f"{int(t['giveback_pct']*100)}% giveback (keep {int((1-t['giveback_pct'])*100)}%)")
    lines.append(f"  Backtest: {t['backtest']}")

    # Target note
    lines.append(f"  ℹ️ {SWING_GUIDANCE['target_note']}")

    # Fib-specific hold
    fib_data = SWING_GUIDANCE["hold_by_fib"].get(str(fib_level))
    if fib_data:
        lines.append(f"  📅 Fib {fib_level}: {fib_data['note']}")
        lines.append(f"     Optimal hold: {fib_data['optimal_hold']} trading days")

    # Direction rule
    dir_rule = SWING_GUIDANCE["direction_rules"].get(direction)
    if dir_rule:
        lines.append(f"  {'🐂' if direction == 'bull' else '🐻'} {dir_rule}")

    # Ticker warning
    if ticker.upper() in SWING_GUIDANCE["remove_tickers"]:
        lines.append(f"  ⚠️ {ticker} is on the swing REMOVE list — poor backtest performance")
    elif ticker.upper() in SWING_GUIDANCE["best_tickers"]:
        lines.append(f"  ✅ {ticker} is a top swing ticker")

    return "\n".join(lines)


def format_income_guidance_block(ticker: str, trade_type: str, cushion_pct: float) -> str:
    """Generate guidance block for income trade cards."""
    g = INCOME_GUIDANCE
    lines = ["", "📊 INCOME GUIDANCE (v7 backtest)"]

    if cushion_pct < g["min_cushion_pct"]:
        lines.append(f"  🔴 CUSHION {cushion_pct:.1f}% is below {g['min_cushion_pct']}% minimum — PASS")
        lines.append(f"  {g['cushion_note']}")
        return "\n".join(lines)

    if trade_type == "bear_call" and ticker.upper() in g["skip_bear_calls"]:
        lines.append(f"  ⚠️ Bear calls on {ticker} average {g['bear_call_avg_wr']:.0f}% WR — prefer bull puts")

    if ticker.upper() in g["best_tickers"]:
        lines.append(f"  ✅ {ticker} is a top income ticker")

    lines.append(f"  Cushion {cushion_pct:.1f}%: "
                  + ("🟢 strong" if cushion_pct >= 8 else "🟡 acceptable" if cushion_pct >= 6 else "🔴 weak"))

    return "\n".join(lines)


def format_conviction_guidance_block(ticker: str) -> str:
    """Generate guidance block for conviction/flow signals."""
    g = CONVICTION_GUIDANCE
    lines = []

    if ticker.upper() in g["anti_signal_tickers"]:
        lines.append(f"  🔴 {ticker} conviction is ANTI-SIGNAL — do NOT trade volume bursts")
        return "\n".join(lines)

    best = g["best"].get(ticker.upper())
    if best:
        lines.append(f"  ⭐ CONVICTION: {best['wr_5d']:.0f}% WR, +{best['ev_5d']:.2f}% at 5d (n={best['n']})")
        lines.append(f"  Hold {g['hold']} — conviction signals need time to play out")

    return "\n".join(lines)


def should_filter_signal(ticker: str, bias: str, regime: str) -> tuple:
    """Check if a signal should be filtered based on v7 backtest guidance.
    Returns (should_filter: bool, reason: str, filter_category: str)."""
    g = ACTIVE_GUIDANCE.get(ticker.upper())
    if not g:
        return False, "", ""

    # Tier: remove
    if g["tier"] == "remove":
        return True, f"{ticker} removed — {g.get('regime_note', 'no edge')}", "ticker_removed"

    # Direction filter
    bb = g.get("best_bias")
    if bb and bb != "any" and bb != bias:
        return True, f"{ticker} is {bb}-only (v7 backtest)", "direction_blocked"

    # Regime filter for conditional tickers
    if g["tier"] == "conditional":
        if ticker.upper() in ("NVDA", "AVGO", "PLTR") and regime == "BULL":
            return True, f"{ticker} TRANSITION-only — BULL regime is negative EV", "regime_blocked"

    return False, "", ""
