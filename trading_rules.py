# trading_rules.py
# Brad's Trading Rules — encoded from conversation
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# v3.5 additions:
#   - Portfolio-level risk limits (daily loss, gross exposure, concentration)
#   - Market regime detection thresholds (VIX, ADX)
#   - Trade journal settings
#   - Greeks P/L attribution settings

# ─────────────────────────────────────────────────────────
# DIRECTION & SIGNAL
# ─────────────────────────────────────────────────────────
ALLOWED_DIRECTIONS       = ["bull"]
SIGNAL_SOURCE            = "unified_pine"
REQUIRE_TIER             = ["1", "2"]
TIER1_SIZE_MULTIPLIER    = 1.0
TIER2_SIZE_MULTIPLIER    = 0.75

# ─────────────────────────────────────────────────────────
# SPREAD TYPE & STRUCTURE
# ─────────────────────────────────────────────────────────
SPREAD_TYPE              = "debit"
OPTION_SIDE              = "call"
BOTH_LEGS_ITM            = True
WIDTH_PREFERENCE         = [1.0, 2.50, 5.0]
NO_HALF_DOLLAR_WIDTHS    = True

# ─────────────────────────────────────────────────────────
# COST / QUALITY FILTERS
# ─────────────────────────────────────────────────────────
MAX_COST_PCT_OF_WIDTH    = 0.70
MIN_COST_PCT_OF_WIDTH    = 0.20
MAX_RISK_PER_TRADE_USD   = 1500.0

# ─────────────────────────────────────────────────────────
# DTE & TIMING
# ─────────────────────────────────────────────────────────
MIN_DTE                  = 0
MAX_DTE                  = 10
TARGET_DTE               = 3
MAX_EXPIRATIONS_TO_PULL  = 4
NO_ENTRY_FIRST_MINUTES   = 15

# ─────────────────────────────────────────────────────────
# EXIT RULES
# ─────────────────────────────────────────────────────────
SAME_DAY_EXIT_PCT        = 0.30
NEXT_DAY_EXIT_PCT        = 0.35
EXTENDED_HOLD_EXIT_PCT   = 0.50

STOP_LOSS_PCT            = 0.40
HIGH_VOLUME_TICKERS      = [
    "SPY", "QQQ", "GLD", "IWM", "DIA",
    "AAPL", "MSFT", "NVDA", "AMZN", "META",
    "TSLA", "GOOGL", "AMD", "SPX",
]
USE_STOP_LOSS_ALL        = False

# ─────────────────────────────────────────────────────────
# DEAL-BREAKERS
# ─────────────────────────────────────────────────────────
NO_EARNINGS_WEEK         = True
NO_DIVIDEND_IN_DTE       = True
MIN_OPEN_INTEREST        = 50
MAX_BID_ASK_SPREAD       = 0.50
MIN_VOLUME_LEG           = 0

# ─────────────────────────────────────────────────────────
# POSITION SIZING
# ─────────────────────────────────────────────────────────
MAX_CONTRACTS            = 50
ACCOUNT_SIZE             = 100_000.0
MAX_RISK_PCT_ACCOUNT     = 0.02

# ─────────────────────────────────────────────────────────
# WEBHOOK CONFIDENCE MAPPING
# ─────────────────────────────────────────────────────────
CONFIDENCE_BOOSTS = {
    "tier_1":            15,
    "tier_2":            5,
    "htf_confirmed":     10,
    "htf_converging":    5,
    "daily_bull":        10,
    "rsi_mfi_bull":      5,
    "above_vwap":        5,
    "wave_oversold":     10,
    "iv_edge":           8,
    "rv_edge":           10,
    "within_em":         5,
    "regime_trending":   8,     # v3.5: trending regime boost
    "regime_low_vix":    5,     # v3.5: calm market boost
}

CONFIDENCE_PENALTIES = {
    "htf_diverging":     -20,
    "daily_bear":        -10,
    "wave_overbought":   -15,
    "low_oi":            -5,
    "wide_spread":       -5,
    "earnings_week":     -100,
    "dividend_in_dte":   -100,
    "iv_crushed":        -5,
    "beyond_em":         -8,
    "regime_choppy":     -10,   # v3.5: choppy / range-bound penalty
    "regime_high_vix":   -8,    # v3.5: elevated VIX penalty
    "regime_crisis":     -25,   # v3.5: VIX > 35 = crisis mode
}

MIN_CONFIDENCE_TO_TRADE  = 40

