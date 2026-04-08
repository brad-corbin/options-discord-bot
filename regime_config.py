# regime_config.py
# ═══════════════════════════════════════════════════════════════════
# 3-Layer Regime System — Configuration & Feature Flags 
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Architecture:
#   Layer 1: Core regime (5 states)      — market structure
#   Layer 2: Event overlay               — temporary overrides
#   Layer 3: Sector overlay              — leadership context
#
# Activation strategy:
#   Build together. Deploy in layers.
#   Use feature flags to control what is live vs log-only.
#
# v3.0 — April 8, 2026
# ═══════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════
# FEATURE FLAGS — Controls what is live vs log-only
# ═══════════════════════════════════════════════════════════
#
# Release 1: Core v2 live, overlays log-only
# Release 2: Turn on event overlays
# Release 3: Turn on sector overlays

ENABLE_CORE_REGIME_V2    = True     # 5-regime model replaces 3-regime
ENABLE_EVENT_OVERLAYS    = False    # Event overlays modify live rules
ENABLE_SECTOR_OVERLAYS   = False    # Sector overlays modify live rules
LOG_ONLY_EVENT_OVERLAYS  = True     # Compute + log events even when disabled
LOG_ONLY_SECTOR_OVERLAYS = True     # Compute + log sectors even when disabled


# ═══════════════════════════════════════════════════════════
# LAYER 1 — Core Regime Labels
# ═══════════════════════════════════════════════════════════

BULL_BASE        = "BULL_BASE"
BULL_TRANSITION  = "BULL_TRANSITION"
CHOP             = "CHOP"
BEAR_TRANSITION  = "BEAR_TRANSITION"
BEAR_CRISIS      = "BEAR_CRISIS"

ALL_CORE_REGIMES = [BULL_BASE, BULL_TRANSITION, CHOP, BEAR_TRANSITION, BEAR_CRISIS]

# Score thresholds for classification
# Core score ranges from roughly -10 to +10
REGIME_SCORE_THRESHOLDS = {
    BULL_BASE:       6,    # score >= 6
    BULL_TRANSITION: 2,    # score 2 to 5
    CHOP:           -1,    # score -1 to 1
    BEAR_TRANSITION: -5,   # score -2 to -5
    BEAR_CRISIS:    -99,   # score <= -6
}

# Hysteresis: require N consecutive days at new regime before switching
REGIME_HYSTERESIS_DAYS = 2

# V1 backwards compatibility mapping
# When the scanner or ticker_rules needs the old 3-label regime
V2_TO_V1 = {
    BULL_BASE:       "BULL",
    BULL_TRANSITION: "TRANSITION",
    CHOP:            "TRANSITION",   # conservative: treat as transition
    BEAR_TRANSITION: "BEAR",         # conservative: treat as bear
    BEAR_CRISIS:     "BEAR",
}


# ═══════════════════════════════════════════════════════════
# LAYER 2 — Event Overlay Labels
# ═══════════════════════════════════════════════════════════

EVENT_NONE           = "NONE"
EVENT_MACRO_SHOCK    = "MACRO_SHOCK"        # tariff, CPI, FOMC surprise
EVENT_WAR_CRISIS     = "WAR_CRISIS"         # geopolitical escalation
EVENT_LIQUIDITY      = "LIQUIDITY_EVENT"    # flash crash, liquidity gap
EVENT_MEAN_REVERSION = "MEAN_REVERSION_DAY" # extreme move then reversal

# VIX thresholds for automatic event detection
VIX_SHOCK_THRESHOLD     = 25.0   # VIX absolute level → MACRO_SHOCK
VIX_SPIKE_PCT           = 25.0   # VIX jumps 25%+ in one day → MACRO_SHOCK
VIX_CRISIS_THRESHOLD    = 35.0   # VIX above 35 → WAR_CRISIS eligible
VIX_CRISIS_DAYS         = 2      # VIX above crisis level for N days

# How long event overlays persist (sessions)
EVENT_DECAY_SESSIONS = {
    EVENT_MACRO_SHOCK:    3,
    EVENT_WAR_CRISIS:     5,
    EVENT_LIQUIDITY:      2,
    EVENT_MEAN_REVERSION: 1,
}


