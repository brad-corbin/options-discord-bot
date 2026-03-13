# options_exposure.py
# Black-Scholes Greeks + Dealer Exposure Engine
# Used by the EM card system for accurate GEX, DEX, Vanna, Charm calculations.
# NOTE: Educational/demo code. Not financial advice.

import math
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple

SQRT_2PI = math.sqrt(2.0 * math.pi)


# ─────────────────────────────────────────────────────────
# MATH PRIMITIVES
# ─────────────────────────────────────────────────────────

def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def safe_sqrt(x: float) -> float:
    return math.sqrt(max(x, 1e-12))


def year_fraction(days_to_exp: float) -> float:
    return max(days_to_exp / 365.0, 1e-8)


# ─────────────────────────────────────────────────────────
# BLACK-SCHOLES GREEKS
# ─────────────────────────────────────────────────────────

def bs_d1(S: float, K: float, T: float, r: float, sigma: float) -> float:
    sigma = max(sigma, 1e-8)
    T     = max(T, 1e-8)
    return (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * safe_sqrt(T))


def bs_d2(S: float, K: float, T: float, r: float, sigma: float) -> float:
    return bs_d1(S, K, T, r, sigma) - sigma * safe_sqrt(T)


def bs_delta(option_type: str, S: float, K: float, T: float, r: float, sigma: float) -> float:
    d1 = bs_d1(S, K, T, r, sigma)
    return norm_cdf(d1) if option_type.lower() == "call" else norm_cdf(d1) - 1.0


def bs_gamma(S: float, K: float, T: float, r: float, sigma: float) -> float:
    d1 = bs_d1(S, K, T, r, sigma)
    return norm_pdf(d1) / (S * sigma * safe_sqrt(T))


def bs_vega(S: float, K: float, T: float, r: float, sigma: float) -> float:
    d1 = bs_d1(S, K, T, r, sigma)
    return S * norm_pdf(d1) * safe_sqrt(T)


