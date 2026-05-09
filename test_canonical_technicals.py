"""
test_canonical_technicals.py — Unit + wrapper-consistency tests for
canonical_technicals.

Wrapper-consistency: every public function in canonical_technicals must
produce byte-identical output to its source-of-truth in active_scanner.
We import the source directly and compare. If the canonical ever drifts,
these tests fail.
"""

from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASSED = []
FAILED = []


def assert_eq(actual, expected, msg):
    if actual != expected:
        FAILED.append(f"{msg}: expected {expected!r}, got {actual!r}")
        return False
    PASSED.append(msg)
    return True


def assert_approx(actual, expected, tol, msg):
    if actual is None or abs(actual - expected) > tol:
        FAILED.append(f"{msg}: expected ~{expected} ±{tol}, got {actual!r}")
        return False
    PASSED.append(msg)
    return True


def assert_is_none(actual, msg):
    if actual is not None:
        FAILED.append(f"{msg}: expected None, got {actual!r}")
        return False
    PASSED.append(msg)
    return True


def assert_true(cond, msg):
    if not cond:
        FAILED.append(f"{msg}: condition was False")
        return False
    PASSED.append(msg)
    return True


# ───────────────────────────────────────────────────────────────────────
# Deterministic synthetic data
# ───────────────────────────────────────────────────────────────────────

def _ramp_closes(start: float, step: float, n: int) -> list:
    return [start + step * i for i in range(n)]


def _alternating_closes(base: float, amp: float, n: int) -> list:
    return [base + amp * (1 if i % 2 == 0 else -1) for i in range(n)]


def _gentle_oscillation(n: int) -> list:
    return [100.0 + 5.0 * math.sin(i / 4.0) for i in range(n)]


# ───────────────────────────────────────────────────────────────────────
# RSI tests
# ───────────────────────────────────────────────────────────────────────

def test_rsi_insufficient_data_returns_none():
    from canonical_technicals import rsi
    assert_is_none(rsi([100.0, 101.0]), "rsi: <period+1 closes returns None")
    assert_is_none(rsi([], 14), "rsi: empty list returns None")
    assert_is_none(rsi(_ramp_closes(100, 1, 14), 14), "rsi: exactly period closes returns None")


def test_rsi_pure_uptrend_returns_100():
    from canonical_technicals import rsi
    closes = _ramp_closes(100.0, 1.0, 30)
    val = rsi(closes, period=14)
    assert_approx(val, 100.0, 1e-9, "rsi: monotonic uptrend → 100")


def test_rsi_pure_downtrend_near_zero():
    from canonical_technicals import rsi
    closes = _ramp_closes(200.0, -1.0, 30)
    val = rsi(closes, period=14)
    assert_true(val is not None and val < 1.0, "rsi: monotonic downtrend → ~0")


def test_rsi_wrapper_consistency_uptrend():
    """Canonical rsi must match active_scanner._compute_rsi byte-for-byte."""
    from canonical_technicals import rsi as canon
    from active_scanner import _compute_rsi as src
    closes = _ramp_closes(100.0, 1.0, 30)
    assert_eq(canon(closes), src(closes),
              "rsi wrapper-consistency: uptrend default period")
    assert_eq(canon(closes, 7), src(closes, 7),
              "rsi wrapper-consistency: uptrend period=7")


def test_rsi_wrapper_consistency_oscillation():
    from canonical_technicals import rsi as canon
    from active_scanner import _compute_rsi as src
    closes = _gentle_oscillation(60)
    assert_eq(canon(closes), src(closes),
              "rsi wrapper-consistency: oscillation default period")
    assert_eq(canon(closes, 21), src(closes, 21),
              "rsi wrapper-consistency: oscillation period=21")


def test_rsi_wrapper_consistency_alternating():
    from canonical_technicals import rsi as canon
    from active_scanner import _compute_rsi as src
    closes = _alternating_closes(100.0, 1.0, 40)
    assert_eq(canon(closes), src(closes),
              "rsi wrapper-consistency: alternating up/down")


