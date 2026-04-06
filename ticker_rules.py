# ticker_rules.py
# ═══════════════════════════════════════════════════════════════════
# Per-Ticker Trading Rules — Active Scanner Backtest Derived
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Each ticker has a rule set for BEAR / TRANSITION / BULL regime.
# The active_scanner reads this table, filters signals, and builds
# the signal alert — the trader sees everything they need inline.
#
# Rule derivation: active scanner backtest Oct 2025 – Apr 2026 (10 tickers)
# plus full-year 2025 SLV backtest and Apr 2025 – Apr 2026 QQQ backtest.
#
# Current regime (March 2026): BEAR
# Focused live tickers: MSFT · IWM · QQQ · META · TSLA · AAPL
#
# ─── WHAT NEVER CHANGES ────────────────────────────────────────────
#   MSFT / META / TSLA  — always CONFIRMED+bear (works every regime)
#   AAPL                — always CONVERGING+bull, always exit 5d
#   NVDA / AMZN         — always OPPOSING when active, no score gate
#   Score ≥80 cap       — hard skip on MSFT / GOOGL / GLD / SLV always
# ═══════════════════════════════════════════════════════════════════

from typing import Optional, List
from datetime import date, timedelta

# ── Regime labels ──
BEAR       = "BEAR"
TRANSITION = "TRANSITION"
BULL       = "BULL"


# ═══════════════════════════════════════════════════════════
# COMPLETE PER-TICKER RULE TABLE
# ═══════════════════════════════════════════════════════════
#
# Each rule dict contains:
#   active          bool   — is this ticker tradeable in this regime?
#   htf             str    — required htf_status from scanner
#   bias            str    — "bear" | "bull" | "both" (OPPOSING)
#   score_min       int    — minimum score to take signal
#   score_max       int    — maximum score (99 = no cap)
#   phase           str|None — "MORNING" to restrict phase, None = any
#   rsi_max         int|None — RSI must be BELOW this for bull signals
#   rsi_min         int|None — RSI must be ABOVE this for bear signals (unused except flagging)
#   exit_days       int    — hold to this many calendar days post-entry
#   spread          str    — "bear_put" | "bull_call"
#   premium_flag    str|None — condition that triggers premium flag (e.g. "RSI>50")
#   premium_wr      float  — expected WR when premium condition is met
#   wr_3d           float  — backtest WR at 3d with buffer (%)
#   wr_5d           float  — backtest WR at 5d with buffer (%)
#   n               int    — backtest signal count
#   period          str    — backtest period label
#   notes           list   — things the trader SHOULD do
#   never           list   — hard NO rules shown in every alert

