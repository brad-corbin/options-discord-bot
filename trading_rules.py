# trading_rules.py
# Brad's Trading Rules — encoded from conversation
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# v5.0 additions (2026-03-26):
#   - ADAPTIVE STRIKE PLACEMENT: BOTH_LEGS_ITM=False, delta-based short leg
#   - PRE-CHAIN GATE: qualification checks before expensive chain API calls
#   - ACTIVE SCANNER: continuous 35-ticker watchlist monitoring
#   - NEW CONFIDENCE FACTORS: volume, sector, macro, fundamentals, VIX term structure
#   - ECONOMIC CALENDAR: FOMC/CPI/NFP awareness, 0DTE blocking
#   - FUNDAMENTAL SCREENING: PEG, EPS growth, Lynch classification for swing
#   - SECTOR ROTATION: relative strength ranking for confidence scoring
#   - VIX TERM STRUCTURE: contango/backwardation regime nuance
#   - TRAILING STOPS: activation after 20%+ profit
#   - EXPANDED WATCHLIST: 35 tickers across 3 scan tiers
#   - EXPANDED SECTOR MAP: all scanned tickers mapped
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
# v5.0: Adaptive strike placement replaces rigid BOTH_LEGS_ITM=True.
# Long leg always ITM for directional exposure. Short leg uses
# delta-based placement — can be ATM for cheaper spreads on 0-3 DTE.
BOTH_LEGS_ITM            = False
ITM_LONG_LEG_REQUIRED    = True     # Long leg must always be ITM
SHORT_LEG_PLACEMENT      = "DELTA"  # "ITM" (old behavior) or "DELTA" (new)
SHORT_LEG_MIN_DELTA_CALL = 0.40     # Short call delta floor (prevents far OTM)
SHORT_LEG_MIN_DELTA_PUT  = -0.40    # Short put delta floor (absolute value)
# For 0-3 DTE: allow ATM short leg (delta ~0.45-0.55) for cheaper spreads.
# For 4+ DTE: prefer ITM short leg (delta >= 0.55) for higher probability.
SHORT_LEG_DTE_ITM_THRESHOLD = 4     # DTE above this prefers ITM short leg
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
    # Tier A — scanned every 5 min
    "SPY", "QQQ", "IWM",
    # Tier B — scanned every 10 min
    "AAPL", "MSFT", "NVDA", "AMZN", "META",
    "TSLA", "GOOGL", "AMD", "NFLX", "COIN",
    "AVGO", "PLTR",
    # Tier C — scanned every 15 min
    "DIA", "GLD", "SPX", "CRM", "ORCL",
    "ARM", "SMCI", "MSTR", "XLF", "XLE",
    "XLV", "SOXX", "TLT", "JPM", "GS",
    "BA", "CAT", "LLY", "UNH",
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
# SLIPPAGE MODEL (v4.1, v5.0 index override)
# ═══════════════════════════════════════════════════════════

SLIPPAGE_SPREAD_FACTOR   = 0.35
SLIPPAGE_MIN_EV_AFTER    = 0.0

# v5.0: Index ETFs have the tightest spreads in the market.
# At VIX 25, SPY put spread B/A is still ~$0.03-0.06 wide.
# The default 0.35 factor was rejecting every SPY spread as
# slippage_or_negative_ev on a day where the thesis was 5/5
# and SPY moved $9. Override for liquid index products.
INDEX_SLIPPAGE_SPREAD_FACTOR = 0.15  # SPY/QQQ/SPX can fill at mid
LARGE_CAP_SLIPPAGE_SPREAD_FACTOR = 0.25  # AAPL/NVDA/AMZN etc

