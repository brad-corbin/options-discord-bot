"""
canonical_gamma_flip.py — The single canonical computer of gamma flip price.

PURPOSE
-------
Find the PRICE where dealer net gamma exposure crosses zero — the
geometric anchor for dealer regime classification.

NOTE ON TERMINOLOGY: this function returns an interpolated PRICE, not
a listed strike. The underlying engine sweeps spot across a grid of
prices (typically 121 points at ±25% by default) and uses linear
interpolation between adjacent grid points to find the exact zero
crossing. So a chain with strikes [95, 100, 105] can produce a flip
of 99.89 — between strikes, on the continuous price axis. Callers
should treat the return value as a price, not a strike.

WHY THIS WRAPPER EXISTS
-----------------------
The data_inventory found 165 hits for gamma flip detection across 24 files,
plus 46 hits for `flip_price` (same concept, different name) in 9 more files.
That's the worst fragmentation in the whole codebase for any single computed
concept.

The actual MATH is in `options_exposure.ExposureEngine.gamma_flip()` plus
`InstitutionalExpectationEngine._grid()` (post-Patch-8 IV-aware band). Those
are correct and battle-tested. The fragmentation isn't in the math — it's in
24+ files reaching for "what is this ticker's flip" via 24+ different paths
to the same underlying calculation.

This module provides ONE entry point. Every reader of "gamma flip price"
calls `canonical_gamma_flip()`. As the rebuild progresses, every existing
flip-finding site in the codebase redirects to this function. After enough
redirects, the canonical name `gamma_flip` (not `flip_price`) is the only
name in the codebase, and every value comes from the same code path.

WHAT IT RETURNS
---------------
The price (rounded to 2 decimals) where net dealer GEX crosses zero,
found by sweeping spot across a band and locating the zero-crossing via
linear interpolation between grid points.

Returns `None` when the flip is OUTSIDE the swept band — for high-IV names
like SMCI or low-OI names like SOXX, the flip can be far enough out that
even ±25% sweep doesn't find it. None is the honest answer; engines should
treat that as "regime detection unreliable for this ticker right now"
rather than picking a wrong number.

CONVENTION
----------
Post-Patch-9 (Walk 1E resolution): SqueezeMetrics convention. Dealers long
calls, short puts. The `_exposures` source in `options_exposure.py` produces
GEX with the correct sign by default; this function inherits that.

DEPENDENCIES
------------
- engine_bridge.build_option_rows: chain-dict → OptionRow conversion (canonical)
- options_exposure.ExposureEngine: the GEX computer + zero-crossing logic
- options_exposure._grid (or our local _build_iv_aware_grid): IV-aware band

The grid construction is duplicated locally (~10 lines) rather than
imported because `_grid` is a method on InstitutionalExpectationEngine,
which has unrelated state. Cleaner to inline the small grid math than
to instantiate a heavy class.
"""

from __future__ import annotations

import logging
from typing import Optional

# Lazy imports inside the function so test files can monkeypatch / mock
# without having to import options_exposure (which has a heavy module load).

log = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────────────
# CONSTANTS — band sizing parameters.
# Match production values from options_exposure.py post-Patch-8.
# ───────────────────────────────────────────────────────────────────────

DEFAULT_BAND_PCT = 0.25         # ±25% blanket fallback when no IV context
IV_AWARE_BAND_FLOOR = 0.15      # min ±15% even for low-IV names
IV_AWARE_BAND_CEILING = 0.40    # max ±40% even for high-IV / long-DTE names
DEFAULT_GRID_STEPS = 121        # number of price points in the sweep

# Risk-free rate (matches options_exposure.ExposureEngine default)
DEFAULT_RISK_FREE_RATE = 0.04