# ═══════════════════════════════════════════════════════════
# LAYER 3 — Sector Overlay Labels
# ═══════════════════════════════════════════════════════════

SECTOR_AI_STRONG          = "AI_STRONG"
SECTOR_AI_WEAK            = "AI_WEAK"
SECTOR_COMMODITIES_STRONG = "COMMODITIES_STRONG"
SECTOR_DEFENSIVES_STRONG  = "DEFENSIVES_STRONG"
SECTOR_BREADTH_WEAK       = "INDEX_BREADTH_WEAK"
SECTOR_BREADTH_HEALTHY    = "BREADTH_HEALTHY"

# ETFs used for sector relative strength (5D return vs SPY)
SECTOR_ETFS = {
    "tech":        ["XLK", "SMH"],    # AI / semiconductor leadership
    "commodity":   ["XLE", "GLD"],     # energy + gold
    "defensive":   ["XLP", "XLU"],     # staples + utilities
    "breadth":     ["IWM"],            # small-cap breadth proxy
    "bonds":       ["TLT"],            # rates / risk-off
}

# Relative strength thresholds (5D return differential vs SPY)
SECTOR_OUTPERFORM_THRESHOLD  = 1.5   # +1.5% vs SPY = outperforming
SECTOR_UNDERPERFORM_THRESHOLD = -1.5  # -1.5% vs SPY = underperforming


# ═══════════════════════════════════════════════════════════
# DATA SOURCES — Tickers fetched daily
# ═══════════════════════════════════════════════════════════

# Core regime inputs (always fetched)
CORE_TICKERS = ["SPY", "QQQ", "IWM"]
VIX_TICKER   = "^VIX"

# Sector overlay inputs (fetched only when sector overlays enabled or logged)
SECTOR_TICKERS = ["XLK", "SMH", "XLE", "XLP", "XLU", "GLD", "TLT"]

# How many daily closes to fetch
DAILY_LOOKBACK = 70


# ═══════════════════════════════════════════════════════════
# REGIME DEFAULTS — Per-regime rule parameters
# ═══════════════════════════════════════════════════════════
#
# These are the base defaults for each regime. Ticker-specific
# overrides in ticker_rules.py take precedence where they exist.

REGIME_DEFAULTS = {

    BULL_BASE: {
        "direction_bias":         "bull",
        "long_min_score":         55,
        "short_min_score":        75,
        "above_vwap_required":    False,   # preferred but not required
        "htf_allowed_long":       ["CONFIRMED", "CONVERGING", "OPPOSING"],
        "htf_allowed_short":      ["CONFIRMED"],
        "rsi_min_long":           40,
        "rsi_max_long":           78,
        "rsi_min_short":          None,
        "rsi_max_short":          None,
        "ema_min_long":           None,    # no minimum
        "ema_max_long":           None,
        "phase_preference":       None,    # all phases valid
        "default_hold_long":      5,       # 3D–5D
        "default_hold_short":     1,       # EOD–1D
        "max_spread_width":       2.50,
    },

    BULL_TRANSITION: {
        "direction_bias":         "bull",
        "long_min_score":         60,
        "short_min_score":        80,
        "above_vwap_required":    True,
        "htf_allowed_long":       ["CONFIRMED", "CONVERGING"],
        "htf_allowed_short":      ["CONFIRMED"],
        "rsi_min_long":           55,
        "rsi_max_long":           75,
        "rsi_min_short":          None,
        "rsi_max_short":          None,
        "ema_min_long":           0.05,
        "ema_max_long":           None,
        "phase_preference":       "AFTERNOON",
        "default_hold_long":      5,       # 3D–5D
        "default_hold_short":     1,       # 1D only
        "max_spread_width":       2.50,
    },

    CHOP: {
        "direction_bias":         "neutral",
        "long_min_score":         70,
        "short_min_score":        70,
        "above_vwap_required":    True,    # both sides
        "htf_allowed_long":       ["CONFIRMED"],
        "htf_allowed_short":      ["CONFIRMED"],
        "rsi_min_long":           50,
        "rsi_max_long":           70,
        "rsi_min_short":          30,
        "rsi_max_short":          50,
        "ema_min_long":           0.05,
        "ema_max_long":           0.50,
        "phase_preference":       None,
        "default_hold_long":      1,       # EOD–1D
        "default_hold_short":     1,       # EOD–1D
        "max_spread_width":       2.50,
    },

    BEAR_TRANSITION: {
        "direction_bias":         "bear",
        "long_min_score":         72,
        "short_min_score":        60,
        "above_vwap_required":    False,
        "htf_allowed_long":       ["CONFIRMED", "CONVERGING"],
        "htf_allowed_short":      ["CONFIRMED", "CONVERGING"],
        "rsi_min_long":           50,
        "rsi_max_long":           70,
        "rsi_min_short":          None,
        "rsi_max_short":          None,
        "ema_min_long":           0.05,
        "ema_max_long":           None,
        "phase_preference":       None,
        "default_hold_long":      1,       # EOD–1D tactical
        "default_hold_short":     3,       # 1D–3D
        "max_spread_width":       2.50,
    },

    BEAR_CRISIS: {
        "direction_bias":         "bear",
        "long_min_score":         80,
        "short_min_score":        55,
        "above_vwap_required":    False,
        "htf_allowed_long":       ["CONVERGING"],  # countertrend only
        "htf_allowed_short":      ["CONFIRMED", "CONVERGING"],
        "rsi_min_long":           None,
        "rsi_max_long":           None,
        "rsi_min_short":          None,
        "rsi_max_short":          None,
        "ema_min_long":           None,
        "ema_max_long":           None,
        "phase_preference":       None,
        "default_hold_long":      1,       # EOD only unless crisis reversal
        "default_hold_short":     5,       # 3D–5D
        "max_spread_width":       2.50,
    },
}