def get_slippage_factor(ticker: str) -> float:
    """Return tier-appropriate slippage factor."""
    t = ticker.upper()
    if t in INDEX_ETF_TICKERS:
        return INDEX_SLIPPAGE_SPREAD_FACTOR
    elif t in LARGE_CAP_TICKERS:
        return LARGE_CAP_SLIPPAGE_SPREAD_FACTOR
    return SLIPPAGE_SPREAD_FACTOR


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
    # v5.0 — new confidence factors
    "volume_surge":      10,    # Current volume > 1.5x 20-day avg
    "volume_above_avg":  5,     # Current volume > 1.0x avg
    "sector_strong":     5,     # Ticker's sector in top 3 relative strength
    "term_structure_contango": 3,  # VIX in contango — normal conditions
    "insider_buying":    5,     # Net insider buying last 90 days (swing only)
    "peg_under_1":       8,     # PEG < 1.0 — Lynch golden metric (swing only)
    "peg_under_1_5":     3,     # PEG 1.0-1.5 — fairly valued (swing only)
    "lynch_fast_grower": 10,    # Lynch: fast grower classification (swing only)
    "lynch_stalwart":    5,     # Lynch: stalwart classification (swing only)
    "consistent_growth": 5,     # 2+ years of positive EPS growth (swing only)
    "iv_cheap_vs_rv":    8,     # IV < RV (implied EM < realized EM) — buyer edge
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
    "iv_crushed":        -8,     # v5.0: raised from -5 — IV crush matters more for debit buyer
    "beyond_em":         -8,
    "regime_choppy":     -10,
    "regime_high_vix":   -5,
    "regime_crisis":     -10,
    # v5.0 — new penalties
    "volume_dry":        -8,     # Current volume < 0.5x average
    "sector_weak":       -5,     # Sector in bottom 3 of 11
    "macro_event_today": -15,    # High-impact econ event TODAY (FOMC, CPI, NFP)
    "macro_event_window":-8,     # High-impact event within DTE window (not today)
    "term_structure_backwardation": -8,  # VIX in backwardation — stress
    "term_structure_severe": -15, # Severe backwardation — panic
    "vvix_extreme":      -5,     # VVIX > 130 — regime instability
    "eod_approach_0dte": -5,     # Signal after 2:30 PM CT for 0DTE
    "insider_selling":   -5,     # Net insider selling (swing only)
    "peg_over_2_5":      -8,     # PEG > 2.5 — overvalued (swing only)
    "negative_eps":      -12,    # Negative EPS growth (swing only)
    "lynch_slow_grower": -8,     # Lynch: slow grower (swing only)
    "iv_rich_vs_rv":     -5,     # IV > RV — seller has edge, not buyer
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
    "AAPL":  "tech", "MSFT":  "tech", "GOOGL": "comm_svc", "META":  "comm_svc",
    "AMZN":  "cons_disc", "NVDA":  "tech", "AMD":   "tech", "TSLA":  "cons_disc",
    "SPY":   "index", "QQQ":  "index", "IWM":  "index", "DIA":  "index",
    "SPX":   "index", "GLD":  "commodity",
    # v5.0 — expanded for full watchlist
    "NFLX":  "comm_svc", "COIN": "fintech", "AVGO": "tech", "PLTR": "tech",
    "CRM":   "tech", "ORCL": "tech", "ARM":  "tech", "SMCI": "tech",
    "MSTR":  "tech", "JPM":  "financial", "GS":  "financial",
    "BA":    "industrial", "CAT": "industrial", "LLY": "healthcare",
    "UNH":   "healthcare", "XLF": "sector_etf", "XLE": "sector_etf",
    "XLV":   "sector_etf", "SOXX": "sector_etf", "TLT": "bond",
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


# ═══════════════════════════════════════════════════════════
# TRAILING STOP & DYNAMIC EXIT SETTINGS (v5.0)
# ═══════════════════════════════════════════════════════════

# Trailing stop activates after position reaches this profit threshold.
# Once activated, stop trails at DISTANCE below the max favorable excursion.
TRAILING_STOP_ENABLED         = True
TRAILING_STOP_ACTIVATION_PCT  = 0.20    # Activate after 20% profit achieved
TRAILING_STOP_DISTANCE_PCT    = 0.50    # Trail at 50% of max profit (give back half)
TRAILING_STOP_MIN_DISTANCE    = 0.10    # Never trail tighter than 10% of max profit

# Time-weighted exit targets for 0DTE.
# Early in session: hold for bigger target. Late: take what you have.
# Applied as multiplier to SAME_DAY_EXIT_PCT.
# session_progress 0.0=open, 1.0=close
DYNAMIC_EXIT_0DTE_ENABLED     = True
DYNAMIC_EXIT_EARLY_MULT       = 1.5     # Before 10:30 AM: target 45% (0.30 × 1.5)
DYNAMIC_EXIT_MID_MULT         = 1.0     # 10:30-1:30: target 30% (normal)
DYNAMIC_EXIT_LATE_MULT        = 0.65    # After 1:30: target 20% (0.30 × 0.65)
DYNAMIC_EXIT_POWER_HOUR_MULT  = 0.50    # After 2:30: target 15% (take anything)