TICKER_RULES = {

    # ────────────────────────────────────────────────────────
    # MSFT — Always CONFIRMED+bear. Score 60–79. Exit 5d.
    # RSI>50 = premium entry (81% WR) — size up.
    # ────────────────────────────────────────────────────────
    "MSFT": {
        BEAR: {
            "active": True,
            "htf": "CONFIRMED", "bias": "bear",
            "score_min": 60, "score_max": 79,
            "phase": None, "rsi_max": None, "rsi_min": None,
            "exit_days": 5, "spread": "bear_put",
            "premium_flag": "RSI>50",
            "premium_wr": 81.0,
            "wr_3d": 96.0, "wr_5d": 97.8, "n": 50, "period": "Mar 1+",
            "notes": [
                "Hold to expiration (5d from entry)",
                "RSI>50 at signal time = PREMIUM — size up, WR 81%",
            ],
            "never": [
                "Exit at 3d — must hold to 5d",
                "Take score ≥80 — already filtered",
                "Take bull signals on MSFT",
                "Take OPPOSING signals on MSFT",
                "Exit early unless 75%+ max profit already hit",
            ],
        },
        TRANSITION: {
            "active": True,
            "htf": "CONFIRMED", "bias": "bear",
            "score_min": 60, "score_max": 79,
            "phase": None, "rsi_max": None, "rsi_min": None,
            "exit_days": 5, "spread": "bear_put",
            "premium_flag": "RSI>50",
            "premium_wr": 81.0,
            "wr_3d": 60.3, "wr_5d": 64.2, "n": 151, "period": "Oct 25 – Feb 26",
            "notes": ["Hold to 5d", "RSI>50 = premium"],
            "never": ["Exit at 3d", "Score ≥80", "Bull signals"],
        },
        BULL: {
            "active": True,
            "htf": "CONFIRMED", "bias": "bear",
            "score_min": 60, "score_max": 79,
            "phase": None, "rsi_max": None, "rsi_min": None,
            "exit_days": 5, "spread": "bear_put",
            "premium_flag": "RSI>50",
            "premium_wr": 81.0,
            "wr_3d": 60.3, "wr_5d": 64.2, "n": 151, "period": "Oct 25 – Feb 26",
            "notes": ["Hold to 5d", "RSI>50 = premium"],
            "never": ["Exit at 3d", "Score ≥80", "Bull signals"],
        },
    },

    # ────────────────────────────────────────────────────────
    # IWM — CONFIRMED+bear score ≥60. Exit 5d. Highest WR.
    # ETF — fires rarely, take every qualifying signal.
    # ────────────────────────────────────────────────────────
    "IWM": {
        BEAR: {
            "active": True,
            "htf": "CONFIRMED", "bias": "bear",
            "score_min": 60, "score_max": 99,
            "phase": None, "rsi_max": None, "rsi_min": None,
            "exit_days": 5, "spread": "bear_put",
            "premium_flag": None, "premium_wr": None,
            "wr_3d": 92.0, "wr_5d": 93.2, "n": 50, "period": "Mar 1+",
            "notes": [
                "Take EVERY qualifying signal — fires ~2x/day",
                "Hold to expiration (5d)",
                "ETF signals are cleaner than single stock",
            ],
            "never": [
                "Exit at 3d",
                "Take bull signals on IWM in BEAR regime",
                "Take OPPOSING signals on IWM",
                "Exit early unless 75%+ max profit already hit",
            ],
        },
        TRANSITION: {
            "active": True,
            "htf": "CONFIRMED", "bias": "bear",
            "score_min": 60, "score_max": 99,
            "phase": None, "rsi_max": None, "rsi_min": None,
            "exit_days": 5, "spread": "bear_put",
            "premium_flag": None, "premium_wr": None,
            "wr_3d": 69.6, "wr_5d": 73.9, "n": 23, "period": "Oct 25 – Feb 26",
            "notes": ["Hold to 5d"],
            "never": ["Exit at 3d", "Bull signals", "OPPOSING"],
        },
        BULL: {
            "active": True,
            "htf": "CONFIRMED", "bias": "bear",
            "score_min": 60, "score_max": 99,
            "phase": None, "rsi_max": None, "rsi_min": None,
            "exit_days": 5, "spread": "bear_put",
            "premium_flag": None, "premium_wr": None,
            "wr_3d": 69.6, "wr_5d": 73.9, "n": 23, "period": "Oct 25 – Feb 26",
            "notes": ["Hold to 5d"],
            "never": ["Exit at 3d", "OPPOSING"],
        },
    },

    # ────────────────────────────────────────────────────────
    # QQQ — Flips direction by regime.
    # BEAR: CONFIRMED+bear ≥60, morning only, exit 3d (90.8% WR)
    # TRANSITION/BULL: CONFIRMED+bull 60–79, morning only, exit 3d
    # 5d DEGRADES on QQQ in all regimes — always exit at 3d.
    # ────────────────────────────────────────────────────────
    "QQQ": {
        BEAR: {
            "active": True,
            "htf": "CONFIRMED", "bias": "bear",
            "score_min": 60, "score_max": 99,
            "phase": "MORNING", "rsi_max": None, "rsi_min": None,
            "exit_days": 3, "spread": "bear_put",
            "premium_flag": None, "premium_wr": None,
            "wr_3d": 90.8, "wr_5d": 92.3, "n": 131, "period": "Mar 1+",
            "notes": [
                "MORNING signals only — midday/afternoon WR drops to 65%",
                "Exit at 3d — 5d degrades on QQQ",
                "Auto-flipped from bull: no bull signals fire in BEAR regime",
            ],
            "never": [
                "Trade midday or afternoon sessions",
                "Hold past 3d — 5d WR degrades",
                "Take bull signals in BEAR regime",
            ],
        },
        TRANSITION: {
            "active": True,
            "htf": "CONFIRMED", "bias": "bull",
            "score_min": 60, "score_max": 79,
            "phase": "MORNING", "rsi_max": None, "rsi_min": None,
            "exit_days": 3, "spread": "bull_call",
            "premium_flag": None, "premium_wr": None,
            "wr_3d": 77.8, "wr_5d": 72.4, "n": 406, "period": "Apr 25 – Oct 25",
            "notes": [
                "Flipped to BULL — regime has transitioned",
                "MORNING signals only",
                "Exit at 3d — 5d degrades",
            ],
            "never": [
                "Trade midday or afternoon",
                "Hold past 3d",
                "Score ≥80",
            ],
        },
        BULL: {
            "active": True,
            "htf": "CONFIRMED", "bias": "bull",
            "score_min": 60, "score_max": 79,
            "phase": "MORNING", "rsi_max": None, "rsi_min": None,
            "exit_days": 3, "spread": "bull_call",
            "premium_flag": None, "premium_wr": None,
            "wr_3d": 77.8, "wr_5d": 72.4, "n": 406, "period": "Apr 25 – Oct 25",
            "notes": ["MORNING only", "Exit 3d"],
            "never": ["Midday/afternoon", "Hold past 3d", "Score ≥80"],
        },
    },

    # ────────────────────────────────────────────────────────
    # META — CONFIRMED+bear score ≥75 only. Exit 3d.
    # Strict score gate — score 60–74 is below breakeven on META.
    # Fires ~0.3/day — take every one.
    # ────────────────────────────────────────────────────────
    "META": {
        BEAR: {
            "active": True,
            "htf": "CONFIRMED", "bias": "bear",
            "score_min": 75, "score_max": 99,
            "phase": None, "rsi_max": None, "rsi_min": None,
            "exit_days": 3, "spread": "bear_put",
            "premium_flag": None, "premium_wr": None,
            "wr_3d": 100.0, "wr_5d": 100.0, "n": 7, "period": "Mar 1+",
            "notes": [
                "Score ≥75 required — score 60–74 is BELOW breakeven on META",
                "Exit at 3d — 5d degrades",
                "Fires rarely (~1-2/week) — take every qualifying signal",
            ],
            "never": [
                "Take score 60–74 signals on META",
                "Hold past 3d — 5d WR drops",
                "Take bull signals on META",
                "Take OPPOSING signals on META",
            ],
        },
        TRANSITION: {
            "active": True,
            "htf": "CONFIRMED", "bias": "bear",
            "score_min": 75, "score_max": 99,
            "phase": None, "rsi_max": None, "rsi_min": None,
            "exit_days": 3, "spread": "bear_put",
            "premium_flag": None, "premium_wr": None,
            "wr_3d": 58.6, "wr_5d": 51.7, "n": 29, "period": "Oct 25 – Feb 26",
            "notes": ["Score ≥75 only", "Exit 3d"],
            "never": ["Score 60–74", "Hold past 3d", "Bull signals"],
        },
        BULL: {
            "active": True,
            "htf": "CONFIRMED", "bias": "bear",
            "score_min": 75, "score_max": 99,
            "phase": None, "rsi_max": None, "rsi_min": None,
            "exit_days": 3, "spread": "bear_put",
            "premium_flag": None, "premium_wr": None,
            "wr_3d": 58.6, "wr_5d": 51.7, "n": 29, "period": "Oct 25 – Feb 26",
            "notes": ["Score ≥75 only", "Exit 3d"],
            "never": ["Score 60–74", "Hold past 3d", "Bull signals"],
        },
    },

    # ────────────────────────────────────────────────────────
    # TSLA — CONFIRMED+bear score 65–79. Exit 5d ONLY.
    # 3d WR is 62% (below breakeven) — 5d WR is 86%.
    # Do not exit at 3d under any circumstances.
    # ────────────────────────────────────────────────────────
    "TSLA": {
        BEAR: {
            "active": True,
            "htf": "CONFIRMED", "bias": "bear",
            "score_min": 65, "score_max": 79,
            "phase": None, "rsi_max": None, "rsi_min": None,
            "exit_days": 5, "spread": "bear_put",
            "premium_flag": None, "premium_wr": None,
            "wr_3d": 61.5, "wr_5d": 86.4, "n": 28, "period": "Mar 1+",
            "notes": [
                "3d WR is 61.5% — BELOW breakeven. You MUST hold to 5d.",
                "5d WR is 86.4% — the edge only shows at expiration",
                "TSLA is volatile — the spread needs full DTE to work",
            ],
            "never": [
                "Exit at 3d — 61.5% WR loses money",
                "Score ≥80 or score <65",
                "Take bull signals on TSLA",
                "Exit early unless 75%+ max profit already hit",
            ],
        },
        TRANSITION: {
            "active": True,
            "htf": "CONFIRMED", "bias": "bear",
            "score_min": 65, "score_max": 79,
            "phase": None, "rsi_max": None, "rsi_min": None,
            "exit_days": 5, "spread": "bear_put",
            "premium_flag": None, "premium_wr": None,
            "wr_3d": 63.3, "wr_5d": 69.4, "n": 49, "period": "Oct 25 – Feb 26",
            "notes": ["Hold to 5d — 3d is below breakeven"],
            "never": ["Exit at 3d", "Score ≥80 or <65", "Bull signals"],
        },
        BULL: {
            "active": True,
            "htf": "CONFIRMED", "bias": "bear",
            "score_min": 65, "score_max": 79,
            "phase": None, "rsi_max": None, "rsi_min": None,
            "exit_days": 5, "spread": "bear_put",
            "premium_flag": None, "premium_wr": None,
            "wr_3d": 63.3, "wr_5d": 69.4, "n": 49, "period": "Oct 25 – Feb 26",
            "notes": ["Hold to 5d"],
            "never": ["Exit at 3d", "Score ≥80 or <65", "Bull signals"],
        },
    },

    # ────────────────────────────────────────────────────────
    # AAPL — CONVERGING+bull ONLY. Exit 5d. Same in all regimes.
    # 3d WR is 29–61% depending on regime — BELOW breakeven always.
    # 5d WR is 78–100%. This rule never changes by regime.
    # ────────────────────────────────────────────────────────
    "AAPL": {
        BEAR: {
            "active": True,
            "htf": "CONVERGING", "bias": "bull",
            "score_min": 50, "score_max": 99,
            "phase": None, "rsi_max": None, "rsi_min": None,
            "exit_days": 5, "spread": "bull_call",
            "premium_flag": None, "premium_wr": None,
            "wr_3d": 29.4, "wr_5d": 100.0, "n": 29, "period": "Mar 1+",
            "notes": [
                "CONVERGING HTF only — CONFIRMED+bull on AAPL does NOT qualify",
                "3d WR is 29% — FAR below breakeven. Exit at 5d ONLY.",
                "The bear market delays AAPL recoveries — needs full 5d to work",
            ],
            "never": [
                "Exit at 3d — 29% WR loses money badly",
                "Take CONFIRMED+bull on AAPL — only CONVERGING qualifies",
                "Take bear signals on AAPL",
                "Exit early unless 75%+ max profit already hit",
            ],
        },
        TRANSITION: {
            "active": True,
            "htf": "CONVERGING", "bias": "bull",
            "score_min": 50, "score_max": 99,
            "phase": None, "rsi_max": None, "rsi_min": None,
            "exit_days": 3, "spread": "bull_call",
            "premium_flag": None, "premium_wr": None,
            "wr_3d": 74.4, "wr_5d": 69.2, "n": 39, "period": "Oct 25 – Feb 26",
            "notes": [
                "CONVERGING only — same rule as BEAR regime",
                "Exit at 3d in TRANSITION/BULL — 74.4% vs 69.2% at 5d (opposite of BEAR regime)",
                "The 5d rule only applies in BEAR where 3d is 29% and 5d is 100%",
            ],
            "never": ["CONFIRMED+bull on AAPL", "Bear signals"],
        },
        BULL: {
            "active": True,
            "htf": "CONVERGING", "bias": "bull",
            "score_min": 50, "score_max": 99,
            "phase": None, "rsi_max": None, "rsi_min": None,
            "exit_days": 3, "spread": "bull_call",
            "premium_flag": None, "premium_wr": None,
            "wr_3d": 74.4, "wr_5d": 69.2, "n": 39, "period": "Oct 25 – Feb 26",
            "notes": [
                "CONVERGING only",
                "Exit at 3d — 74.4% WR vs 69.2% at 5d",
            ],
            "never": ["CONFIRMED+bull", "Bear signals", "Hold past 3d"],
        },
    },

    # ────────────────────────────────────────────────────────
    # GOOGL — Active in TRANSITION and BULL only.
    # CONFIRMED+bull score 60–79. Exit 3d.
    # In BEAR: 0 signals fire (scanner auto-suppresses).
    # ────────────────────────────────────────────────────────
    "GOOGL": {
        BEAR: {
            "active": False,
            "htf": "CONFIRMED", "bias": "bull",
            "score_min": 60, "score_max": 79,
            "phase": None, "rsi_max": None, "rsi_min": None,
            "exit_days": 3, "spread": "bull_call",
            "premium_flag": None, "premium_wr": None,
            "wr_3d": 0.0, "wr_5d": 0.0, "n": 0, "period": "Mar 1+",
            "notes": ["SUSPENDED in BEAR regime"],
            "never": ["Take any GOOGL signals in BEAR — 0 qualifying signals fire"],
        },
        TRANSITION: {
            "active": True,
            "htf": "CONFIRMED", "bias": "bull",
            "score_min": 60, "score_max": 79,
            "phase": None, "rsi_max": None, "rsi_min": None,
            "exit_days": 3, "spread": "bull_call",
            "premium_flag": None, "premium_wr": None,
            "wr_3d": 73.3, "wr_5d": 70.7, "n": 273, "period": "Oct 25 – Apr 26",
            "notes": ["Reactivated — regime has transitioned", "Exit 3d (5d degrades slightly)"],
            "never": ["Score ≥80", "Hold past 3d", "Bear signals"],
        },
        BULL: {
            "active": True,
            "htf": "CONFIRMED", "bias": "bull",
            "score_min": 60, "score_max": 79,
            "phase": None, "rsi_max": None, "rsi_min": None,
            "exit_days": 3, "spread": "bull_call",
            "premium_flag": None, "premium_wr": None,
            "wr_3d": 73.3, "wr_5d": 70.7, "n": 273, "period": "Oct 25 – Apr 26",
            "notes": ["Exit 3d"],
            "never": ["Score ≥80", "Hold past 3d", "Bear signals"],
        },
    },

    # ────────────────────────────────────────────────────────
    # NVDA — OPPOSING (mean reversion), score NOT gated (inverted).
    # Suspended in BEAR (61% WR in March = borderline).
    # Active in TRANSITION and BULL. Exit 5d.
    # ────────────────────────────────────────────────────────
    "NVDA": {
        BEAR: {
            "active": False,
            "htf": "OPPOSING", "bias": "both",
            "score_min": 0, "score_max": 99,
            "phase": None, "rsi_max": None, "rsi_min": None,
            "exit_days": 5, "spread": "bull_call",  # depends on bias at signal time
            "premium_flag": None, "premium_wr": None,
            "wr_3d": 60.5, "wr_5d": 73.7, "n": 38, "period": "Mar 1+",
            "notes": [
                "SUSPENDED in BEAR — 60.5% WR is below 71% breakeven at 3d",
                "73.7% WR at 5d clears threshold — monitoring monthly",
                "Will reactivate in TRANSITION if monthly WR recovers above 71%",
            ],
            "never": ["Take NVDA signals in BEAR regime until WR confirms"],
        },
        TRANSITION: {
            "active": True,
            "htf": "OPPOSING", "bias": "both",
            "score_min": 0, "score_max": 99,
            "phase": None, "rsi_max": None, "rsi_min": None,
            "exit_days": 5, "spread": None,  # determined by bias at signal time
            "premium_flag": None, "premium_wr": None,
            "wr_3d": 72.3, "wr_5d": 79.2, "n": 101, "period": "Oct 25 – Apr 26",
            "notes": [
                "OPPOSING only — mean reversion play",
                "NO score gate — score is INVERTED on NVDA (lower = better)",
                "Direction can be bull or bear — take either",
                "Exit 5d for best WR",
            ],
            "never": [
                "Gate on score — score is inverted, lower scores outperform",
                "Take CONFIRMED signals on NVDA",
                "Exit at 3d — edge peaks at 5d",
            ],
        },
        BULL: {
            "active": True,
            "htf": "OPPOSING", "bias": "both",
            "score_min": 0, "score_max": 99,
            "phase": None, "rsi_max": None, "rsi_min": None,
            "exit_days": 5, "spread": None,
            "premium_flag": None, "premium_wr": None,
            "wr_3d": 72.3, "wr_5d": 79.2, "n": 101, "period": "Oct 25 – Apr 26",
            "notes": ["OPPOSING only", "No score gate", "Exit 5d"],
            "never": ["Gate on score", "CONFIRMED signals", "Exit 3d"],
        },
    },

    # ────────────────────────────────────────────────────────
    # AMZN — OPPOSING (mean reversion). Exit 3d ONLY.
    # 5d WR collapses to 58% — this is the OPPOSITE of NVDA.
    # Score is inverted on AMZN (like NVDA).
    # ────────────────────────────────────────────────────────
    "AMZN": {
        BEAR: {
            "active": False,
            "htf": "OPPOSING", "bias": "both",
            "score_min": 0, "score_max": 99,
            "phase": None, "rsi_max": None, "rsi_min": None,
            "exit_days": 3, "spread": None,
            "premium_flag": None, "premium_wr": None,
            "wr_3d": 100.0, "wr_5d": 0.0, "n": 10, "period": "Mar 1+",
            "notes": ["Tiny sample (n=10) — monitoring", "3d exit critical"],
            "never": ["Hold past 3d — 5d goes to 0%"],
        },
        TRANSITION: {
            "active": True,
            "htf": "OPPOSING", "bias": "both",
            "score_min": 0, "score_max": 99,
            "phase": None, "rsi_max": None, "rsi_min": None,
            "exit_days": 3, "spread": None,
            "premium_flag": None, "premium_wr": None,
            "wr_3d": 73.4, "wr_5d": 58.4, "n": 82, "period": "Oct 25 – Apr 26",
            "notes": [
                "OPPOSING only — mean reversion",
                "Exit at 3d — NEVER hold to 5d",
                "No score gate — scoring is inverted on AMZN",
            ],
            "never": [
                "Hold past 3d — 5d WR collapses to 58%",
                "Gate on score",
                "CONFIRMED signals",
            ],
        },
        BULL: {
            "active": True,
            "htf": "OPPOSING", "bias": "both",
            "score_min": 0, "score_max": 99,
            "phase": None, "rsi_max": None, "rsi_min": None,
            "exit_days": 3, "spread": None,
            "premium_flag": None, "premium_wr": None,
            "wr_3d": 73.4, "wr_5d": 58.4, "n": 82, "period": "Oct 25 – Apr 26",
            "notes": ["OPPOSING only", "Exit 3d ONLY"],
            "never": ["Hold past 3d", "Gate on score", "CONFIRMED signals"],
        },
    },

    # ────────────────────────────────────────────────────────
    # GLD — SUSPENDED in BEAR (flipped bear signal only 45.5% WR).
    # BULL regime: CONFIRMED+bull 60–79, RSI<65 hard gate. Exit 3d.
    # RSI gradient is monotonic: 83% at RSI<45 → 63% at RSI>75.
    # ────────────────────────────────────────────────────────
    "GLD": {
        BEAR: {
            "active": False,
            "htf": "CONFIRMED", "bias": "bull",
            "score_min": 60, "score_max": 79,
            "phase": None, "rsi_max": 65, "rsi_min": None,
            "exit_days": 3, "spread": "bull_call",
            "premium_flag": None, "premium_wr": None,
            "wr_3d": 36.1, "wr_5d": 18.0, "n": 61, "period": "Mar 1+",
            "notes": [
                "SUSPENDED — GLD down 12.6% since March 1",
                "Bull rule: 36.1% WR (below 65% breakeven)",
                "Bear flip: 45.5% WR (still below breakeven)",
                "Metals in turmoil — wait for direction",
            ],
            "never": ["Any GLD signals in current regime"],
        },
        TRANSITION: {
            "active": False,
            "htf": "CONFIRMED", "bias": "bull",
            "score_min": 60, "score_max": 79,
            "phase": None, "rsi_max": 65, "rsi_min": None,
            "exit_days": 3, "spread": "bull_call",
            "premium_flag": None, "premium_wr": None,
            "wr_3d": 68.9, "wr_5d": 62.7, "n": 161, "period": "Oct 25 – Apr 26",
            "notes": ["Still OFF — wait for BULL regime and GLD reclaiming 20d MA"],
            "never": ["GLD signals until BULL confirmed"],
        },
        BULL: {
            "active": True,
            "htf": "CONFIRMED", "bias": "bull",
            "score_min": 60, "score_max": 79,
            "phase": None, "rsi_max": 65, "rsi_min": None,
            "exit_days": 3, "spread": "bull_call",
            "premium_flag": None, "premium_wr": None,
            "wr_3d": 68.9, "wr_5d": 62.7, "n": 161, "period": "Oct 25 – Apr 26",
            "notes": [
                "RSI<65 is a HARD GATE — skip any signal with RSI≥65",
                "RSI gradient is monotonic: lower RSI = better WR",
                "Exit 3d — 5d degrades on gold",
                "Score ≥80 is hard skip on GLD",
            ],
            "never": [
                "Take RSI≥65 bull signals — hard gate",
                "Score ≥80",
                "Hold past 3d",
                "Take bear signals on GLD in BULL regime",
            ],
        },
    },

    # ────────────────────────────────────────────────────────
    # SLV — SUSPENDED in BEAR and TRANSITION.
    # BULL regime: CONVERGING+bull ALL scores, exit 5d (95.5% WR).
    # Also take CONFIRMED+bull any score in BULL, exit 5d (80.1% WR).
    # Never take bear signals on SLV. Never take score ≥80 CONFIRMED.
    # ────────────────────────────────────────────────────────
    "SLV": {
        BEAR: {
            "active": False,
            "htf": "CONVERGING", "bias": "bull",
            "score_min": 50, "score_max": 99,
            "phase": None, "rsi_max": None, "rsi_min": None,
            "exit_days": 5, "spread": "bull_call",
            "premium_flag": None, "premium_wr": None,
            "wr_3d": 25.5, "wr_5d": 25.5, "n": 51, "period": "Mar 1+",
            "notes": [
                "SUSPENDED — SLV down 17.6% since March 1",
                "Bull rule: 25.5% WR — deeply below breakeven",
                "Bear flip: 51.5% 3d, 68.9% 5d — below 65% threshold at 3d",
            ],
            "never": ["Any SLV signals in BEAR regime"],
        },
        TRANSITION: {
            "active": False,
            "htf": "CONVERGING", "bias": "bull",
            "score_min": 50, "score_max": 99,
            "phase": None, "rsi_max": None, "rsi_min": None,
            "exit_days": 5, "spread": "bull_call",
            "premium_flag": None, "premium_wr": None,
            "wr_3d": 80.3, "wr_5d": 95.5, "n": 66, "period": "Full 2025",
            "notes": ["Still OFF — wait for BULL regime"],
            "never": ["SLV signals until BULL confirmed and CONVERGING+bull firing ≥2/month"],
        },
        BULL: {
            "active": True,
            "htf": ["CONVERGING", "CONFIRMED"], "bias": "bull",
            "score_min": 50, "score_max": 79,  # cap at 79: CONFIRMED+bull ≥80 degrades to 72%
            "phase": None, "rsi_max": None, "rsi_min": None,
            "exit_days": 5, "spread": "bull_call",
            "premium_flag": None, "premium_wr": None,
            "wr_3d": 80.3, "wr_5d": 95.5, "n": 66, "period": "Full 2025",
            "notes": [
                "CONVERGING+bull: take ALL signals regardless of score/RSI/phase (95.5% WR at 5d)",
                "CONFIRMED+bull: also valid, any score, exit 5d (80.1% WR)",
                "Score ≥80 on CONFIRMED degrades to 72% — avoid",
                "Regime warning: if CONVERGING+bull fires <2/month, regime has changed",
                "NEVER take bear signals on SLV",
            ],
            "never": [
                "Any bear signal on SLV — ever",
                "CONFIRMED+bull score ≥80",
                "Exit at 3d — 5d is mandatory",
                "Take OPPOSING signals on SLV",
            ],
        },
    },

}


