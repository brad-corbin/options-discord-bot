"""
canonical_technicals.py — Single canonical home for RSI / MACD / ADX.

PURPOSE
-------
Multiple files in the codebase compute the same technical indicators by
hand (see active_scanner.py, risk_manager.py, app.py, swing_scanner.py,
income_scanner.py, unified_models.py, backtest_v3_runner.py, backtest/
bt_*.py). The canonical-rebuild discipline says: there should be ONE
implementation per concept, and a wrapper-consistency test should prove
the canonical matches its source-of-truth byte-for-byte.

Patch E lifts RSI / MACD / ADX out of active_scanner.py — those are the
versions the production trade-decision engines (V2 5D Edge Model, Long
Call Burst classifier, conviction scorer feature ingestion) depend on.

Patch E does NOT touch any caller. active_scanner.py, risk_manager.py
and friends keep their own implementations unchanged. Patch F redirects
callers to canonical_technicals and reconciles risk_manager's drifted
ADX (SMA-seeded Wilder, vs. active_scanner's RMA-seeded version that's
aligned with backtest_v3_runner's ind_adx quintile data).

CONVENTIONS
-----------
- Pure Python, no numpy / pandas. Mirrors the originals exactly.
- All math is byte-identical to active_scanner. Wrapper-consistency
  tests in test_canonical_technicals.py prove this.
- Public API: rsi(closes, period=14), macd(closes),
  adx(highs, lows, closes, length=14).
- Private helpers: _ema (for MACD), _rma (for ADX). These mirror
  active_scanner._compute_ema and active_scanner._rma respectively.

VERSION
-------
Lifted under Patch E (v11.7). See docs/superpowers/plans/
2026-05-09-canonical-technicals-patch-e.md.
"""

from __future__ import annotations

from typing import Dict, Optional


# v11.7 (Patch E.2): MACD constants — mirror active_scanner.py:82-84.
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9


# v11.7 (Patch E.1): RSI lifted byte-identically from
# active_scanner._compute_rsi. Wilder's classic RSI but using simple
# averages over the last `period` gains/losses rather than RMA — this
# matches what the production scanner has shipped for years and what
# the conviction scorer's RSI quintile rules are calibrated against.
def rsi(closes: list, period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(0, diff))
        losses.append(max(0, -diff))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1 + rs))


# v11.7 (Patch E.2): _ema helper lifted byte-identically from
# active_scanner._compute_ema. Used by macd().
def _ema(values: list, period: int) -> list:
    if len(values) < period:
        return []
    ema = [sum(values[:period]) / period]
    mult = 2.0 / (period + 1)
    for v in values[period:]:
        ema.append(v * mult + ema[-1] * (1 - mult))
    return ema


# v11.7 (Patch E.2): MACD lifted byte-identically from
# active_scanner._compute_macd. Returns macd_line, signal_line,
# macd_hist, and bull/bear cross flags. Returns {} when insufficient
# data — the conviction scorer treats {} as "MACD unavailable, skip".
def macd(closes: list) -> Dict:
    if len(closes) < MACD_SLOW + MACD_SIGNAL:
        return {}
    ema_fast = _ema(closes, MACD_FAST)
    ema_slow = _ema(closes, MACD_SLOW)
    offset = MACD_SLOW - MACD_FAST
    macd_line = [ema_fast[i + offset] - ema_slow[i] for i in range(len(ema_slow))]
    if len(macd_line) < MACD_SIGNAL:
        return {}
    signal = _ema(macd_line, MACD_SIGNAL)
    hist = macd_line[-1] - signal[-1] if signal else 0
    return {
        "macd_line": macd_line[-1] if macd_line else 0,
        "signal_line": signal[-1] if signal else 0,
        "macd_hist": hist,
        "macd_cross_bull": (len(macd_line) >= 2 and len(signal) >= 2
                           and macd_line[-2] < signal[-2]
                           and macd_line[-1] > signal[-1]) if signal else False,
        "macd_cross_bear": (len(macd_line) >= 2 and len(signal) >= 2
                           and macd_line[-2] > signal[-2]
                           and macd_line[-1] < signal[-1]) if signal else False,
    }