# ═══════════════════════════════════════════════════════════
# PRE-CHAIN GATE SETTINGS (v5.0)
# ═══════════════════════════════════════════════════════════

# Pre-chain gate: run cheap checks before pulling option chains.
# Saves ~5 API calls per rejected signal (1 expirations + 4 chains).
PRECHAIN_GATE_ENABLED             = True
PRECHAIN_MIN_CONFIDENCE_ESTIMATE  = 45    # Below this, chains won't save the trade
PRECHAIN_STALE_LIMIT_SEC          = 300   # Don't pull chains for signals > 5 min old
PRECHAIN_HARD_DRIFT_PCT           = 1.5   # Don't pull chains if price drifted > 1.5%
PRECHAIN_EARNINGS_BLOCK           = True  # Block before chains if earnings in window
PRECHAIN_MACRO_EVENT_BLOCK_0DTE   = True  # Block 0DTE if FOMC/CPI/NFP today


# ═══════════════════════════════════════════════════════════
# ACTIVE SCANNER SETTINGS (v5.0)
# ═══════════════════════════════════════════════════════════

# Enable/disable the active scanner background thread
ACTIVE_SCANNER_ENABLED       = True
SCANNER_SIGNAL_DEDUP_TTL     = 900   # Don't re-signal same ticker+bias within 15 min
SCANNER_MIN_SIGNAL_SCORE     = 50    # Minimum technical score to generate signal
SCANNER_T1_THRESHOLD         = 75    # Score for Tier 1 signal
SCANNER_T2_THRESHOLD         = 55    # Score for Tier 2 signal


# ═══════════════════════════════════════════════════════════
# FUNDAMENTAL SCREENING SETTINGS (v5.0)
# ═══════════════════════════════════════════════════════════

# Enable fundamental data for swing trade qualification
FUNDAMENTAL_SCREENING_ENABLED = True
FUNDAMENTAL_MIN_SCORE_SWING   = 30    # Min fundamental score for swing entry
FUNDAMENTAL_BATCH_HOUR_CT     = 17    # 5 PM CT — nightly batch fetch

# Lynch-specific thresholds
LYNCH_PEG_BUY_THRESHOLD      = 1.0   # PEG < 1.0 = strong buy signal
LYNCH_PEG_AVOID_THRESHOLD    = 2.5   # PEG > 2.5 = avoid for options
LYNCH_EPS_FAST_GROWER_PCT    = 0.20  # 20%+ EPS growth = fast grower
LYNCH_EPS_STALWART_PCT       = 0.05  # 5-20% = stalwart


# ═══════════════════════════════════════════════════════════
# IV vs RV EDGE ENHANCEMENT (v5.0)
# ═══════════════════════════════════════════════════════════

# Skew-adjusted expected move: asymmetric EM based on put/call skew
SKEW_ADJUSTED_EM_ENABLED     = True
SKEW_PUT_CALL_DELTA           = 25    # Use 25-delta for skew measurement
SKEW_ADJUSTMENT_WEIGHT        = 0.30  # How much skew shifts the EM center

# IV/RV ratio for confidence scoring
IV_RV_RATIO_BUYER_EDGE       = 0.90  # IV < 90% of RV → boost for debit buyer
IV_RV_RATIO_SELLER_EDGE      = 1.10  # IV > 110% of RV → penalty for debit buyer


# ═══════════════════════════════════════════════════════════
# ALERT HIERARCHY & SUPPRESSION (v5.0)
# ═══════════════════════════════════════════════════════════
#
# Fixes the "validator rejects but critical alert still fires" problem.
# Also suppresses repeated momentum fades and opposite-direction whipsaw.

# After EntryValidator rejects a setup, don't fire the critical trade alert.
# Instead downgrade to "info" priority so it appears only in logs.
ALERT_SUPPRESS_ON_VALIDATOR_REJECT = True