# ═══════════════════════════════════════════════════════════
# EVENT OVERLAY MODIFIERS
# ═══════════════════════════════════════════════════════════
#
# When an event overlay is active AND ENABLE_EVENT_OVERLAYS is True,
# these modifiers are applied ON TOP of the regime defaults.

EVENT_MODIFIERS = {

    EVENT_MACRO_SHOCK: {
        "long_min_score_delta":   +10,  # raise long threshold
        "short_min_score_delta":  +10,  # raise short threshold too
        "hold_duration_factor":   0.5,  # cut hold duration in half
        "suppress_first_break":   True, # prefer reclaim setups
        "notes": "Macro shock active — tighter gates, shorter holds",
    },

    EVENT_WAR_CRISIS: {
        "long_min_score_delta":   +15,
        "short_min_score_delta":  -5,   # shorts get easier
        "hold_duration_factor":   0.5,
        "force_regime_floor":     BEAR_CRISIS,  # override up to BEAR_CRISIS
        "notes": "War/crisis overlay — shorts promoted, longs restricted",
    },

    EVENT_LIQUIDITY: {
        "long_min_score_delta":   +15,
        "short_min_score_delta":  +15,
        "hold_duration_factor":   0.25, # very short holds only
        "notes": "Liquidity event — extreme caution, minimal holds",
    },

    EVENT_MEAN_REVERSION: {
        "long_min_score_delta":   +5,
        "short_min_score_delta":  +5,
        "hold_duration_factor":   0.5,
        "prefer_reversal_setups": True,
        "notes": "Mean reversion day — favor reversal over continuation",
    },
}


# ═══════════════════════════════════════════════════════════
# REGIME PACKAGE — Template for the unified output
# ═══════════════════════════════════════════════════════════

def empty_regime_package() -> dict:
    """Returns a clean regime package template."""
    return {
        # Layer 1: Core
        "core_regime":        BEAR_CRISIS,
        "core_score":         0,
        "core_score_details": {},
        "days_in_regime":     0,
        "regime_confidence":  0.0,

        # Layer 2: Event
        "event_overlay":      EVENT_NONE,
        "event_days_remaining": 0,
        "event_source":       "",

        # Layer 3: Sector
        "sector_overlays":    [],
        "sector_details":     {},

        # Effective output (after overlays applied)
        "effective_regime":   BEAR_CRISIS,
        "v1_regime":          "BEAR",   # backwards-compatible label

        # Feature flag state
        "v2_active":          ENABLE_CORE_REGIME_V2,
        "events_active":      ENABLE_EVENT_OVERLAYS,
        "sectors_active":     ENABLE_SECTOR_OVERLAYS,
    }