# ═══════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════

def get_ticker_rule(ticker: str, regime: str) -> Optional[dict]:
    """
    Returns the rule dict for a ticker in the given regime.
    Returns None if the ticker is not in TICKER_RULES.
    The 'active' field inside the rule tells you if it's tradeable.
    """
    return TICKER_RULES.get(ticker.upper(), {}).get(regime)


def is_signal_valid(ticker: str, regime: str, signal: dict) -> bool:
    """
    Returns True if the scanner signal passes all rule filters
    for this ticker in the current regime.

    Checks: active, htf_status, bias, score, phase, rsi gates.
    """
    rule = get_ticker_rule(ticker, regime)
    if rule is None or not rule.get("active", False):
        return False

    htf    = signal.get("htf_status", "UNKNOWN")
    bias   = signal.get("bias", "")
    score  = int(signal.get("score", 0))
    phase  = signal.get("phase", "")
    rsi    = signal.get("rsi_mfi")  # may be None

    # HTF check — rule["htf"] may be a string or a list of allowed statuses
    allowed_htf = rule["htf"] if isinstance(rule["htf"], list) else [rule["htf"]]
    if htf not in allowed_htf:
        return False

    # Bias check (OPPOSING tickers accept both directions)
    if rule["bias"] != "both" and bias != rule["bias"]:
        return False

    # Score check
    if score < rule["score_min"] or score > rule["score_max"]:
        return False

    # Phase check (None = any phase is acceptable)
    if rule["phase"] and phase != rule["phase"]:
        return False

    # RSI gates
    if rsi is not None:
        if rule.get("rsi_max") and rsi >= rule["rsi_max"]:
            return False
        if rule.get("rsi_min") and rsi <= rule["rsi_min"]:
            return False

    return True