# After a critical trade alert (FADE SHORT / SQUEEZE LONG), suppress
# opposite-direction critical alerts for this many seconds unless a
# real level shift occurs (new break attempt at a different level).
ALERT_OPPOSITE_DIRECTION_COOLDOWN_SEC = 300  # 5 minutes

# Momentum fade alerts: only fire once per direction per this window.
# Prevents "downside fading / upside fading" ping-pong every 60 seconds.
ALERT_MOMENTUM_FADE_COOLDOWN_SEC = 600  # 10 min (was 300 via general cooldown)

# After the first clean failed-move signal per level, demote subsequent
# alerts at the same level from "critical" to "alert" (shown but not shouted).
ALERT_DEMOTE_REPEAT_FAILED_MOVE = True


# ═══════════════════════════════════════════════════════════
# VIX-SCALED STOP DISTANCE (v5.0)
# ═══════════════════════════════════════════════════════════
#
# At VIX 25, a $0.40 stop on SPY ($655) is 0.06% — noise level.
# The thesis monitor kept getting stopped out on minor wicks before
# the real move happened. Scale the minimum stop with VIX.

VIX_STOP_SCALE_ENABLED       = True
VIX_STOP_SCALE_BASE_VIX      = 15.0    # VIX reference point (min stop = BASE at this VIX)
VIX_STOP_SCALE_PER_POINT     = 0.023   # additional $ per VIX point above baseline
# v5.1: Linear model replaces multiplicative. At VIX 30, SPY moves $1+ per
# 5-min candle routinely. A $0.60 stop is noise, not a wrong thesis.
# Formula: min_stop = BASE + (VIX - BASELINE) × SCALE_PER_POINT
# VIX 15: $0.71 | VIX 20: $0.83 | VIX 25: $0.94 | VIX 30: $1.06 | VIX 35: $1.17
VIX_STOP_MIN_FLOOR_SPY       = 0.71    # base min stop at VIX 15
VIX_STOP_MIN_FLOOR_QQQ       = 0.65    # QQQ is slightly less volatile per point
VIX_STOP_MIN_FLOOR_DEFAULT   = 0.30

def get_vix_scaled_min_stop(ticker: str, vix: float = 20.0) -> float:
    """Compute VIX-scaled minimum stop distance in dollars.
    v5.1: Linear model — $0.71 + (VIX - 15) × $0.023.
    Replay validated: blocked 20 trades across 3 days that were 7W/13L = -$14,676."""
    if not VIX_STOP_SCALE_ENABLED:
        base = {"SPY": 0.71, "QQQ": 0.65}.get(ticker.upper(), 0.30)
        return base

    base = {"SPY": VIX_STOP_MIN_FLOOR_SPY, "QQQ": VIX_STOP_MIN_FLOOR_QQQ}.get(
        ticker.upper(), VIX_STOP_MIN_FLOOR_DEFAULT)

    # Linear scale: base + (VIX - baseline) × per-point increment
    vix_excess = max(0, vix - VIX_STOP_SCALE_BASE_VIX)
    scaled = base + vix_excess * VIX_STOP_SCALE_PER_POINT
    return round(scaled, 2)


# ═══════════════════════════════════════════════════════════
# EM GUIDE MATCHING (v5.0)
# ═══════════════════════════════════════════════════════════
#
# When a trade alert fires, check if it matches one of the EM guide's
# "SETUPS TO WATCH" recommendations. If so, annotate the alert with
# "📋 MATCHES EM GUIDE" to boost trader confidence in the signal.

EM_GUIDE_MATCHING_ENABLED = True