def bs_vanna(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """d(delta)/d(vol) — how much dealer delta hedge changes when IV moves."""
    sigma = max(sigma, 1e-8)
    T     = max(T, 1e-8)
    d1    = bs_d1(S, K, T, r, sigma)
    vega  = bs_vega(S, K, T, r, sigma)
    return (vega / S) * (1.0 - d1 / (sigma * safe_sqrt(T)))


def bs_charm(option_type: str, S: float, K: float, T: float, r: float, sigma: float) -> float:
    """d(delta)/d(time) — how much dealer delta hedge changes each day (theta decay of delta)."""
    sigma = max(sigma, 1e-8)
    T     = max(T, 1e-8)
    d1    = bs_d1(S, K, T, r, sigma)
    d2    = bs_d2(S, K, T, r, sigma)
    term1 = -norm_pdf(d1) * (2.0 * r * T - d2 * sigma * safe_sqrt(T)) / (2.0 * T * sigma * safe_sqrt(T))
    if option_type.lower() == "call":
        return term1 - r * norm_cdf(d1)
    return term1 + r * norm_cdf(-d1)


# ─────────────────────────────────────────────────────────
# DATA MODEL
# ─────────────────────────────────────────────────────────

@dataclass
class OptionRow:
    option_type:      str           # "call" or "put"
    strike:           float
    days_to_exp:      float
    iv:               float         # annualized, e.g. 0.28
    open_interest:    int
    underlying_price: float
    contract_size:    int   = 100
    volume:           int   = 0
    # Optional pre-computed greeks from data vendor (used if provided, BS fallback otherwise)
    delta: Optional[float] = None
    gamma: Optional[float] = None
    vanna: Optional[float] = None
    charm: Optional[float] = None


# ─────────────────────────────────────────────────────────
# EXPOSURE ENGINE
# ─────────────────────────────────────────────────────────

class ExposureEngine:
    """
    Computes dealer-side exposure across an options chain.

    Convention: customers are assumed to be net long options,
    dealers are net short. This means:
      - Dealers are short gamma on all strikes (they must chase moves)
      - Positive net GEX = dealer long gamma dominant = vol suppression
      - Negative net GEX = dealer short gamma dominant = trending/explosive

    For calls: dealer is short → negative gamma contribution
    For puts:  dealer is short → positive gamma contribution
               (short put = long underlying delta as price falls → stabilizing)

    This matches the standard SpotGamma / GEX convention used by
    institutional flow desks.
    """

    def __init__(self, r: float = 0.04):
        self.r = r

    def _enrich(self, row: OptionRow) -> Dict:
        S     = row.underlying_price
        K     = row.strike
        T     = year_fraction(row.days_to_exp)
        sigma = max(row.iv, 1e-8)
        ot    = row.option_type.lower()

        delta = row.delta if row.delta is not None else bs_delta(ot, S, K, T, self.r, sigma)
        gamma = row.gamma if row.gamma is not None else bs_gamma(S, K, T, self.r, sigma)
        vanna = row.vanna if row.vanna is not None else bs_vanna(S, K, T, self.r, sigma)
        charm = row.charm if row.charm is not None else bs_charm(ot, S, K, T, self.r, sigma)

        return {
            "type":         ot,
            "strike":       K,
            "days_to_exp":  row.days_to_exp,
            "T":            T,
            "iv":           sigma,
            "oi":           row.open_interest,
            "volume":       row.volume,
            "S":            S,
            "contract_size": row.contract_size,
            "delta":        delta,
            "gamma":        gamma,
            "vanna":        vanna,
            "charm":        charm,
        }

    def _exposures(self, d: Dict) -> Dict:
        S   = d["S"]
        oi  = d["oi"]
        mul = d["contract_size"]
        ot  = d["type"]

        # Dealer sign:
        #   short call → sell on rallies → negative delta (bearish hedge)
        #   short put  → buy on drops   → positive delta (bullish hedge)
        dealer_delta_sign = -1 if ot == "call" else +1

        # GEX convention (SpotGamma):
        #   short call → negative GEX (dealers sell rallies → amplifies up moves)
        #   short put  → positive GEX (dealers buy dips    → suppresses down moves)
        dealer_gamma_sign = -1 if ot == "call" else +1

        dex   = dealer_delta_sign * d["delta"] * oi * mul * S
        gex   = dealer_gamma_sign * d["gamma"] * oi * mul * (S ** 2) * 0.01
        vanna = dealer_delta_sign * d["vanna"] * oi * mul * S
        charm = dealer_delta_sign * d["charm"] * oi * mul * S

        return {**d, "dex": dex, "gex": gex, "vanna_exp": vanna, "charm_exp": charm}

    def compute(self, rows: List[OptionRow]) -> Dict:
        enriched = [self._exposures(self._enrich(r)) for r in rows]

        by_strike: Dict[float, Dict] = {}
        for x in enriched:
            K = x["strike"]
            if K not in by_strike:
                by_strike[K] = {"gex": 0.0, "dex": 0.0, "vanna": 0.0, "charm": 0.0,
                                 "call_oi": 0, "put_oi": 0, "call_vol": 0, "put_vol": 0}
            by_strike[K]["gex"]   += x["gex"]
            by_strike[K]["dex"]   += x["dex"]
            by_strike[K]["vanna"] += x["vanna_exp"]
            by_strike[K]["charm"] += x["charm_exp"]
            if x["type"] == "call":
                by_strike[K]["call_oi"]  += x["oi"]
                by_strike[K]["call_vol"] += x["volume"]
            else:
                by_strike[K]["put_oi"]  += x["oi"]
                by_strike[K]["put_vol"] += x["volume"]

        total_gex   = sum(x["gex"]       for x in enriched)
        total_dex   = sum(x["dex"]       for x in enriched)
        total_vanna = sum(x["vanna_exp"] for x in enriched)
        total_charm = sum(x["charm_exp"] for x in enriched)

        call_wall  = max(by_strike, key=lambda k: by_strike[k]["call_oi"],        default=None)
        put_wall   = max(by_strike, key=lambda k: by_strike[k]["put_oi"],         default=None)
        gamma_wall = max(by_strike, key=lambda k: abs(by_strike[k]["gex"]),       default=None)

        return {
            "enriched":  enriched,
            "by_strike": by_strike,
            "net": {
                "gex":   total_gex,
                "dex":   total_dex,
                "vanna": total_vanna,
                "charm": total_charm,
            },
            "walls": {
                "call_wall":  call_wall,
                "put_wall":   put_wall,
                "gamma_wall": gamma_wall,
            },
        }

    def gamma_flip(self, rows: List[OptionRow], price_grid: List[float]) -> Optional[float]:
        """
        Sweep a price grid and find where net GEX crosses zero.
        More accurate than inspecting existing strikes — gives true flip price.
        """
        pts: List[Tuple[float, float]] = []
        for test_spot in price_grid:
            shifted = [
                OptionRow(
                    option_type      = r.option_type,
                    strike           = r.strike,
                    days_to_exp      = r.days_to_exp,
                    iv               = r.iv,
                    open_interest    = r.open_interest,
                    underlying_price = test_spot,
                    contract_size    = r.contract_size,
                    volume           = r.volume,
                )
                for r in rows
            ]
            gex = self.compute(shifted)["net"]["gex"]
            pts.append((test_spot, gex))

        for i in range(1, len(pts)):
            p0, g0 = pts[i - 1]
            p1, g1 = pts[i]
            if g0 == 0:
                return round(p0, 2)
            if g0 * g1 < 0:
                w = abs(g0) / (abs(g0) + abs(g1))
                return round(p0 + (p1 - p0) * w, 2)
        return None


# ─────────────────────────────────────────────────────────
# INTERPRETATION HELPERS
# ─────────────────────────────────────────────────────────

def gex_regime(net_gex: float) -> Dict:
    """Plain-English summary of the GEX regime."""
    if net_gex < 0:
        return {
            "regime":    "NEGATIVE GAMMA",
            "style":     "Trending / Explosive",
            "note":      "Market makers will AMPLIFY moves — they chase price",
            "preferred": "Debit spreads, momentum, breakout plays",
            "avoid":     "Iron condors, mean reversion",
        }
    return {
        "regime":    "POSITIVE GAMMA",
        "style":     "Range-bound / Vol suppression",
        "note":      "Market makers will SUPPRESS moves — they fade extremes",
        "preferred": "Credit spreads, mean reversion, selling premium",
        "avoid":     "Chasing breakouts",
    }


def vanna_charm_context(net_vanna: float, net_charm: float) -> Dict:
    """Plain-English Vanna and Charm flow context."""
    vanna_note = (
        "Vanna tailwind — rising IV will push dealers to buy, supporting price"
        if net_vanna > 0 else
        "Vanna headwind — rising IV will push dealers to sell, pressuring price"
    )
    charm_note = (
        "Charm tailwind — time decay is removing dealer short hedges (mildly bullish)"
        if net_charm > 0 else
        "Charm headwind — time decay is adding dealer short hedges (mildly bearish)"
    )
    return {"vanna": vanna_note, "charm": charm_note}
