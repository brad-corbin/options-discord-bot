# trading_rules.py
# Brad's Trading Rules — encoded from conversation
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# These rules define the complete trade selection, sizing, and exit logic.
# The options engine reads these to build and validate spreads.

# ─────────────────────────────────────────────────────────
# DIRECTION & SIGNAL
# ─────────────────────────────────────────────────────────
ALLOWED_DIRECTIONS       = ["bull"]           # Bull only for now
SIGNAL_SOURCE            = "unified_pine"     # BUS v1.0 webhook
REQUIRE_TIER             = ["1", "2"]         # T1 and T2 are actionable
TIER1_SIZE_MULTIPLIER    = 1.0                # Full size on T1
TIER2_SIZE_MULTIPLIER    = 0.75               # Slightly reduced on T2

# ─────────────────────────────────────────────────────────
# SPREAD TYPE & STRUCTURE
# ─────────────────────────────────────────────────────────
SPREAD_TYPE              = "debit"            # Debit spreads only
OPTION_SIDE              = "call"             # Bull debit = call spreads
BOTH_LEGS_ITM            = True               # Both legs must be ITM at entry
# Width preference order: try $1 first, then $2.50, then $5
WIDTH_PREFERENCE         = [1.0, 2.50, 5.0]
NO_HALF_DOLLAR_WIDTHS    = True               # Never trade $0.50 widths

# ─────────────────────────────────────────────────────────
# COST / QUALITY FILTERS
# ─────────────────────────────────────────────────────────
MAX_COST_PCT_OF_WIDTH    = 0.70               # Debit must be ≤ 70% of width
MIN_COST_PCT_OF_WIDTH    = 0.20               # Below 20% = suspicious pricing
MAX_RISK_PER_TRADE_USD   = 1500.0             # Max $1,500 debit per trade

# ─────────────────────────────────────────────────────────
# DTE & TIMING
# ─────────────────────────────────────────────────────────
MIN_DTE                  = 1                  # Minimum 1 DTE
MAX_DTE                  = 5                  # Maximum 5 DTE
TARGET_DTE               = 3                  # Prefer ~3 DTE
NO_ENTRY_FIRST_MINUTES   = 15                 # No entry in first 15 min (9:30-9:45 ET)

# ─────────────────────────────────────────────────────────
# EXIT RULES
# ─────────────────────────────────────────────────────────
# Exit targets are % RETURN ON RISK (debit paid)
# If paid $0.70 → same-day target = $0.70 * 1.30 = $0.91
SAME_DAY_EXIT_PCT        = 0.30               # 30% return on risk
NEXT_DAY_EXIT_PCT        = 0.35               # 35% return on risk
EXTENDED_HOLD_EXIT_PCT   = 0.50               # 50% if signal still strong past day 2

# Stop loss — only on high-volume tickers
STOP_LOSS_PCT            = 0.40               # 40% loss (e.g., $0.70 → stop at $0.42)
HIGH_VOLUME_TICKERS      = [                  # Only these get stop losses
    "SPY", "QQQ", "GLD", "IWM", "DIA",
    "AAPL", "MSFT", "NVDA", "AMZN", "META",
    "TSLA", "GOOGL", "AMD", "SPX",
]
USE_STOP_LOSS_ALL        = False              # If True, apply stop to all tickers

# ─────────────────────────────────────────────────────────
# DEAL-BREAKERS (hard filters — trade is rejected)
# ─────────────────────────────────────────────────────────
NO_EARNINGS_WEEK         = True               # Block trades during earnings week
NO_DIVIDEND_IN_DTE       = True               # Block if ex-div falls within DTE window
MIN_OPEN_INTEREST        = 50                 # Minimum OI per leg
MAX_BID_ASK_SPREAD       = 0.50               # Max bid/ask spread per leg
MIN_VOLUME_LEG           = 0                  # No hard minimum (OI is enough)

# ─────────────────────────────────────────────────────────
# POSITION SIZING
# ─────────────────────────────────────────────────────────
# contracts = min(MAX_RISK / (debit * 100), MAX_CONTRACTS)
MAX_CONTRACTS            = 50                 # Safety cap
ACCOUNT_SIZE             = 100_000.0          # For % risk calculations
MAX_RISK_PCT_ACCOUNT     = 0.02               # 2% of account per trade

# ─────────────────────────────────────────────────────────
# WEBHOOK CONFIDENCE MAPPING
# ─────────────────────────────────────────────────────────
# How Pine Script webhook fields map to confidence scoring
CONFIDENCE_BOOSTS = {
    "tier_1":            15,    # T1 signal
    "tier_2":            5,     # T2 signal
    "htf_confirmed":     10,    # 1H trend confirmed bullish
    "htf_converging":    5,     # 1H trend converging
    "daily_bull":        10,    # Daily trend bullish
    "rsi_mfi_bull":      5,     # RSI+MFI buying pressure
    "above_vwap":        5,     # Price above VWAP
    "wave_oversold":     10,    # Wave trend below -30
}

CONFIDENCE_PENALTIES = {
    "htf_diverging":     -20,   # 1H trend diverging bearish — big penalty
    "daily_bear":        -10,   # Daily trend bearish
    "wave_overbought":   -15,   # Wave trend above 60
    "low_oi":            -5,    # Per leg with low OI
    "wide_spread":       -5,    # Per leg with wide bid/ask
    "earnings_week":     -100,  # Instant kill
    "dividend_in_dte":   -100,  # Instant kill
}

MIN_CONFIDENCE_TO_TRADE  = 40    # Below this = no trade