# ═══════════════════════════════════════════════════════════
# DIRECTIONAL LONG OPTIONS — INSTITUTIONAL FRAMEWORK (v5.1)
# ═══════════════════════════════════════════════════════════
#
# When spread candidates fail (slippage, liquidity) OR conditions
# strongly favor uncapped directional exposure, use long puts/calls.
#
# INSTITUTIONAL PRINCIPLES:
#
#   1. THETA CURVE: Buy 45-60 DTE. Sell at 21+ DTE remaining.
#      Theta decays ~1/3 in first half of life, ~2/3 in second half.
#      We rent the slow part, never hold into the acceleration zone.
#
#   2. STRIKE = EXPECTED MOVE × MULTIPLIER, ALIGNED TO LEVELS:
#      Compute the swing expected move for the thesis window.
#      Place strike at 1.0-1.5× EM distance from spot.
#      Then snap to nearest S/R level for structural confluence.
#
#   3. VOL-ADJUSTED STRIKE DISTANCE:
#      High IV (VIX 25+): go further OTM — premium is expensive,
#        larger moves are more likely, OTM gives better leverage/$.
#      Low IV (VIX < 18): stay closer to ATM — premium is cheap,
#        delta participation matters more than leverage.
#
#   4. EXIT FRAMEWORK (pre-defined before entry):
#      Time stop:   50% of planned holding period elapsed, flat → exit.
#      Profit:      50-100% gain → take it or trail 30% giveback.
#      Loss:        40-50% of premium lost → exit, thesis failed.
#
#   5. ASYMMETRY:
#      Put buyer in crash:  vol expands → delta + vega both help.
#      Call buyer in bounce: vol contracts → delta - vega (headwind).
#      → Long puts favored in high VIX, long calls in low VIX.
#        Exception: snap-back squeeze where delta overwhelms vega.
#
# ═══════════════════════════════════════════════════════════

NAKED_OPTION_ENABLED         = True

# ─── DTE Selection (ride the slow theta curve) ───
# Scalp path (0DTE / TV signals):
NAKED_SCALP_MIN_DTE          = 0
NAKED_SCALP_MAX_DTE          = 5       # 0-5 DTE for intraday directional
NAKED_SCALP_EXIT_BY_DTE      = 0       # exit same day

# Swing path:
NAKED_SWING_TARGET_DTE       = 52      # target: ~7.5 weeks out (45-60 sweet spot)
NAKED_SWING_MIN_DTE          = 35      # minimum: 5 weeks
NAKED_SWING_MAX_DTE          = 75      # maximum: ~10.5 weeks
NAKED_SWING_EXIT_BY_DTE      = 21      # sell before theta acceleration zone

# ─── Conditions: Long Puts (bearish) ───
NAKED_PUT_MIN_VIX            = 22.0    # VIX must be elevated (big moves likely)
NAKED_PUT_PREFER_GEX_NEG     = True    # GEX negative = downside accelerates
NAKED_PUT_MIN_CONFIDENCE     = 60      # confidence threshold

# ─── Conditions: Long Calls (bullish) ───
NAKED_CALL_LOW_VIX_MAX       = 18.0    # standard calls: VIX must be low (cheap premium)
NAKED_CALL_SNAPBACK_MIN_VIX  = 22.0    # snap-back calls: VIX high + squeeze setup
NAKED_CALL_MIN_CONFIDENCE    = 65      # higher bar — calls fight vol crush

# ─── Strike Selection (EM-based, vol-adjusted) ───
# The strike is placed at spot ± (expected_move × multiplier).
# High VIX → larger multiplier (further OTM, cheaper, more leverage).
# Low VIX → smaller multiplier (closer to ATM, better delta/$$).
NAKED_EM_MULT_LOW_VIX        = 0.7     # VIX < 18: strike at 0.7× EM from spot
NAKED_EM_MULT_MID_VIX        = 1.0     # VIX 18-25: strike at 1.0× EM
NAKED_EM_MULT_HIGH_VIX       = 1.3     # VIX 25+: strike at 1.3× EM (further OTM)
NAKED_EM_MULT_CRISIS_VIX     = 1.5     # VIX 30+: strike at 1.5× EM (max leverage)

# Delta guardrails (reject options outside this range regardless of EM calc)
NAKED_PUT_MIN_DELTA          = -0.40   # not too deep ITM (overpaying)
NAKED_PUT_MAX_DELTA          = -0.15   # not too far OTM (lottery ticket)
NAKED_CALL_MIN_DELTA         = 0.15    # not too far OTM
NAKED_CALL_MAX_DELTA         = 0.40    # not too deep ITM

# Level alignment: snap strike to nearest S/R if within this %
NAKED_LEVEL_SNAP_PCT         = 1.5     # snap to level within 1.5% of EM-derived strike

# ─── Sizing (smaller than spreads — max loss = full premium) ───
NAKED_SIZE_MULT              = 0.50    # half normal contract count
NAKED_MAX_PREMIUM_USD        = 600     # max $6.00 per contract ($600 per lot)
NAKED_MAX_PREMIUM_PCT_ACCT   = 0.015   # max 1.5% of account per trade