# ───────────────────────────────────────────────────────────────────────
# MACD tests
# ───────────────────────────────────────────────────────────────────────

def test_macd_insufficient_data_returns_empty():
    from canonical_technicals import macd
    assert_eq(macd([100.0] * 20), {},
              "macd: <slow+signal closes returns {}")
    assert_eq(macd([]), {}, "macd: empty list returns {}")


def test_macd_returns_required_keys():
    from canonical_technicals import macd
    closes = _ramp_closes(100.0, 0.5, 60)
    out = macd(closes)
    for key in ("macd_line", "signal_line", "macd_hist",
                "macd_cross_bull", "macd_cross_bear"):
        assert_true(key in out, f"macd: result has key {key!r}")


def test_macd_wrapper_consistency_uptrend():
    from canonical_technicals import macd as canon
    from active_scanner import _compute_macd as src
    closes = _ramp_closes(100.0, 0.5, 60)
    assert_eq(canon(closes), src(closes),
              "macd wrapper-consistency: uptrend")


def test_macd_wrapper_consistency_oscillation():
    from canonical_technicals import macd as canon
    from active_scanner import _compute_macd as src
    closes = _gentle_oscillation(80)
    assert_eq(canon(closes), src(closes),
              "macd wrapper-consistency: oscillation")


def test_macd_wrapper_consistency_choppy():
    from canonical_technicals import macd as canon
    from active_scanner import _compute_macd as src
    closes = _alternating_closes(100.0, 1.0, 80)
    assert_eq(canon(closes), src(closes),
              "macd wrapper-consistency: alternating")


def test_macd_wrapper_consistency_minimum_length():
    """Right at the boundary: slow + signal = 26 + 9 = 35 closes."""
    from canonical_technicals import macd as canon
    from active_scanner import _compute_macd as src
    closes = _ramp_closes(100.0, 0.3, 35)
    assert_eq(canon(closes), src(closes),
              "macd wrapper-consistency: 35-close minimum")


def test_ema_wrapper_consistency():
    from canonical_technicals import _ema as canon
    from active_scanner import _compute_ema as src
    values = _ramp_closes(100.0, 1.0, 30)
    assert_eq(canon(values, 12), src(values, 12),
              "_ema wrapper-consistency: ramp period=12")
    assert_eq(canon(values, 26), src(values, 26),
              "_ema wrapper-consistency: ramp period=26")
    assert_eq(canon([1.0, 2.0, 3.0], 5), src([1.0, 2.0, 3.0], 5),
              "_ema wrapper-consistency: insufficient data → []")


# ───────────────────────────────────────────────────────────────────────
# ADX tests
# ───────────────────────────────────────────────────────────────────────

def _ohlc_uptrend(n: int):
    closes = _ramp_closes(100.0, 1.0, n)
    highs = [c + 0.5 for c in closes]
    lows  = [c - 0.5 for c in closes]
    return highs, lows, closes


def _ohlc_choppy(n: int):
    closes = _alternating_closes(100.0, 1.0, n)
    highs = [c + 0.5 for c in closes]
    lows  = [c - 0.5 for c in closes]
    return highs, lows, closes


def _ohlc_oscillation(n: int):
    closes = _gentle_oscillation(n)
    highs = [c + 0.4 for c in closes]
    lows  = [c - 0.4 for c in closes]
    return highs, lows, closes


def test_adx_insufficient_data_returns_zero():
    from canonical_technicals import adx
    h, l, c = _ohlc_uptrend(10)
    assert_eq(adx(h, l, c, 14), 0.0,
              "adx: <length+1 bars returns 0.0")
    assert_eq(adx([], [], [], 14), 0.0,
              "adx: empty arrays return 0.0")


def test_adx_mismatched_array_lengths_returns_zero():
    from canonical_technicals import adx
    closes = _ramp_closes(100.0, 1.0, 30)
    highs  = closes[:25]
    lows   = closes[:25]
    assert_eq(adx(highs, lows, closes, 14), 0.0,
              "adx: mismatched lengths return 0.0")


