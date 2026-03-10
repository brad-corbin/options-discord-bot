# trading_rules.py
# Brad's Trading Rules — encoded from conversation
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# v3.5 additions:
#   - Portfolio-level risk limits (daily loss, gross exposure, concentration)
#   - Market regime detection thresholds (VIX, ADX)
#   - Trade journal settings
#   - Greeks P/L attribution settings
#
# v3.6 additions:
#   - Bear direction enabled (bull + bear)

# ─────────────────────────────────────────────────────────
# DIRECTION & SIGNAL
# ─────────────────────────────────────────────────────────
ALLOWED_DIRECTIONS       = ["bull", "bear"]   # v3.6: bear enabled
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
    "daily_bear":        10,   # v3.6: daily bear confirms bear trades
    "rsi_mfi_bull":      5,
    "rsi_mfi_bear":      5,    # v3.6: RSI/MFI selling confirms bear trades
    "above_vwap":        5,
    "below_vwap":        5,    # v3.6: below VWAP confirms bear trades
    "wave_oversold":     10,
    "wave_overbought":   10,   # v3.6: overbought confirms bear trades
    "iv_edge":           8,
    "rv_edge":           10,
    "within_em":         5,
    "regime_trending":   8,
    "regime_low_vix":    5,
}

CONFIDENCE_PENALTIES = {
    "htf_diverging":     -20,
    "daily_bear":        -10,  # penalty when bull trade, daily is bear
    "daily_bull":        -10,  # v3.6: penalty when bear trade, daily is bull
    "wave_overbought":   -15,  # penalty when bull trade, wave overbought
    "wave_oversold":     -15,  # v3.6: penalty when bear trade, wave oversold
    "low_oi":            -5,
    "wide_spread":       -5,
    "earnings_week":     -100,
    "dividend_in_dte":   -100,
    "iv_crushed":        -5,
    "beyond_em":         -8,
    "regime_choppy":     -10,
    "regime_high_vix":   -8,
    "regime_crisis":     -25,
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

DAILY_LOSS_LIMIT_USD     = 3000.0
DAILY_LOSS_LIMIT_PCT     = 0.03

MAX_GROSS_EXPOSURE_USD   = 10000.0
MAX_GROSS_EXPOSURE_PCT   = 0.10

MAX_TICKER_EXPOSURE_USD  = 3000.0
MAX_TICKER_EXPOSURE_PCT  = 0.03

MAX_OPEN_SPREADS         = 8

MAX_SAME_SECTOR_SPREADS  = 4

SECTOR_MAP = {
    "AAPL":  "tech", "MSFT":  "tech", "GOOGL": "tech", "META":  "tech",
    "AMZN":  "tech", "NVDA":  "tech", "AMD":   "tech", "TSLA":  "auto",
    "SPY":   "index", "QQQ":  "index", "IWM":  "index", "DIA":  "index",
    "SPX":   "index", "GLD":  "commodity",
}

MAX_PORTFOLIO_DELTA      = 300
MAX_PORTFOLIO_GAMMA      = 50
MAX_PORTFOLIO_VEGA       = 150


# ═══════════════════════════════════════════════════════════
# MARKET REGIME DETECTION (v3.5)
# ═══════════════════════════════════════════════════════════

REGIME_VIX_LOW           = 15.0
REGIME_VIX_NORMAL        = 25.0
REGIME_VIX_ELEVATED      = 35.0

REGIME_ADX_CHOPPY        = 20.0
REGIME_ADX_TRENDING      = 30.0

REGIME_CRISIS_BLOCK      = True
REGIME_ELEVATED_SIZE_MULT = 0.50
REGIME_CHOPPY_SIZE_MULT  = 0.75
REGIME_TRENDING_SIZE_MULT = 1.0


# ═══════════════════════════════════════════════════════════
# TRADE JOURNAL SETTINGS (v3.5)
# ═══════════════════════════════════════════════════════════

JOURNAL_LOG_ALL_SIGNALS  = True
JOURNAL_LOG_REJECTED     = True
JOURNAL_MAX_ENTRIES      = 5000

GREEKS_ATTRIBUTION_ON_CLOSE = True