# ─────────────────────────────────────────────────────────
# EXPECTED MOVE & IV vs RV EDGE (v3.4)
# ─────────────────────────────────────────────────────────
EM_DISPLAY_ON_CARD       = True
IV_RV_DISPLAY_ON_CARD    = True
IV_RANK_LOW              = 20
IV_RANK_HIGH             = 70
RV_LOOKBACK_DAYS         = 20
RV_ANNUALIZE_FACTOR      = 252
IV_RV_BUYER_EDGE_PCT     = -5.0
IV_RV_SELLER_EDGE_PCT    = 5.0


# ═══════════════════════════════════════════════════════════
# PORTFOLIO-LEVEL RISK LIMITS (v3.5)
# ═══════════════════════════════════════════════════════════
#
# Hard limits enforced by the risk manager.
# If any limit is breached, new trades are blocked.

# Daily loss limit: realized + unrealized for today
DAILY_LOSS_LIMIT_USD     = 3000.0             # Max daily drawdown before auto-pause
DAILY_LOSS_LIMIT_PCT     = 0.03               # 3% of account — whichever hits first

# Gross exposure: total open risk across all spreads
MAX_GROSS_EXPOSURE_USD   = 10000.0            # Max $10k total open risk
MAX_GROSS_EXPOSURE_PCT   = 0.10               # 10% of account

# Concentration: max risk on a single underlying ticker
MAX_TICKER_EXPOSURE_USD  = 3000.0             # Max $3k on any one ticker
MAX_TICKER_EXPOSURE_PCT  = 0.03               # 3% of account on one ticker

# Max concurrent open spreads
MAX_OPEN_SPREADS         = 8

# Sector correlation (soft limit — warns but doesn't block)
MAX_SAME_SECTOR_SPREADS  = 4

SECTOR_MAP = {
    "AAPL":  "tech", "MSFT":  "tech", "GOOGL": "tech", "META":  "tech",
    "AMZN":  "tech", "NVDA":  "tech", "AMD":   "tech", "TSLA":  "auto",
    "SPY":   "index", "QQQ":  "index", "IWM":  "index", "DIA":  "index",
    "SPX":   "index", "GLD":  "commodity",
}

# Portfolio-level Greeks caps (v3.6)
# These are SOFT limits (warnings, not blocks).
# Net delta/gamma/vega across all open spreads × contracts × 100.
MAX_PORTFOLIO_DELTA      = 300                # Max ±300 net delta
MAX_PORTFOLIO_GAMMA      = 50                 # Max ±50 net gamma
MAX_PORTFOLIO_VEGA       = 150                # Max ±150 net vega


# ═══════════════════════════════════════════════════════════
# MARKET REGIME DETECTION (v3.5)
# ═══════════════════════════════════════════════════════════
#
# VIX thresholds:
#   < 15:   LOW — calm, trend-friendly
#   15-25:  NORMAL — standard conditions
#   25-35:  ELEVATED — increase caution
#   > 35:   CRISIS — consider pausing
#
# ADX (14-period daily):
#   < 20:   CHOPPY — range-bound, whipsaw risk
#   20-30:  MODERATE — trend forming
#   > 30:   TRENDING — strong trend

REGIME_VIX_LOW           = 15.0
REGIME_VIX_NORMAL        = 25.0
REGIME_VIX_ELEVATED      = 35.0

REGIME_ADX_CHOPPY        = 20.0
REGIME_ADX_TRENDING      = 30.0

REGIME_CRISIS_BLOCK      = True               # Block entries when VIX > 35
REGIME_ELEVATED_SIZE_MULT = 0.50              # Halve size when VIX elevated + choppy
REGIME_CHOPPY_SIZE_MULT  = 0.75               # 75% size in choppy regime
REGIME_TRENDING_SIZE_MULT = 1.0               # Full size in trending regime


# ═══════════════════════════════════════════════════════════
# TRADE JOURNAL SETTINGS (v3.5)
# ═══════════════════════════════════════════════════════════
#
# Two types of entries:
#   SIGNAL: logged when Pine fires a webhook (even if no trade)
#   TRADE:  logged when a spread is opened/closed, with full context

JOURNAL_LOG_ALL_SIGNALS  = True               # Log every TV webhook signal
JOURNAL_LOG_REJECTED     = True               # Log signals that didn't produce a trade
JOURNAL_MAX_ENTRIES      = 5000               # Rolling cap (oldest pruned)

GREEKS_ATTRIBUTION_ON_CLOSE = True            # Estimate delta/theta/vega P/L attribution