def get_premium_flag(ticker: str, regime: str, signal: dict) -> bool:
    """Returns True if this signal meets the premium entry condition."""
    rule = get_ticker_rule(ticker, regime)
    if not rule or not rule.get("premium_flag"):
        return False

    flag = rule["premium_flag"]
    rsi  = signal.get("rsi_mfi")

    if flag == "RSI>50" and rsi is not None:
        return rsi > 50

    return False


def format_exit_date(exit_days: int) -> str:
    """Returns 'Fri Apr 11 (5d)' style string for the alert."""
    target = date.today() + timedelta(days=exit_days)
    # Skip weekends (rough — doesn't account for holidays)
    while target.weekday() > 4:  # Saturday=5, Sunday=6
        target += timedelta(days=1)
    return target.strftime("%a %b %-d") + f" ({exit_days}d)"


def get_active_tickers(regime: str) -> List[str]:
    """Returns list of tickers that have an active rule in the given regime."""
    return [
        ticker for ticker, regimes in TICKER_RULES.items()
        if regimes.get(regime, {}).get("active", False)
    ]


def get_spread_type(ticker: str, regime: str, signal_bias: str) -> str:
    """Returns human-readable spread type."""
    rule = get_ticker_rule(ticker, regime)
    if rule is None:
        # For OPPOSING tickers (NVDA/AMZN), spread depends on signal direction
        if signal_bias == "bull":
            return "Bull Call Spread"
        return "Bear Put Spread"
    spread = rule.get("spread")
    if spread == "bear_put":
        return "Bear Put Spread"
    elif spread == "bull_call":
        return "Bull Call Spread"
    else:
        # OPPOSING tickers — direction determines spread
        if signal_bias == "bull":
            return "Bull Call Spread"
        return "Bear Put Spread"


