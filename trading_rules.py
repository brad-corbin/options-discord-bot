# trading_rules.py
# Brad's Trading Rules — encoded from conversation
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# v4.2 additions (2026-03-17):
#   - LOW VOL CHOP regime: tighter position sizing and confidence gate
#   - check_ticker timeout reduced 75s → 45s (CAT timeout wasted a full worker)
#   - MODERATE PIN regime: bear signals now skip if not enough ITM puts (already
#     enforced in check_ticker — added rule constant for explicit gating)
#   - Swing IV bounds guard constants added (fix for NFLX EM overflow bug)
#   - Swing preferred DTE tightened 30 → 21 (avoid 45 DTE unless no better option)
#   - OHLC warning dedup: log only first occurrence per ticker per cycle (app.py note)

# ─────────────────────────────────────────────────────────
# DIRECTION & SIGNAL
# ─────────────────────────────────────────────────────────
ALLOWED_DIRECTIONS       = ["bull", "bear"]
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
CHECK_TICKER_TIMEOUT_SEC     = 90    # 90s: allows up to 75s prefetch wait + ~15s processing
NO_ENTRY_FIRST_MINUTES       = 15

# ─────────────────────────────────────────────────────────
# REGIME-SPECIFIC GATES (v4.2)
# ─────────────────────────────────────────────────────────
# LOW VOL CHOP (VIX 22-25, ADX < 20): reduce size, raise confidence gate.
# v4.3: lowered from 75 → 65 — 75 was too aggressive, blocked all T2 signals.
# Normal gate is 60; 65 is only 5 above normal, enough to filter noise
# without making CHOP regime = zero trades.
CHOP_REGIME_CONF_GATE        = 65    # vs normal MIN_CONFIDENCE_TO_TRADE=60
CHOP_REGIME_SIZE_MULT        = 0.65  # vs REGIME_CHOPPY_SIZE_MULT=0.75 — tighter

# PIN regime: block directional debit spreads when gamma is pinning price.
# v4.3: Only block the OPPOSING side in PIN. If bias confirms direction
# and v4 shows PIN, we still block the side fighting the pin.
# Setting both to True was too aggressive — blocked ALL entries.
PIN_REGIME_BLOCK_BEAR_PUTS   = True  # Block bear puts if v4 regime contains PIN
PIN_REGIME_BLOCK_BULL_CALLS  = False # v4.3: Allow bull calls in PIN — calls can work if price drifts up to pin

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
MIN_VOLUME_LEG           = 0

# ═══════════════════════════════════════════════════════════
# HARD LIQUIDITY FILTERS (v4.1)
# ═══════════════════════════════════════════════════════════

# v4.3: Relaxed significantly for elevated vol environments (VIX 25+).
# At VIX 27, bid-ask spreads on $10-50 stocks routinely hit $0.30-$0.50.
# Old thresholds (500 OI, $0.15 spread, 12% pct) filtered ALL strikes
# for mid-cap tickers (IREN, MRNA, RTX, HD, BWXT, BE, INOD).
# The relaxed pass (2.5x) is the real safety net; strict pass should
# let through "tradeable" contracts, not "perfect" ones.
MIN_OPEN_INTEREST        = 100
MAX_BID_ASK_SPREAD       = 0.40
MAX_SPREAD_PCT_OF_MID    = 0.25

INDEX_ETF_TICKERS        = {"SPY", "QQQ", "IWM", "DIA", "SPX", "GLD"}
INDEX_MIN_OPEN_INTEREST  = 1000
INDEX_MAX_BID_ASK_SPREAD = 0.08
INDEX_MAX_SPREAD_PCT     = 0.06

LARGE_CAP_TICKERS        = {
    "AAPL", "MSFT", "NVDA", "AMZN", "META",
    "TSLA", "GOOGL", "AMD", "NFLX", "COIN",
}
# v4.3: Relaxed for elevated vol — MRNA, HD, RTX were all getting blocked.
LARGE_CAP_MIN_OI         = 250
LARGE_CAP_MAX_SPREAD     = 0.25
LARGE_CAP_MAX_SPREAD_PCT = 0.15


def get_liquidity_thresholds(ticker: str) -> dict:
    t = ticker.upper()
    if t in INDEX_ETF_TICKERS:
        return {"min_oi": INDEX_MIN_OPEN_INTEREST, "max_spread": INDEX_MAX_BID_ASK_SPREAD,
                "max_spread_pct": INDEX_MAX_SPREAD_PCT, "tier": "index"}
    elif t in LARGE_CAP_TICKERS:
        return {"min_oi": LARGE_CAP_MIN_OI, "max_spread": LARGE_CAP_MAX_SPREAD,
                "max_spread_pct": LARGE_CAP_MAX_SPREAD_PCT, "tier": "large_cap"}
    else:
        return {"min_oi": MIN_OPEN_INTEREST, "max_spread": MAX_BID_ASK_SPREAD,
                "max_spread_pct": MAX_SPREAD_PCT_OF_MID, "tier": "default"}


# ═══════════════════════════════════════════════════════════
# SLIPPAGE MODEL (v4.1)
# ═══════════════════════════════════════════════════════════

SLIPPAGE_SPREAD_FACTOR   = 0.35
SLIPPAGE_MIN_EV_AFTER    = 0.0


# ═══════════════════════════════════════════════════════════
# TRADE RANKING MODEL (v4.1)
# ═══════════════════════════════════════════════════════════