# ─── Exit Framework ───
# Time stop: exit if N% of planned holding period passes with no gain
NAKED_TIME_STOP_PCT          = 0.50    # 50% of hold window → exit if flat/losing
# Profit targets
NAKED_PROFIT_TARGET_1_PCT    = 0.50    # first scale: 50% gain → take 1/3
NAKED_PROFIT_TARGET_2_PCT    = 1.00    # second scale: 100% gain → take 1/3
NAKED_TRAIL_GIVEBACK_PCT     = 0.30    # trail: exit if gives back 30% from peak
# Loss stop
NAKED_LOSS_STOP_PCT          = 0.45    # exit at 45% loss of premium


def get_em_strike_multiplier(vix: float) -> float:
    """Return the EM multiplier for strike placement based on VIX."""
    if vix >= 30:
        return NAKED_EM_MULT_CRISIS_VIX
    elif vix >= 25:
        return NAKED_EM_MULT_HIGH_VIX
    elif vix >= 18:
        return NAKED_EM_MULT_MID_VIX
    else:
        return NAKED_EM_MULT_LOW_VIX


def get_naked_dte_range(is_swing: bool) -> tuple:
    """Return (min_dte, target_dte, max_dte) for the trade type."""
    if is_swing:
        return (NAKED_SWING_MIN_DTE, NAKED_SWING_TARGET_DTE, NAKED_SWING_MAX_DTE)
    else:
        return (NAKED_SCALP_MIN_DTE, NAKED_SCALP_MAX_DTE, NAKED_SCALP_MAX_DTE)


# ═══════════════════════════════════════════════════════════
# CRISIS REGIME — LONG OPTION EXIT FRAMEWORK (v5.1 Change 8)
# ═══════════════════════════════════════════════════════════
#
# When vol regime is CRISIS, long puts/calls are PRIMARY instrument.
# Spreads become fallback. The exit framework scales out in thirds
# to capture asymmetric moves while protecting capital.
#
# Phase 1: Hold window (no exit except premium stop)
# Phase 2: Scale 1/3 at 50% gain, 1/3 at 100% gain
# Phase 3: Trail last 1/3 at 30% giveback from peak
# Phase 4: Last 60 min — tighten to 15% giveback (theta acceleration)
#
# Replay validated: March 31 spread = +$486. Long option = +$5,000-$10,000.

CRISIS_LONG_OPTION_PRIMARY       = True    # long options primary in CRISIS
CRISIS_HOLD_WINDOW_MIN           = 15      # minutes — no exit (except premium stop)
CRISIS_PREMIUM_STOP_PCT          = 0.45    # 45% loss of entry premium → exit all
CRISIS_SCALE_1_PCT               = 0.50    # 50% premium gain → sell 1/3
CRISIS_SCALE_2_PCT               = 1.00    # 100% premium gain → sell 1/3
CRISIS_TRAIL_GIVEBACK_PCT        = 0.30    # trail last 1/3: 30% giveback from peak
CRISIS_FINAL_HOUR_GIVEBACK_PCT   = 0.15    # last 60 min: tighten to 15%
CRISIS_FINAL_HOUR_MINUTES        = 60      # when to tighten trail

# ─── CRISIS 0DTE Strike Targeting ───
# Slightly OTM in CRISIS: cheaper premium, explosive gamma when
# strike approaches ATM. VIX 30+ means $5-10 moves are routine,
# so delta 0.25-0.35 ($1-3 OTM) is high probability.
CRISIS_0DTE_DELTA_TARGET         = 0.30    # target delta for CRISIS 0DTE
CRISIS_0DTE_DELTA_RANGE          = (0.25, 0.35)  # acceptable range
# Standard (non-CRISIS):
LONG_OPTION_DELTA_TARGET         = 0.45
LONG_OPTION_DELTA_RANGE          = (0.35, 0.55)

# ─── CRISIS GEX conditions ───
# Puts in CRISIS: always primary (vol expands on drops = double tailwind)
# Calls in CRISIS: only if GEX negative (squeeze/snap-back context)
CRISIS_PUTS_ALWAYS_PRIMARY       = True    # no GEX requirement for puts
CRISIS_CALLS_REQUIRE_GEX_NEG     = True    # calls need GEX- for squeeze context


