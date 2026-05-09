"""
test_active_scanner_technicals_delegate.py — Verify the F.2 shim wiring.

Pre-F.2: this test confirms active_scanner._compute_* matches
canonical_technicals.* (already true — Patch E proved byte-identicalness).

Post-F.2: this test verifies the shim wiring — that active_scanner.X is
importable, present in the namespace, and delegates correctly. The
equality assertions become tautological (both paths call the same code),
but the test still catches: broken imports, missing names, accidental
deletion of a delegation wrapper, AttributeError on the canonical_technicals
side, and end-to-end composition via _analyze_ticker.

Math correctness is verified by test_canonical_technicals.py — don't
duplicate that work here.
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


def assert_true(cond, msg):
    if not cond:
        FAILED.append(f"{msg}: condition was False")
        return False
    PASSED.append(msg)
    return True


def assert_no_exception(callable_, msg):
    try:
        result = callable_()
    except Exception as e:
        FAILED.append(f"{msg}: raised {type(e).__name__}: {e}")
        return None
    PASSED.append(msg)
    return result


# ───────────────────────────────────────────────────────────────────────
# Synthetic data — duplicated from test_canonical_technicals.py on purpose.
# Keeping the two test files independent: the canonical tests verify the
# math, this file verifies the shim wiring.
# ───────────────────────────────────────────────────────────────────────

def _ramp_closes(start: float, step: float, n: int) -> list:
    return [start + step * i for i in range(n)]


def _alternating_closes(base: float, amp: float, n: int) -> list:
    return [base + amp * (1 if i % 2 == 0 else -1) for i in range(n)]


def _gentle_oscillation(n: int) -> list:
    return [100.0 + 5.0 * math.sin(i / 4.0) for i in range(n)]


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


# ───────────────────────────────────────────────────────────────────────
# Wrapper-consistency: active_scanner.X == canonical_technicals.X
# ───────────────────────────────────────────────────────────────────────

def test_compute_rsi_matches_canonical():
    from active_scanner import _compute_rsi
    from canonical_technicals import rsi
    closes_a = _ramp_closes(100.0, 1.0, 30)
    closes_b = _gentle_oscillation(60)
    closes_c = _alternating_closes(100.0, 1.0, 40)
    assert_eq(_compute_rsi(closes_a), rsi(closes_a),
              "_compute_rsi delegates: ramp default period")
    assert_eq(_compute_rsi(closes_a, 7), rsi(closes_a, 7),
              "_compute_rsi delegates: ramp period=7")
    assert_eq(_compute_rsi(closes_b), rsi(closes_b),
              "_compute_rsi delegates: oscillation default period")
    assert_eq(_compute_rsi(closes_c, 21), rsi(closes_c, 21),
              "_compute_rsi delegates: alternating period=21")


def test_compute_macd_matches_canonical():
    from active_scanner import _compute_macd
    from canonical_technicals import macd
    closes_a = _ramp_closes(100.0, 0.5, 60)
    closes_b = _gentle_oscillation(80)
    closes_c = _alternating_closes(100.0, 1.0, 80)
    assert_eq(_compute_macd(closes_a), macd(closes_a),
              "_compute_macd delegates: ramp")
    assert_eq(_compute_macd(closes_b), macd(closes_b),
              "_compute_macd delegates: oscillation")
    assert_eq(_compute_macd(closes_c), macd(closes_c),
              "_compute_macd delegates: alternating")


def test_compute_ema_matches_canonical():
    from active_scanner import _compute_ema
    from canonical_technicals import _ema
    values = _ramp_closes(100.0, 1.0, 30)
    assert_eq(_compute_ema(values, 12), _ema(values, 12),
              "_compute_ema delegates: ramp period=12")
    assert_eq(_compute_ema(values, 26), _ema(values, 26),
              "_compute_ema delegates: ramp period=26")
    assert_eq(_compute_ema([1.0, 2.0, 3.0], 5), _ema([1.0, 2.0, 3.0], 5),
              "_compute_ema delegates: insufficient data → []")


def test_compute_adx_matches_canonical():
    from active_scanner import _compute_adx
    from canonical_technicals import adx
    h_up,  l_up,  c_up  = _ohlc_uptrend(80)
    h_ch,  l_ch,  c_ch  = _ohlc_choppy(80)
    assert_eq(_compute_adx(h_up, l_up, c_up), adx(h_up, l_up, c_up),
              "_compute_adx delegates: uptrend default length")
    assert_eq(_compute_adx(h_up, l_up, c_up, 7),
              adx(h_up, l_up, c_up, 7),
              "_compute_adx delegates: uptrend length=7")
    assert_eq(_compute_adx(h_ch, l_ch, c_ch), adx(h_ch, l_ch, c_ch),
              "_compute_adx delegates: choppy")


def test_rma_matches_canonical():
    from active_scanner import _rma as as_rma
    from canonical_technicals import _rma as ct_rma
    values = _ramp_closes(1.0, 0.1, 50)
    assert_eq(as_rma(values, 14), ct_rma(values, 14),
              "_rma delegates: ramp length=14")
    assert_eq(as_rma([], 14), ct_rma([], 14),
              "_rma delegates: empty list")


# ───────────────────────────────────────────────────────────────────────
# End-to-end smoke: _analyze_ticker composes correctly through the shims.
# Asserts no exception; result is None or a dict (matches the function's
# documented return contract). Doesn't assert on signal content — the goal
# is to confirm the shim chain composes, not to verify scanner logic.
# ───────────────────────────────────────────────────────────────────────

def _make_fake_intraday(n_bars: int = 80):
    """Return a callable matching active_scanner's intraday_fn signature."""
    closes = _ramp_closes(100.0, 0.1, n_bars)
    highs  = [c + 0.5 for c in closes]
    lows   = [c - 0.5 for c in closes]
    volumes = [100_000] * n_bars  # liquid enough to bypass low-ADTV filter
    bars = {"c": closes, "h": highs, "l": lows, "v": volumes}

    def fake(ticker, resolution=5, countback=80):
        return bars

    return fake


def _make_fake_daily(n_days: int = 30):
    """Return a callable matching active_scanner's daily_candle_fn signature."""
    closes = _ramp_closes(100.0, 0.5, n_days)

    def fake(ticker, days=30):
        return closes

    return fake


def test_analyze_ticker_smoke():
    """End-to-end: _analyze_ticker runs through every shim without raising.

    Uses ticker='SPY' to bypass the low-ADTV reject path. Synthetic data
    is deterministic and sufficient for RSI/MACD/EMA/ADX to populate.
    Result is allowed to be None (no setup detected) or a dict (signal
    detected) — both are valid. The assertion is simply that the function
    completes without raising and returns the right type.
    """
    from active_scanner import _analyze_ticker
    intraday = _make_fake_intraday(80)
    daily    = _make_fake_daily(30)

    result = assert_no_exception(
        lambda: _analyze_ticker(
            ticker="SPY",
            intraday_fn=intraday,
            daily_candle_fn=daily,
            regime="NORMAL",
        ),
        "_analyze_ticker smoke: runs without raising through all shims",
    )

    assert_true(result is None or isinstance(result, dict),
                "_analyze_ticker smoke: returns None or dict per contract")


# ───────────────────────────────────────────────────────────────────────
# Test runner
# ───────────────────────────────────────────────────────────────────────

def main():
    tests = [
        test_compute_rsi_matches_canonical,
        test_compute_macd_matches_canonical,
        test_compute_ema_matches_canonical,
        test_compute_adx_matches_canonical,
        test_rma_matches_canonical,
        test_analyze_ticker_smoke,
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