RANK_WEIGHT_EV           = 0.30
RANK_WEIGHT_WIN_PROB     = 0.25
RANK_WEIGHT_LIQUIDITY    = 0.20
RANK_WEIGHT_IV_EDGE      = 0.10
RANK_WEIGHT_EM_DISTANCE  = 0.10
RANK_WEIGHT_WIDTH_EFF    = 0.05
RANK_MIN_SCORE           = 0.15


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
    "daily_bear":        10,
    "rsi_mfi_bull":      5,
    "rsi_mfi_bear":      5,
    "above_vwap":        5,
    "below_vwap":        5,
    "wave_oversold":     10,
    "wave_overbought":   10,
    "iv_edge":           8,
    "rv_edge":           10,
    "within_em":         5,
    "regime_trending":   8,
    "regime_low_vix":    5,
}

CONFIDENCE_PENALTIES = {
    "htf_diverging":     -10,    # v4.3: was -20, which destroyed confidence when TV didn't send the field
    "daily_bear":        -10,
    "daily_bull":        -10,
    "wave_overbought":   -15,
    "wave_oversold":     -15,
    "low_oi":            -5,
    "wide_spread":       -5,
    "earnings_week":     -100,
    "dividend_in_dte":   -100,
    "iv_crushed":        -5,
    "beyond_em":         -8,
    "regime_choppy":     -10,
    "regime_high_vix":   -5,
    "regime_crisis":     -10,
}

MIN_CONFIDENCE_TO_TRADE  = 60
MIN_WIN_PROBABILITY      = 0.45

# ─────────────────────────────────────────────────────────
# EXPECTED MOVE & IV vs RV EDGE
# ─────────────────────────────────────────────────────────
EM_DISPLAY_ON_CARD       = True
IV_RV_DISPLAY_ON_CARD    = True
IV_RANK_LOW              = 20
IV_RANK_HIGH             = 70
RV_LOOKBACK_DAYS         = 60  # 60 days needed for RV, IV rank (was 20 — too few for get_iv_rank_from_closes)
RV_ANNUALIZE_FACTOR      = 252
IV_RV_BUYER_EDGE_PCT     = -5.0
IV_RV_SELLER_EDGE_PCT    = 5.0


# ═══════════════════════════════════════════════════════════
# JOURNAL FEEDBACK LOOP (v4.1)
# ═══════════════════════════════════════════════════════════

JOURNAL_FEEDBACK_ENABLED      = True
JOURNAL_MIN_TRADES_FOR_STATS  = 15
JOURNAL_SUPPRESS_WIN_RATE     = 0.30
JOURNAL_REDUCE_WIN_RATE       = 0.40
JOURNAL_REDUCE_SIZE_MULT      = 0.50
JOURNAL_LOOKBACK_SIGNALS      = 30


# ═══════════════════════════════════════════════════════════
# PORTFOLIO-LEVEL RISK LIMITS
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
# MARKET REGIME DETECTION
# ═══════════════════════════════════════════════════════════

REGIME_VIX_LOW           = 15.0
REGIME_VIX_NORMAL        = 25.0
REGIME_VIX_ELEVATED      = 35.0
REGIME_ADX_CHOPPY        = 20.0
REGIME_ADX_TRENDING      = 30.0
REGIME_CRISIS_BLOCK      = True
REGIME_ELEVATED_SIZE_MULT = 0.50
REGIME_CHOPPY_SIZE_MULT  = 0.75
REGIME_TRENDING_SIZE_MULT = 1.25  # Bonus sizing in trending low-vol regime


# ═══════════════════════════════════════════════════════════
# TRADE JOURNAL SETTINGS
# ═══════════════════════════════════════════════════════════

JOURNAL_LOG_ALL_SIGNALS  = True
JOURNAL_LOG_REJECTED     = True
JOURNAL_MAX_ENTRIES      = 5000
GREEKS_ATTRIBUTION_ON_CLOSE = True


# ═══════════════════════════════════════════════════════════
# DIGEST / TRADECARD SETTINGS (v4.1)
# ═══════════════════════════════════════════════════════════

IMMEDIATE_POST_TIER          = ["1"]
IMMEDIATE_POST_MIN_CONF      = 75
IMMEDIATE_POST_0DTE          = True
DIGEST_CARD_CACHE_TTL_SEC    = 3600


# ═══════════════════════════════════════════════════════════
# SWING ENGINE SETTINGS (v4.2)
# ═══════════════════════════════════════════════════════════

# DTE preferences — tightened from 30 to 21.
# NFLX T1 today used 45 DTE (2026-05-01). For a 2–3 week swing
# thesis, 21 DTE keeps theta manageable and exits cleaner.
SWING_TARGET_DTE_OVERRIDE    = 21    # Overrides swing_engine.SWING_TARGET_DTE=30
SWING_MAX_DTE_OVERRIDE       = 45    # Same ceiling, but engine should prefer shorter

# IV sanity bounds for avg_iv computation — fixes EM overflow bug.
# swing_engine now clamps all IV values to this range before averaging.
# Deep OTM / near-expiry contracts can return IV > 100.0 (10,000%)
# from MarketData.app; those must be excluded from the EM input.
SWING_IV_MIN                 = 0.05  # 5%  — below this is data noise
SWING_IV_MAX                 = 5.00  # 500% — above this is a blown-up contract
SWING_IV_ATM_BAND_PCT        = 0.05  # Use ATM-only IV (within 5% of spot) when >= 3 hits

# Logging dedup: suppress repeated OHLC/candle timeout warnings after first.
# Today's run logged 32 identical "Cached OHLC bars fetch failed for MA" lines.
# app.py should track warned_tickers per cycle and skip subsequent log lines.
OHLC_WARN_ONCE_PER_CYCLE     = True