def canonical_gamma_flip(
    chain: dict,
    spot: float,
    days_to_exp: float,
    *,
    iv: Optional[float] = None,
    dte_years: Optional[float] = None,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> Optional[float]:
    """
    Find the gamma flip price for a chain.

    This is the SINGLE canonical computer of gamma flip. Every reader of
    "what's this ticker's flip price?" should call this function.

    Args:
        chain:          Raw chain dict-of-arrays from DataRouter (the shape
                        in `RawInputs.chain`). Must include "optionSymbol",
                        "strike", "side", "iv", "openInterest" arrays.
        spot:           Current spot price for the underlying.
        days_to_exp:    Days until the chain's expiration. Used for OptionRow
                        construction. Also used to auto-derive `dte_years`
                        when `iv` is provided without an explicit `dte_years`
                        (see below).
        iv:             Optional. Representative IV (decimal, e.g. 0.30 for 30%).
                        When provided, the sweep band widens to cover ±3σ of
                        expected price movement, clamped to [15%, 40%].
                        When omitted, falls back to ±25% blanket band.
        dte_years:      Optional. Days-to-expiration as a fraction of a year
                        (e.g. 7/365 for a weekly). If `iv` is provided but
                        `dte_years` is None, this is auto-derived from
                        `days_to_exp / 365.0`. Pass explicitly only when you
                        need a different time horizon than the chain's DTE
                        (rare).
        risk_free_rate: Risk-free rate for Greek calculations. Defaults to 0.04.

    Returns:
        The PRICE (rounded to 2 decimals) where net dealer GEX crosses zero,
        found by linear interpolation between grid points. NOT necessarily
        a listed strike — for chain strikes [95, 100, 105], the flip might
        be 99.89, between strikes on the continuous price axis.

        Returns None if the zero crossing is outside the swept band.

    Raises:
        ValueError if spot <= 0 or chain is empty.

    Examples:
        # Blanket band (no IV context — ±25% always)
        flip = canonical_gamma_flip(raw.chain, spot=550, days_to_exp=3)

        # IV-aware band — dte_years auto-derived from days_to_exp
        flip = canonical_gamma_flip(
            raw.chain, spot=46, days_to_exp=3,
            iv=0.65,
        )
        # Equivalent to passing dte_years=3/365.0 explicitly.

        # Override with a different time horizon (rare, e.g. for thesis-DTE
        # context vs chain-DTE context)
        flip = canonical_gamma_flip(
            raw.chain, spot=550, days_to_exp=3,
            iv=0.30, dte_years=21/365,  # use 3-week horizon for band sizing
        )
    """
    if spot <= 0:
        raise ValueError(f"spot must be positive, got {spot}")
    if not chain:
        raise ValueError("chain is empty")
    if days_to_exp <= 0:
        # Permissive: DTE 0 happens on expiration day, just clamp to small positive
        days_to_exp = 0.01

    # Auto-derive dte_years from days_to_exp if iv is provided but dte_years is not.
    # Prevents the silent-fallback footgun where caller passes iv expecting
    # IV-aware widening but accidentally gets the ±25% blanket because they
    # forgot the second argument.
    if iv is not None and dte_years is None:
        dte_years = max(days_to_exp, 0.01) / 365.0

    # Lazy imports — keep module load light.
    try:
        from engine_bridge import build_option_rows
        from options_exposure import ExposureEngine
    except ImportError as e:
        log.error(f"canonical_gamma_flip: required modules not available: {e}")
        return None

    # 1. Convert chain dict-of-arrays → OptionRow list (canonical converter).
    rows = build_option_rows(chain, spot=spot, days_to_exp=days_to_exp)
    if not rows:
        # Could happen if the chain has no rows passing the build_option_rows
        # filters (strike > 0, iv > 0, side in call/put). Genuine empty result.
        log.debug(f"canonical_gamma_flip: build_option_rows returned 0 rows")
        return None

    # 2. Build the price sweep grid.
    grid = _build_grid(spot, iv=iv, dte_years=dte_years)

    # 3. Engine + zero-crossing.
    #    ExposureEngine.gamma_flip(rows, pg=grid) sweeps net GEX across the
    #    grid, finds the first sign change, returns the interpolated zero.
    #    Returns None if no zero crossing is found within the band.
    try:
        engine = ExposureEngine(r=risk_free_rate)
        flip = engine.gamma_flip(rows, pg=grid)
    except Exception as e:
        log.warning(f"canonical_gamma_flip: ExposureEngine failed: {e}")
        return None

    return flip


# ───────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────

def _build_grid(
    spot: float,
    *,
    iv: Optional[float] = None,
    dte_years: Optional[float] = None,
    steps: int = DEFAULT_GRID_STEPS,
) -> list[float]:
    """
    Build the price-sweep grid for finding the gamma flip.

    Mirrors `options_exposure.InstitutionalExpectationEngine._grid()` exactly:
      - If iv and dte_years are both provided: ±3σ band, clamped to [15%, 40%]
      - Otherwise: ±25% blanket band
      - 121 evenly-spaced price points

    Inlined here rather than imported because the canonical `_grid` is a
    method on a heavy class with unrelated state. The math is small enough
    that duplication is cheaper than the coupling.

    NOTE: any change to band logic must be mirrored in
    `options_exposure.InstitutionalExpectationEngine._grid()`. If they
    drift, that's a new fragmentation. Future cleanup: extract `_grid` to
    a free function and have both call sites use it.
    """
    if iv is not None and dte_years is not None and iv > 0 and dte_years > 0:
        sigma = iv * (dte_years ** 0.5)
        pct = max(IV_AWARE_BAND_FLOOR, min(IV_AWARE_BAND_CEILING, 3.0 * sigma))
    else:
        pct = DEFAULT_BAND_PCT

    lo = spot * (1 - pct)
    hi = spot * (1 + pct)
    step = (hi - lo) / max(steps - 1, 1)
    return [round(lo + i * step, 2) for i in range(steps)]


# ───────────────────────────────────────────────────────────────────────
# AST sanity check at module import / direct run
# ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("canonical_gamma_flip.py module loaded.")
    print(f"  DEFAULT_BAND_PCT = {DEFAULT_BAND_PCT}")
    print(f"  IV_AWARE_BAND_FLOOR = {IV_AWARE_BAND_FLOOR}")
    print(f"  IV_AWARE_BAND_CEILING = {IV_AWARE_BAND_CEILING}")
    print(f"  DEFAULT_GRID_STEPS = {DEFAULT_GRID_STEPS}")

    # Verify grid math
    g_blanket = _build_grid(100.0)
    print(f"\nBlanket grid for spot=100: {len(g_blanket)} points, "
          f"range [{g_blanket[0]}, {g_blanket[-1]}]")

    g_iv = _build_grid(100.0, iv=0.30, dte_years=7/365)
    print(f"IV-aware grid for spot=100, iv=30%, dte=7d: {len(g_iv)} points, "
          f"range [{g_iv[0]}, {g_iv[-1]}]")

    g_high_iv = _build_grid(100.0, iv=2.0, dte_years=7/365)
    print(f"High-IV grid (clamped to 40% ceiling): {len(g_high_iv)} points, "
          f"range [{g_high_iv[0]}, {g_high_iv[-1]}]")

    g_low_iv = _build_grid(100.0, iv=0.10, dte_years=1/365)
    print(f"Low-IV grid (clamped to 15% floor): {len(g_low_iv)} points, "
          f"range [{g_low_iv[0]}, {g_low_iv[-1]}]")