# ═══════════════════════════════════════════════════════════
# CONSECUTIVE LOSS CIRCUIT BREAKER (v5.1 Change 6)
# ═══════════════════════════════════════════════════════════
#
# After 2 consecutive stops in the same direction, pause that
# direction for 30 minutes. Prevents revenge trading.
# Tracks bias (bearish/bullish), not position type.
# A long put = bearish bias.

CIRCUIT_BREAKER_ENABLED          = True
CIRCUIT_BREAKER_MAX_CONSEC_STOPS = 2       # stops before pause
CIRCUIT_BREAKER_PAUSE_MIN        = 30      # minutes to pause
# What counts as a stop: hard stop, premium stop
# What does NOT count: giveback, scale, momentum exhaustion, trail


# ═══════════════════════════════════════════════════════════
# MULTI-TOUCH LEVEL BREAK (v5.1 Change 9)
# ═══════════════════════════════════════════════════════════
#
# When a support/resistance level has been tested 3+ times on the
# same side and then breaks with spot-poll confirmation, fire a
# long option entry with the stop above the FULL consolidation zone.
#
# This naturally produces $1.00+ stop distances that pass the
# VIX gate, and captures structural breakdowns/breakouts that
# the tight-stop fades miss.
#
# Replay validated:
#   March 27: $640 support broke → $633 → +$4,710 with long put
#   March 30: $636 support broke → $630 → +$4,180 with long put

MULTI_TOUCH_BREAK_ENABLED        = True
MULTI_TOUCH_MIN_TOUCHES          = 3       # same-side touches before break qualifies
MULTI_TOUCH_LOOKBACK_MIN         = 30      # level must have existed for 30+ minutes
MULTI_TOUCH_STOP_ZONE_BUFFER     = 0.10    # add $0.10 above/below zone for safety
MULTI_TOUCH_CONFIRM_POLLS        = 3       # consecutive spot polls beyond level
MULTI_TOUCH_MAX_ACTIVE           = 1       # max active Change 9 trades per ticker
MULTI_TOUCH_MAX_TOUCHES          = 10      # levels tested >10x are ranges, not fresh breaks
MULTI_TOUCH_MAX_AGE_MIN          = 180     # level must be < 3 hours old (first_seen)
MULTI_TOUCH_RECENT_TOUCH_MIN     = 60      # last touch must be within 60 min

# ── v5.1 Change 3: Spot-poll confirmation ───
# Dedicated constant for normal break confirmation (separate from multi-touch)
BREAK_CONFIRM_POLLS              = 3       # consecutive 60s spot polls beyond level


# ═══════════════════════════════════════════════════════════
# SWING SCANNER (v5.1)
# ═══════════════════════════════════════════════════════════
#
# Python translation of Brad's Fibonacci Swing Signal v3.0
# plus institutional enhancements. Runs daily on Yahoo Finance
# data (free, unlimited). Separate watchlist from scalp scanner.

SWING_SCANNER_ENABLED        = True

# ── Swing-only tickers (not liquid enough for 0DTE, fine for 45-60 DTE) ──
# Add tickers here anytime. They get swing-scanned but never scalp-scanned.
SWING_ONLY_TICKERS = {
    "INOD", "BWXT", "PLTR", "SNOW", "CRWD", "NET", "DDOG", "ZS",
    "PANW", "FTNT", "ABNB", "DASH", "RBLX", "U", "TTD", "SHOP",
    "MELI", "SE", "GRAB", "NU", "SQ", "PYPL", "AFRM", "UPST",
    "ENPH", "SEDG", "FSLR", "RUN", "PLUG", "RIVN", "LCID", "NIO",
    "LI", "XPEV", "DKNG", "PENN", "MGM", "WYNN", "LVS",
    "EL", "LULU", "NKE", "SBUX", "CMG", "MCD",
    "PFE", "MRNA", "ABBV", "LLY", "UNH", "JNJ",
    "XOM", "CVX", "OXY", "SLB", "HAL",
    "JPM", "GS", "MS", "BAC", "C", "WFC",
    "CAT", "DE", "HON", "GE", "RTX", "LMT", "NOC",
    "COST", "WMT", "TGT", "HD", "LOW",
    "CRM", "ORCL", "NOW", "ADBE", "INTU",
}