def build_alert_message(ticker: str, regime: str, signal: dict) -> str:
    """
    Builds the complete, self-contained signal alert message.
    Trader needs nothing else — all rules are inline.
    """
    rule = get_ticker_rule(ticker, regime)
    if rule is None:
        return f"[no rule found for {ticker} in {regime}]"

    bias        = signal.get("bias", "")
    score       = int(signal.get("score", 0))
    htf_status  = signal.get("htf_status", "UNKNOWN")
    rsi         = signal.get("rsi_mfi")
    phase       = signal.get("phase", "")
    spot        = signal.get("close", 0.0)
    vol_ratio   = signal.get("volume_ratio", 1.0)
    tier        = signal.get("tier", "2")
    is_premium  = get_premium_flag(ticker, regime, signal)

    # Header
    regime_emoji = {"BEAR": "🔴", "TRANSITION": "🟡", "BULL": "🟢"}.get(regime, "⚪")
    tier_emoji   = "🥇" if tier == "1" else "🥈"
    dir_emoji    = "🐻" if bias == "bear" else "🐂"
    htf_display  = {
        "CONFIRMED": "✅ CONFIRMED",
        "CONVERGING": "🟡 CONVERGING",
        "OPPOSING": "🔴 OPPOSING",
        "UNKNOWN": "⚪ NO DATA",
    }.get(htf_status, htf_status)

    rsi_str = f"  RSI: {rsi:.1f}" if rsi is not None else ""
    premium_str = " ⭐ PREMIUM" if is_premium else ""
    vol_str = f"  Vol: {vol_ratio:.1f}x avg" if vol_ratio > 1.0 else ""
    phase_str = f"  Phase: {phase} ✅" if rule.get("phase") else ""

    # Spread and exit
    spread_name = get_spread_type(ticker, regime, bias)
    exit_days   = rule["exit_days"]
    exit_date   = format_exit_date(exit_days)
    spread_legs = ("Long ITM put / Short ATM put"
                   if "Put" in spread_name else
                   "Long ITM call / Short ATM call")

    # Score gate display
    if rule["score_max"] < 99:
        score_gate = f"{rule['score_min']}–{rule['score_max']}"
    else:
        score_gate = f"≥{rule['score_min']}"

    # Build message
    divider = "━" * 32

    lines = [
        f"{tier_emoji} {ticker} {bias.upper()} {dir_emoji}  ●  {regime_emoji} {regime} REGIME",
        divider,
        f"Score: {score} (gate: {score_gate})  HTF: {htf_display}{phase_str}",
        f"Price: ${spot:.2f}{rsi_str}{premium_str}{vol_str}",
        "",
        "📋 TRADE INSTRUCTIONS",
        f"Type:   {spread_name}",
        f"Legs:   {spread_legs}",
        f"Width:  $2.50  —  max pay $1.68 (67% of width)",
        f"DTE:    3–5 remaining at entry",
        f"Exit:   Hold to EXPIRATION → {exit_date}",
    ]

    # Premium flag block
    if is_premium and rule.get("premium_flag"):
        lines += [
            "",
            f"⭐ PREMIUM ENTRY — {rule['premium_flag']}",
            f"   WR on premium entries: {rule['premium_wr']:.0f}%  — size up",
        ]

    # 3d warning for tickers that must hold to 5d
    if exit_days == 5 and rule["wr_3d"] < 65.0:
        lines += [
            "",
            f"⚠️  3d WR IS {rule['wr_3d']:.1f}% — BELOW BREAKEVEN",
            f"    You MUST hold to 5d where WR is {rule['wr_5d']:.1f}%",
            f"    Do not let this position expire at 3d.",
        ]

    # Expected performance
    lines += [
        "",
        "📊 BACKTEST PERFORMANCE",
        f"Period: {rule['period']}  (n={rule['n']} signals)",
        f"WR at 3d: {rule['wr_3d']:.1f}%   WR at 5d: {rule['wr_5d']:.1f}%",
    ]

    # Notes
    if rule.get("notes"):
        lines.append("")
        lines.append("📌 NOTES")
        for note in rule["notes"]:
            lines.append(f"  • {note}")

    # Hard never rules
    if rule.get("never"):
        lines.append("")
        lines.append("🚫 DO NOT:")
        for n in rule["never"]:
            lines.append(f"  ✗ {n}")

    lines.append(divider)

    return "\n".join(lines)