def test_adx_strong_trend_yields_positive_value():
    from canonical_technicals import adx
    h, l, c = _ohlc_uptrend(80)
    val = adx(h, l, c, 14)
    assert_true(val > 0.0, "adx: strong uptrend yields adx > 0")


def test_adx_wrapper_consistency_uptrend():
    from canonical_technicals import adx as canon
    from active_scanner import _compute_adx as src
    h, l, c = _ohlc_uptrend(80)
    assert_eq(canon(h, l, c), src(h, l, c),
              "adx wrapper-consistency: uptrend default length")
    assert_eq(canon(h, l, c, 7), src(h, l, c, 7),
              "adx wrapper-consistency: uptrend length=7")


def test_adx_wrapper_consistency_choppy():
    from canonical_technicals import adx as canon
    from active_scanner import _compute_adx as src
    h, l, c = _ohlc_choppy(80)
    assert_eq(canon(h, l, c), src(h, l, c),
              "adx wrapper-consistency: choppy")


def test_adx_wrapper_consistency_oscillation():
    from canonical_technicals import adx as canon
    from active_scanner import _compute_adx as src
    h, l, c = _ohlc_oscillation(120)
    assert_eq(canon(h, l, c), src(h, l, c),
              "adx wrapper-consistency: oscillation")


def test_adx_wrapper_consistency_minimum_length():
    """Right at the boundary: length+1 bars."""
    from canonical_technicals import adx as canon
    from active_scanner import _compute_adx as src
    h, l, c = _ohlc_uptrend(15)  # length=14, needs >= 15 bars
    assert_eq(canon(h, l, c, 14), src(h, l, c, 14),
              "adx wrapper-consistency: 15-bar minimum")


def test_rma_wrapper_consistency():
    from canonical_technicals import _rma as canon
    from active_scanner import _rma as src
    values = _ramp_closes(1.0, 0.1, 50)
    assert_eq(canon(values, 14), src(values, 14),
              "_rma wrapper-consistency: ramp length=14")
    assert_eq(canon([], 14), src([], 14),
              "_rma wrapper-consistency: empty list")
    assert_eq(canon([1.0, 2.0], 0), src([1.0, 2.0], 0),
              "_rma wrapper-consistency: length=0 returns []")


# ───────────────────────────────────────────────────────────────────────
# Test runner
# ───────────────────────────────────────────────────────────────────────

def main():
    tests = [
        test_rsi_insufficient_data_returns_none,
        test_rsi_pure_uptrend_returns_100,
        test_rsi_pure_downtrend_near_zero,
        test_rsi_wrapper_consistency_uptrend,
        test_rsi_wrapper_consistency_oscillation,
        test_rsi_wrapper_consistency_alternating,
        test_macd_insufficient_data_returns_empty,
        test_macd_returns_required_keys,
        test_macd_wrapper_consistency_uptrend,
        test_macd_wrapper_consistency_oscillation,
        test_macd_wrapper_consistency_choppy,
        test_macd_wrapper_consistency_minimum_length,
        test_ema_wrapper_consistency,
        test_adx_insufficient_data_returns_zero,
        test_adx_mismatched_array_lengths_returns_zero,
        test_adx_strong_trend_yields_positive_value,
        test_adx_wrapper_consistency_uptrend,
        test_adx_wrapper_consistency_choppy,
        test_adx_wrapper_consistency_oscillation,
        test_adx_wrapper_consistency_minimum_length,
        test_rma_wrapper_consistency,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            FAILED.append(f"{t.__name__}: unexpected exception "
                          f"{type(e).__name__}: {e}")

    print(f"\n{'='*60}")
    print(f"PASSED: {len(PASSED)}")
    for p in PASSED:
        print(f"  ✓ {p}")
    if FAILED:
        print(f"\nFAILED: {len(FAILED)}")
        for f in FAILED:
            print(f"  ✗ {f}")
        sys.exit(1)
    print(f"\n{'='*60}")
    print("All tests passed.")


if __name__ == "__main__":
    main()