# Combined swing watchlist = scalp tickers + swing-only
SWING_WATCHLIST = set(HIGH_VOLUME_TICKERS) | SWING_ONLY_TICKERS

# ── Scanner schedule ──
SWING_SCAN_TIMES_CT          = ["08:15", "15:30"]  # pre-market + post-close
SWING_SCAN_LOOKBACK_DAYS     = 120     # daily bars to fetch (need 50+ for pivots)

# ── Fibonacci settings (mirrors Pine v3.0 inputs) ──
SWING_FIB_LOOKBACK           = 50      # pivot lookback bars
SWING_FIB_TOUCH_ZONE_PCT     = 1.25    # % zone for touch detection

# ── Trend filters ──
SWING_WEEKLY_EMA_FAST        = 5
SWING_WEEKLY_EMA_SLOW        = 20
SWING_WEEKLY_MIN_SEP_PCT     = 0.15
SWING_DAILY_EMA_FAST         = 8
SWING_DAILY_EMA_SLOW         = 21

# ── Momentum ──
SWING_RSI_LENGTH             = 14
SWING_RSI_OVERSOLD           = 48
SWING_RSI_OVERBOUGHT         = 52
SWING_VOL_MA_LENGTH          = 20
SWING_VOL_CONTRACT_MULT      = 0.90
SWING_VOL_EXPAND_MULT        = 1.15

# ── Candle quality ──
SWING_WICK_MIN_PCT           = 35.0
SWING_CLOSE_ZONE_PCT         = 35.0
SWING_COOLDOWN_BARS          = 3

# ── Institutional: Relative strength ──
SWING_RS_LOOKBACK_DAYS       = 20      # RS ratio computed over this window
SWING_RS_REJECT_LONG_BELOW   = -3.0    # reject bull if ticker RS < SPY by this %
SWING_RS_REJECT_SHORT_ABOVE  = 3.0     # reject bear if ticker RS > SPY by this %

# ── Institutional: Correlation grouping ──
SWING_MAX_PER_SECTOR         = 2       # max signals per sector per scan
SWING_SECTOR_MAP = {
    "XLK": ["AAPL", "MSFT", "NVDA", "AMD", "GOOGL", "META", "AVGO", "CRM",
            "ORCL", "NOW", "ADBE", "INTU", "PLTR", "SNOW", "CRWD", "NET",
            "DDOG", "ZS", "PANW", "FTNT", "TTD", "SHOP", "U"],
    "XLF": ["JPM", "GS", "MS", "BAC", "C", "WFC", "SOFI", "COIN", "AFRM", "UPST", "SQ", "PYPL"],
    "XLE": ["XOM", "CVX", "OXY", "SLB", "HAL"],
    "XLY": ["AMZN", "TSLA", "ABNB", "DASH", "RBLX", "DKNG", "PENN", "MGM",
            "WYNN", "LVS", "NKE", "SBUX", "CMG", "MCD", "LULU", "EL",
            "HD", "LOW", "TGT", "COST", "WMT"],
    "XLV": ["PFE", "MRNA", "ABBV", "LLY", "UNH", "JNJ"],
    "XLI": ["CAT", "DE", "HON", "GE", "RTX", "LMT", "NOC", "BWXT", "ITA"],
    "XLRE": ["RIVN", "LCID", "NIO", "LI", "XPEV"],  # EV cluster
    "XLC": ["NFLX", "GOOG"],
    "ENERGY_ALT": ["ENPH", "SEDG", "FSLR", "RUN", "PLUG"],
    "INDEX": ["SPY", "QQQ", "IWM", "DIA"],
    "CRYPTO": ["COIN", "MSTR", "CIFR", "IREN", "MARA"],
}

# ── Institutional: Primary trend filter ──
SWING_PRIMARY_TREND_SMA      = 50      # 50-day SMA
SWING_PRIMARY_TREND_LMA      = 200     # 200-day SMA
# Don't fight the primary trend:
# If 50 < 200, reject bull signals (death cross environment)
# If 50 > 200, reject bear signals only if RS is strong (golden cross)
SWING_PRIMARY_TREND_ENABLED  = True

# ── ATR-based sizing ──
SWING_ATR_LENGTH             = 14
SWING_ATR_RISK_PCT           = 0.01    # risk 1% of account per trade

