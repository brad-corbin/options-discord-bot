# options_exposure.py
# ═══════════════════════════════════════════════════════════════════
# COMPLETE Dealer Exposure + Institutional Expectation Engine
# ═══════════════════════════════════════════════════════════════════
#
# MATH FOUNDATION:
#   • Generalized Black-Scholes (continuous dividend yield q)
#   • Full Greek suite: Delta, Gamma, Vega, Theta, Rho, Vanna, Volga/Vomma,
#     Charm, Veta, Speed, Zomma, Color, Ultima
#   • SVI volatility surface parameterization (raw + multi-expiry interp)
#   • SABR stochastic volatility model (Hagan asymptotic)
#   • Heston stochastic volatility (semi-closed-form via Gauss-Laguerre)
#   • Barone-Adesi-Whaley American option approximation
#   • Implied volatility solver (Brent's method)
#
# EXPOSURE ENGINE:
#   • Dealer GEX, DEX, Vanna, Charm, Volga, Speed, Theta, Rho exposures
#   • Gamma flip / Vanna flip detection
#   • Forward decay simulator (T-step projection)
#   • Multi-expiry term-structure aggregation
#   • Scenario engine (spot × vol shock matrix)
#   • Composite regime classifier
#
# INSTITUTIONAL EXPECTATION ENGINE:
#   • IV surface / skew-aware strike IV
#   • Positioning / flow heuristics (opening vs closing pressure)
#   • Liquidity / market impact model
#   • Volatility risk premium regime (realized vs implied)
#   • Full-stack expectation: bias-adjusted expected move
#   • Strike probability ladder with prob-of-touch
#   • Institutional card text output
#
# NOTE: Educational/demo code. Not financial advice.
# ═══════════════════════════════════════════════════════════════════

import math
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Callable
from enum import Enum

SQRT_2PI = math.sqrt(2.0 * math.pi)
PI       = math.pi


# ╔═══════════════════════════════════════════════════════════════╗
# ║  SECTION 1 — MATH PRIMITIVES                                 ║
# ╚═══════════════════════════════════════════════════════════════╝

def norm_pdf(x: float) -> float:
    """Standard normal probability density function."""
    return math.exp(-0.5 * x * x) / SQRT_2PI


def norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def safe_sqrt(x: float) -> float:
    return math.sqrt(max(x, 1e-12))


def safe_log(x: float) -> float:
    return math.log(max(x, 1e-300))


def year_fraction(days_to_exp: float) -> float:
    return max(days_to_exp / 365.0, 1e-8)


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# Gauss-Laguerre quadrature (20-point) for Heston integration
_GL20_X = [
    0.07053988969198299, 0.37212681800161144, 0.9165821024734295,
    1.7073065310283670, 2.7491992553094321, 4.0489253138508869,
    5.6151749708616165, 7.4590174536710633, 9.5943928695810968,
    12.038802546964820, 14.814293442630737, 17.948895520519376,
    21.478788240285011, 25.451702793186903, 29.932554631700612,
    35.013434240479000, 40.833057056728956, 47.619994047346462,
    55.810795750063898, 66.524416525615754,
]
_GL20_W = [
    0.16874680185111386, 0.29125436200606828, 0.26668610286831735,
    0.16600245326950687, 0.07482581529153363, 0.02491480533585698,
    0.00620874560986777, 0.00114481558318275, 0.00015574177302781,
    0.00001541614988887, 1.0864863665179e-06, 5.3301209095567e-08,
    1.7579811790506e-09, 3.7255024025123e-11, 4.7675292515982e-13,
    3.3728442433624e-15, 1.1550143395397e-17, 1.5395221405044e-20,
    5.2864427255691e-24, 1.6564566124990e-28,
]


# ╔═══════════════════════════════════════════════════════════════╗
# ║  SECTION 2 — GENERALIZED BLACK-SCHOLES GREEKS                ║
# ║  All functions accept continuous dividend yield q             ║
# ╚═══════════════════════════════════════════════════════════════╝

def gbs_d1(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    """d1 for generalized Black-Scholes (with dividend yield q)."""
    sigma = max(sigma, 1e-8)
    T     = max(T, 1e-8)
    return (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * safe_sqrt(T))


def gbs_d2(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    return gbs_d1(S, K, T, r, q, sigma) - sigma * safe_sqrt(T)


# --- Price ---

def gbs_price(option_type: str, S: float, K: float, T: float,
              r: float, q: float, sigma: float) -> float:
    """European option price under generalized Black-Scholes."""
    d1 = gbs_d1(S, K, T, r, q, sigma)
    d2 = d1 - sigma * safe_sqrt(T)
    if option_type.lower() == "call":
        return S * math.exp(-q * T) * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    else:
        return K * math.exp(-r * T) * norm_cdf(-d2) - S * math.exp(-q * T) * norm_cdf(-d1)


# --- First-order Greeks ---

def gbs_delta(option_type: str, S: float, K: float, T: float,
              r: float, q: float, sigma: float) -> float:
    d1 = gbs_d1(S, K, T, r, q, sigma)
    if option_type.lower() == "call":
        return math.exp(-q * T) * norm_cdf(d1)
    return math.exp(-q * T) * (norm_cdf(d1) - 1.0)


def gbs_theta(option_type: str, S: float, K: float, T: float,
              r: float, q: float, sigma: float) -> float:
    """Theta (per calendar day)."""
    T     = max(T, 1e-8)
    sigma = max(sigma, 1e-8)
    d1 = gbs_d1(S, K, T, r, q, sigma)
    d2 = d1 - sigma * safe_sqrt(T)
    common = -(S * math.exp(-q * T) * norm_pdf(d1) * sigma) / (2.0 * safe_sqrt(T))
    if option_type.lower() == "call":
        val = common + q * S * math.exp(-q * T) * norm_cdf(d1) - r * K * math.exp(-r * T) * norm_cdf(d2)
    else:
        val = common - q * S * math.exp(-q * T) * norm_cdf(-d1) + r * K * math.exp(-r * T) * norm_cdf(-d2)
    return val / 365.0


def gbs_rho(option_type: str, S: float, K: float, T: float,
            r: float, q: float, sigma: float) -> float:
    d2 = gbs_d2(S, K, T, r, q, sigma)
    if option_type.lower() == "call":
        return K * T * math.exp(-r * T) * norm_cdf(d2) * 0.01
    return -K * T * math.exp(-r * T) * norm_cdf(-d2) * 0.01


# --- Second-order Greeks ---

def gbs_gamma(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    d1 = gbs_d1(S, K, T, r, q, sigma)
    return math.exp(-q * T) * norm_pdf(d1) / (S * sigma * safe_sqrt(T))


def gbs_vega(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    d1 = gbs_d1(S, K, T, r, q, sigma)
    return S * math.exp(-q * T) * norm_pdf(d1) * safe_sqrt(T) * 0.01  # per 1% vol move


def gbs_vanna(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    """d(delta)/d(vol) = d(vega)/d(spot)."""
    sigma = max(sigma, 1e-8)
    T     = max(T, 1e-8)
    d1 = gbs_d1(S, K, T, r, q, sigma)
    d2 = d1 - sigma * safe_sqrt(T)
    return -math.exp(-q * T) * norm_pdf(d1) * d2 / sigma


def gbs_volga(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    """Vomma / Volga — d(vega)/d(vol). Measures convexity of vega."""
    sigma = max(sigma, 1e-8)
    T     = max(T, 1e-8)
    d1 = gbs_d1(S, K, T, r, q, sigma)
    d2 = d1 - sigma * safe_sqrt(T)
    vega = gbs_vega(S, K, T, r, q, sigma) * 100.0  # un-scale for internal calc
    return vega * d1 * d2 / sigma


def gbs_charm(option_type: str, S: float, K: float, T: float,
              r: float, q: float, sigma: float) -> float:
    """d(delta)/d(time) — charm / delta bleed."""
    sigma = max(sigma, 1e-8)
    T     = max(T, 1e-8)
    d1 = gbs_d1(S, K, T, r, q, sigma)
    d2 = d1 - sigma * safe_sqrt(T)
    pdf_d1 = norm_pdf(d1)
    charm_common = -math.exp(-q * T) * pdf_d1 * (
        2.0 * (r - q) * T - d2 * sigma * safe_sqrt(T)
    ) / (2.0 * T * sigma * safe_sqrt(T))
    if option_type.lower() == "call":
        return charm_common + q * math.exp(-q * T) * norm_cdf(d1)
    return charm_common - q * math.exp(-q * T) * norm_cdf(-d1)


def gbs_veta(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    """d(vega)/d(time) — how vega decays with time."""
    sigma = max(sigma, 1e-8)
    T     = max(T, 1e-8)
    d1 = gbs_d1(S, K, T, r, q, sigma)
    d2 = d1 - sigma * safe_sqrt(T)
    pdf_d1 = norm_pdf(d1)
    return -S * math.exp(-q * T) * pdf_d1 * safe_sqrt(T) * (
        q + ((r - q) * d1) / (sigma * safe_sqrt(T)) - (1.0 + d1 * d2) / (2.0 * T)
    )


# --- Third-order Greeks ---

def gbs_speed(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    """d(gamma)/d(spot) — how gamma shifts as price moves."""
    sigma = max(sigma, 1e-8)
    T     = max(T, 1e-8)
    d1 = gbs_d1(S, K, T, r, q, sigma)
    gamma = gbs_gamma(S, K, T, r, q, sigma)
    return -gamma / S * (d1 / (sigma * safe_sqrt(T)) + 1.0)


def gbs_zomma(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    """d(gamma)/d(vol) — how gamma changes with IV."""
    sigma = max(sigma, 1e-8)
    T     = max(T, 1e-8)
    d1 = gbs_d1(S, K, T, r, q, sigma)
    d2 = d1 - sigma * safe_sqrt(T)
    gamma = gbs_gamma(S, K, T, r, q, sigma)
    return gamma * (d1 * d2 - 1.0) / sigma


def gbs_color(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    """d(gamma)/d(time) — gamma bleed / color."""
    sigma = max(sigma, 1e-8)
    T     = max(T, 1e-8)
    d1 = gbs_d1(S, K, T, r, q, sigma)
    d2 = d1 - sigma * safe_sqrt(T)
    pdf_d1 = norm_pdf(d1)
    return -math.exp(-q * T) * pdf_d1 / (2.0 * S * T * sigma * safe_sqrt(T)) * (
        2.0 * q * T + 1.0 + (2.0 * (r - q) * T - d2 * sigma * safe_sqrt(T))
        / (sigma * safe_sqrt(T)) * d1
    )


def gbs_ultima(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    """d(volga)/d(vol) — third derivative of price w.r.t. vol."""
    sigma = max(sigma, 1e-8)
    T     = max(T, 1e-8)
    d1 = gbs_d1(S, K, T, r, q, sigma)
    d2 = d1 - sigma * safe_sqrt(T)
    vega = gbs_vega(S, K, T, r, q, sigma) * 100.0
    return (-vega / (sigma * sigma)) * (
        d1 * d2 * (1.0 - d1 * d2) + d1 * d1 + d2 * d2
    )


# ─── Backward-compatible wrappers (q=0) for legacy callers ────

def bs_d1(S, K, T, r, sigma):
    return gbs_d1(S, K, T, r, 0.0, sigma)

def bs_d2(S, K, T, r, sigma):
    return gbs_d2(S, K, T, r, 0.0, sigma)

def bs_delta(option_type, S, K, T, r, sigma):
    return gbs_delta(option_type, S, K, T, r, 0.0, sigma)

def bs_gamma(S, K, T, r, sigma):
    return gbs_gamma(S, K, T, r, 0.0, sigma)

def bs_vega(S, K, T, r, sigma):
    return gbs_vega(S, K, T, r, 0.0, sigma)

def bs_vanna(S, K, T, r, sigma):
    return gbs_vanna(S, K, T, r, 0.0, sigma)

def bs_charm(option_type, S, K, T, r, sigma):
    return gbs_charm(option_type, S, K, T, r, 0.0, sigma)


# ╔═══════════════════════════════════════════════════════════════╗
# ║  SECTION 3 — IMPLIED VOLATILITY SOLVER                       ║
# ║  Rational initial guess + Brent's method                     ║
# ╚═══════════════════════════════════════════════════════════════╝

def implied_vol(option_type: str, S: float, K: float, T: float,
                r: float, q: float, market_price: float,
                tol: float = 1e-8, max_iter: int = 100) -> Optional[float]:
    """Solve for implied volatility using Brent's method."""
    intrinsic = max(0.0, (S * math.exp(-q * T) - K * math.exp(-r * T))
                    if option_type.lower() == "call"
                    else (K * math.exp(-r * T) - S * math.exp(-q * T)))
    if market_price <= intrinsic + 1e-10:
        return None

    def objective(sig):
        return gbs_price(option_type, S, K, T, r, q, sig) - market_price

    a, b = 0.001, 5.0
    fa, fb = objective(a), objective(b)
    if fa * fb > 0:
        return None

    c, fc = b, fb
    d = e = b - a
    for _ in range(max_iter):
        if fb * fc > 0:
            c, fc = a, fa
            d = e = b - a
        if abs(fc) < abs(fb):
            a, fa = b, fb
            b, fb = c, fc
            c, fc = a, fa
        tol1 = 2.0 * 1e-15 * abs(b) + 0.5 * tol
        m = 0.5 * (c - b)
        if abs(m) <= tol1 or fb == 0.0:
            return b
        if abs(e) >= tol1 and abs(fa) > abs(fb):
            s = fb / fa
            if a == c:
                p = 2.0 * m * s
                qq = 1.0 - s
            else:
                qq = fa / fc
                rr = fb / fc
                p = s * (2.0 * m * qq * (qq - rr) - (b - a) * (rr - 1.0))
                qq = (qq - 1.0) * (rr - 1.0) * (s - 1.0)
            if p > 0:
                qq = -qq
            p = abs(p)
            if 2.0 * p < min(3.0 * m * qq - abs(tol1 * qq), abs(e * qq)):
                e = d
                d = p / qq
            else:
                d = m
                e = m
        else:
            d = m
            e = m
        a, fa = b, fb
        if abs(d) > tol1:
            b += d
        else:
            b += tol1 if m > 0 else -tol1
        fb = objective(b)
    return b


# ╔═══════════════════════════════════════════════════════════════╗
# ║  SECTION 4 — SVI VOLATILITY SURFACE                          ║
# ╚═══════════════════════════════════════════════════════════════╝

@dataclass
class SVIParams:
    """Raw SVI: w(k) = a + b*(rho*(k-m) + sqrt((k-m)^2 + sigma^2))"""
    a:     float
    b:     float
    rho:   float
    m:     float
    sigma: float

    def total_variance(self, k: float) -> float:
        diff = k - self.m
        return self.a + self.b * (self.rho * diff + math.sqrt(diff * diff + self.sigma * self.sigma))

    def implied_vol_at(self, k: float, T: float) -> float:
        w = self.total_variance(k)
        return safe_sqrt(max(w, 0.0) / max(T, 1e-8))

    def smile(self, strikes: List[float], S: float, T: float,
              r: float, q: float) -> Dict[float, float]:
        F = S * math.exp((r - q) * T)
        return {K: self.implied_vol_at(math.log(K / F), T) for K in strikes}

    def butterfly_arbitrage_free(self) -> bool:
        return self.b * (1.0 + abs(self.rho)) <= 4.0


@dataclass
class SVISurface:
    """Multi-expiry SVI surface. Interpolates total variance between tenors."""
    slices: Dict[float, SVIParams] = field(default_factory=dict)

    def add_slice(self, T: float, params: SVIParams):
        self.slices[T] = params

    def implied_vol(self, K: float, S: float, T: float, r: float, q: float) -> float:
        F = S * math.exp((r - q) * T)
        k = math.log(K / F)
        tenors = sorted(self.slices.keys())
        if not tenors:
            raise ValueError("No SVI slices loaded")
        if T <= tenors[0]:
            return self.slices[tenors[0]].implied_vol_at(k, T)
        if T >= tenors[-1]:
            return self.slices[tenors[-1]].implied_vol_at(k, T)
        for i in range(len(tenors) - 1):
            T0, T1 = tenors[i], tenors[i + 1]
            if T0 <= T <= T1:
                w0 = self.slices[T0].total_variance(k)
                w1 = self.slices[T1].total_variance(k)
                alpha = (T - T0) / (T1 - T0)
                w_interp = w0 * (1.0 - alpha) + w1 * alpha
                return safe_sqrt(max(w_interp, 0.0) / max(T, 1e-8))
        return self.slices[tenors[-1]].implied_vol_at(k, T)


# ╔═══════════════════════════════════════════════════════════════╗
# ║  SECTION 5 — SABR MODEL (Hagan 2002)                         ║
# ╚═══════════════════════════════════════════════════════════════╝

@dataclass
class SABRParams:
    alpha: float
    beta:  float = 0.5
    rho:   float = -0.25
    nu:    float = 0.4

    def implied_vol(self, F: float, K: float, T: float) -> float:
        if abs(F - K) < 1e-10:
            return self._atm_vol(F, T)
        return self._oogf_vol(F, K, T)

    def _atm_vol(self, F: float, T: float) -> float:
        alpha, beta, rho, nu = self.alpha, self.beta, self.rho, self.nu
        Fb = F ** (1.0 - beta)
        term1 = alpha / Fb
        correction = 1.0 + (
            ((1.0 - beta) ** 2 * alpha ** 2) / (24.0 * Fb ** 2)
            + (rho * beta * nu * alpha) / (4.0 * Fb)
            + nu ** 2 * (2.0 - 3.0 * rho ** 2) / 24.0
        ) * T
        return term1 * correction

    def _oogf_vol(self, F: float, K: float, T: float) -> float:
        alpha, beta, rho, nu = self.alpha, self.beta, self.rho, self.nu
        FK = F * K
        FK_beta = FK ** ((1.0 - beta) / 2.0)
        log_FK = math.log(F / K)
        z = (nu / alpha) * FK_beta * log_FK
        if abs(z) < 1e-10:
            xz = 1.0
        else:
            sqrt_term = math.sqrt(1.0 - 2.0 * rho * z + z * z)
            xz = z / math.log((sqrt_term + z - rho) / (1.0 - rho))
        denom = FK_beta * (
            1.0 + (1.0 - beta) ** 2 * log_FK ** 2 / 24.0
            + (1.0 - beta) ** 4 * log_FK ** 4 / 1920.0
        )
        prefix = alpha / denom
        correction = 1.0 + (
            (1.0 - beta) ** 2 * alpha ** 2 / (24.0 * FK_beta ** 2)
            + rho * beta * nu * alpha / (4.0 * FK_beta)
            + nu ** 2 * (2.0 - 3.0 * rho ** 2) / 24.0
        ) * T
        return prefix * xz * correction

    def smile(self, F: float, strikes: List[float], T: float) -> Dict[float, float]:
        return {K: self.implied_vol(F, K, T) for K in strikes}


# ╔═══════════════════════════════════════════════════════════════╗
# ║  SECTION 6 — HESTON STOCHASTIC VOLATILITY MODEL              ║
# ╚═══════════════════════════════════════════════════════════════╝

@dataclass
class HestonParams:
    """dS = (r-q)S dt + sqrt(V)S dW1;  dV = kappa*(theta-V) dt + xi*sqrt(V) dW2"""
    v0:    float
    theta: float
    kappa: float
    xi:    float
    rho:   float

    def characteristic_fn(self, u: complex, S: float, K: float, T: float,
                          r: float, q: float, j: int) -> complex:
        import cmath
        v0, theta, kappa, xi, rho = self.v0, self.theta, self.kappa, self.xi, self.rho
        if j == 1:
            u_adj = u - 1j
            b = kappa - rho * xi
        else:
            u_adj = u
            b = kappa
        alpha = -0.5 * (u_adj * u_adj + 1j * u_adj)
        beta_ = b - rho * xi * 1j * u_adj
        gamma_ = 0.5 * xi * xi
        disc = (beta_ * beta_ - 4.0 * alpha * gamma_)
        d = disc ** 0.5
        r_plus  = (beta_ + d) / (2.0 * gamma_)
        r_minus = (beta_ - d) / (2.0 * gamma_)
        g = r_minus / r_plus if abs(r_plus) > 1e-15 else 0.0
        exp_dT = cmath.exp(-d * T)
        C = kappa * (r_minus * T - (2.0 / xi ** 2) *
                     cmath.log((1.0 - g * exp_dT) / (1.0 - g)))
        D = r_minus * (1.0 - exp_dT) / (1.0 - g * exp_dT)
        log_spot = math.log(S) + (r - q) * T
        return cmath.exp(C * theta + D * v0 + 1j * u * log_spot)

    def price(self, option_type: str, S: float, K: float, T: float,
              r: float, q: float) -> float:
        import cmath
        log_K = math.log(K)
        def integrand(u_real, j):
            u = u_real
            phi = self.characteristic_fn(u - 0.5j, S, K, T, r, q, j)
            return (cmath.exp(-1j * u * log_K) * phi / (1j * u)).real
        P1, P2 = 0.5, 0.5
        for i in range(len(_GL20_X)):
            u = _GL20_X[i]
            w = _GL20_W[i]
            if u < 1e-15:
                continue
            P1 += w * integrand(u, 1) / PI
            P2 += w * integrand(u, 2) / PI
        P1 = clamp(P1, 0.0, 1.0)
        P2 = clamp(P2, 0.0, 1.0)
        call_price = max(S * math.exp(-q * T) * P1 - K * math.exp(-r * T) * P2, 0.0)
        if option_type.lower() == "call":
            return call_price
        return call_price - S * math.exp(-q * T) + K * math.exp(-r * T)

    def implied_vol_bs(self, option_type: str, S: float, K: float, T: float,
                       r: float, q: float) -> Optional[float]:
        hp = self.price(option_type, S, K, T, r, q)
        return implied_vol(option_type, S, K, T, r, q, hp)


# ╔═══════════════════════════════════════════════════════════════╗
# ║  SECTION 7 — BARONE-ADESI-WHALEY AMERICAN APPROXIMATION      ║
# ╚═══════════════════════════════════════════════════════════════╝

def baw_american_price(option_type: str, S: float, K: float, T: float,
                       r: float, q: float, sigma: float) -> float:
    """BAW (1987) quadratic approximation for American options."""
    european = gbs_price(option_type, S, K, T, r, q, sigma)
    if option_type.lower() == "call" and q <= 0.0:
        return european
    if option_type.lower() == "put" and r <= 0.0:
        return european
    T = max(T, 1e-8)
    sigma = max(sigma, 1e-8)
    M  = 2.0 * r / (sigma * sigma)
    N  = 2.0 * (r - q) / (sigma * sigma)
    K_coef = 1.0 - math.exp(-r * T)

    if option_type.lower() == "call":
        q2 = (-(N - 1.0) + math.sqrt((N - 1.0) ** 2 + 4.0 * M / K_coef)) / 2.0
        if q2 <= 0:
            return european
        S_star = _baw_critical_call(S, K, T, r, q, sigma, q2)
        if S >= S_star:
            return S - K
        A2 = (S_star / q2) * (1.0 - math.exp(-q * T) * norm_cdf(
            gbs_d1(S_star, K, T, r, q, sigma)))
        return european + A2 * (S / S_star) ** q2
    else:
        q1 = (-(N - 1.0) - math.sqrt((N - 1.0) ** 2 + 4.0 * M / K_coef)) / 2.0
        if q1 >= 0:
            return european
        S_star = _baw_critical_put(S, K, T, r, q, sigma, q1)
        if S <= S_star:
            return K - S
        A1 = -(S_star / q1) * (1.0 - math.exp(-q * T) * norm_cdf(
            -gbs_d1(S_star, K, T, r, q, sigma)))
        return european + A1 * (S / S_star) ** q1


def _baw_critical_call(S, K, T, r, q, sigma, q2, max_iter=50, tol=1e-6):
    S_star = K * 1.5
    for _ in range(max_iter):
        d1 = gbs_d1(S_star, K, T, r, q, sigma)
        euro_call = gbs_price("call", S_star, K, T, r, q, sigma)
        LHS = euro_call + (1.0 - math.exp(-q * T) * norm_cdf(d1)) * S_star / q2
        RHS = S_star - K
        bi_diff = LHS - RHS
        if abs(bi_diff) < tol:
            break
        slope = max((1.0 - 1.0 / q2) * (1.0 - math.exp(-q * T) * norm_cdf(d1)), 1e-10)
        S_star -= bi_diff / slope
        S_star = max(S_star, K * 1.001)
    return S_star


def _baw_critical_put(S, K, T, r, q, sigma, q1, max_iter=50, tol=1e-6):
    S_star = K * 0.5
    for _ in range(max_iter):
        d1 = gbs_d1(S_star, K, T, r, q, sigma)
        euro_put = gbs_price("put", S_star, K, T, r, q, sigma)
        LHS = euro_put - (1.0 - math.exp(-q * T) * norm_cdf(-d1)) * S_star / q1
        RHS = K - S_star
        bi_diff = LHS - RHS
        if abs(bi_diff) < tol:
            break
        slope = max(-(1.0 - 1.0 / q1) * (1.0 - math.exp(-q * T) * norm_cdf(-d1)), 1e-10)
        S_star -= bi_diff / slope
        S_star = clamp(S_star, 0.001, K * 0.999)
    return S_star


# ╔═══════════════════════════════════════════════════════════════╗
# ║  SECTION 8 — DATA MODEL (UNIFIED)                            ║
# ║  Merges both files: exposure fields + expectation metadata    ║
# ╚═══════════════════════════════════════════════════════════════╝

class ExerciseStyle(Enum):
    EUROPEAN = "european"
    AMERICAN = "american"


@dataclass
class OptionRow:
    option_type:      str                  # "call" or "put"
    strike:           float
    days_to_exp:      float
    iv:               float                # annualized, e.g. 0.28
    open_interest:    int
    underlying_price: float
    contract_size:    int   = 100
    volume:           int   = 0
    dividend_yield:   float = 0.0          # continuous dividend yield q
    exercise_style:   ExerciseStyle = ExerciseStyle.EUROPEAN
    # Market data
    bid:              Optional[float] = None
    ask:              Optional[float] = None
    last:             Optional[float] = None
    oi_change:        Optional[int]   = None  # +/- vs prior session
    # Optional pre-computed greeks from data vendor
    delta:            Optional[float] = None
    gamma:            Optional[float] = None
    vanna:            Optional[float] = None
    charm:            Optional[float] = None
    volga:            Optional[float] = None
    speed:            Optional[float] = None
    theta:            Optional[float] = None
    rho:              Optional[float] = None


@dataclass
class MarketContext:
    """Session + market-level context for the expectation engine."""
    spot:                    float
    risk_free_rate:          float = 0.04
    # Realized vol windows
    realized_vol_20d:        Optional[float] = None
    realized_vol_10d:        Optional[float] = None
    realized_vol_5d:         Optional[float] = None
    # Liquidity inputs
    avg_daily_dollar_volume: Optional[float] = None
    intraday_dollar_volume:  Optional[float] = None
    orderbook_depth_dollars: Optional[float] = None
    bid_ask_spread_pct:      Optional[float] = None
    # Session
    session_progress:        float = 0.50    # 0.0=open, 1.0=close
    is_0dte:                 bool  = False


# ╔═══════════════════════════════════════════════════════════════╗
# ║  SECTION 9 — HELPER METRICS                                   ║
# ║  Probability, expected move, touch approximation              ║
# ╚═══════════════════════════════════════════════════════════════╝

def expected_move_from_iv(S: float, iv: float, T: float) -> float:
    """1-sigma expected move from implied vol."""
    return S * max(iv, 1e-8) * math.sqrt(max(T, 1e-8))


def prob_finish_above(S: float, K: float, T: float, r: float,
                      q: float, sigma: float) -> float:
    d2 = gbs_d2(S, K, T, r, q, sigma)
    return norm_cdf(d2)


def prob_finish_below(S: float, K: float, T: float, r: float,
                      q: float, sigma: float) -> float:
    d2 = gbs_d2(S, K, T, r, q, sigma)
    return norm_cdf(-d2)


def prob_touch_approx(prob_finish_itm: float) -> float:
    """Practical trader approximation: P(touch) ≈ 2 * P(finish ITM)."""
    return clamp(2.0 * prob_finish_itm, 0.0, 1.0)


# ╔═══════════════════════════════════════════════════════════════╗
# ║  SECTION 10 — EXPOSURE ENGINE (COMPLETE)                      ║
# ║  GEX, DEX, Vanna, Charm, Volga, Speed, Theta, Rho exposures  ║
# ╚═══════════════════════════════════════════════════════════════╝

class ExposureEngine:
    """
    Computes dealer-side exposure across an options chain.

    Convention: customers net long, dealers net short.
      - Positive net GEX = vol suppression
      - Negative net GEX = trending/explosive
    Matches SpotGamma / institutional flow desk convention.

    Features:
      - Dividend yield support (q)
      - Full Greek exposure: GEX, DEX, Vanna, Charm, Volga, Speed, Theta, Rho
      - Vol surface integration (SVI, SABR)
      - American option pricing (BAW)
      - OI change tracking
    """

    def __init__(self, r: float = 0.04,
                 vol_surface: Optional[SVISurface] = None,
                 sabr_params: Optional[Dict[float, SABRParams]] = None):
        self.r = r
        self.vol_surface = vol_surface
        self.sabr_params = sabr_params

    def _resolve_iv(self, row: OptionRow) -> float:
        S, K = row.underlying_price, row.strike
        T = year_fraction(row.days_to_exp)
        q = row.dividend_yield
        if self.vol_surface:
            try:
                return self.vol_surface.implied_vol(K, S, T, self.r, q)
            except Exception:
                pass
        if self.sabr_params:
            tenors = sorted(self.sabr_params.keys())
            nearest_T = min(tenors, key=lambda t: abs(t - T), default=None)
            if nearest_T is not None:
                F = S * math.exp((self.r - q) * T)
                return self.sabr_params[nearest_T].implied_vol(F, K, T)
        return max(row.iv, 1e-8)

    def _enrich(self, row: OptionRow) -> Dict:
        S     = row.underlying_price
        K     = row.strike
        T     = year_fraction(row.days_to_exp)
        q     = row.dividend_yield
        sigma = self._resolve_iv(row)
        ot    = row.option_type.lower()

        delta = row.delta if row.delta is not None else gbs_delta(ot, S, K, T, self.r, q, sigma)
        gamma = row.gamma if row.gamma is not None else gbs_gamma(S, K, T, self.r, q, sigma)
        vanna = row.vanna if row.vanna is not None else gbs_vanna(S, K, T, self.r, q, sigma)
        charm = row.charm if row.charm is not None else gbs_charm(ot, S, K, T, self.r, q, sigma)
        volga = row.volga if row.volga is not None else gbs_volga(S, K, T, self.r, q, sigma)
        speed = row.speed if row.speed is not None else gbs_speed(S, K, T, self.r, q, sigma)
        theta = row.theta if row.theta is not None else gbs_theta(ot, S, K, T, self.r, q, sigma)
        rho   = row.rho   if row.rho   is not None else gbs_rho(ot, S, K, T, self.r, q, sigma)

        price = (baw_american_price(ot, S, K, T, self.r, q, sigma)
                 if row.exercise_style == ExerciseStyle.AMERICAN
                 else gbs_price(ot, S, K, T, self.r, q, sigma))

        return {
            "type": ot, "strike": K, "days_to_exp": row.days_to_exp,
            "T": T, "iv": sigma, "oi": row.open_interest, "volume": row.volume,
            "S": S, "q": q, "contract_size": row.contract_size,
            "delta": delta, "gamma": gamma, "vanna": vanna, "charm": charm,
            "volga": volga, "speed": speed, "theta": theta, "rho": rho,
            "price": price, "exercise_style": row.exercise_style.value,
            "bid": row.bid, "ask": row.ask, "last": row.last,
            "oi_change": row.oi_change,
        }

    def _exposures(self, d: Dict) -> Dict:
        S, oi, mul, ot = d["S"], d["oi"], d["contract_size"], d["type"]
        ds = -1 if ot == "call" else +1   # dealer delta sign
        gs = -1 if ot == "call" else +1   # dealer gamma sign

        dex       = ds * d["delta"] * oi * mul * S
        gex       = gs * d["gamma"] * oi * mul * (S ** 2) * 0.01
        vanna_exp = ds * d["vanna"] * oi * mul * S
        charm_exp = ds * d["charm"] * oi * mul * S
        volga_exp = gs * d["volga"] * oi * mul * S
        speed_exp = gs * d["speed"] * oi * mul * (S ** 3) * 0.0001
        theta_exp = ds * d["theta"] * oi * mul
        rho_exp   = ds * d["rho"]   * oi * mul

        return {
            **d,
            "dex": dex, "gex": gex, "vanna_exp": vanna_exp, "charm_exp": charm_exp,
            "volga_exp": volga_exp, "speed_exp": speed_exp,
            "theta_exp": theta_exp, "rho_exp": rho_exp,
        }

    def compute(self, rows: List[OptionRow]) -> Dict:
        enriched = [self._exposures(self._enrich(r)) for r in rows]

        by_strike: Dict[float, Dict] = {}
        for x in enriched:
            K = x["strike"]
            if K not in by_strike:
                by_strike[K] = {
                    "gex": 0.0, "dex": 0.0, "vanna": 0.0, "charm": 0.0,
                    "volga": 0.0, "speed": 0.0, "theta": 0.0, "rho": 0.0,
                    "call_oi": 0, "put_oi": 0, "call_vol": 0, "put_vol": 0,
                    "call_oi_change": 0, "put_oi_change": 0,
                }
            by_strike[K]["gex"]   += x["gex"]
            by_strike[K]["dex"]   += x["dex"]
            by_strike[K]["vanna"] += x["vanna_exp"]
            by_strike[K]["charm"] += x["charm_exp"]
            by_strike[K]["volga"] += x["volga_exp"]
            by_strike[K]["speed"] += x["speed_exp"]
            by_strike[K]["theta"] += x["theta_exp"]
            by_strike[K]["rho"]   += x["rho_exp"]
            if x["type"] == "call":
                by_strike[K]["call_oi"]  += x["oi"]
                by_strike[K]["call_vol"] += x["volume"]
                by_strike[K]["call_oi_change"] += x["oi_change"] or 0
            else:
                by_strike[K]["put_oi"]  += x["oi"]
                by_strike[K]["put_vol"] += x["volume"]
                by_strike[K]["put_oi_change"] += x["oi_change"] or 0

        exp_keys = ["gex", "dex", "vanna_exp", "charm_exp",
                    "volga_exp", "speed_exp", "theta_exp", "rho_exp"]
        net = {k: sum(x[k] for x in enriched) for k in exp_keys}

        call_wall   = max(by_strike, key=lambda k: by_strike[k]["call_oi"],  default=None)
        put_wall    = max(by_strike, key=lambda k: by_strike[k]["put_oi"],   default=None)
        gamma_wall  = max(by_strike, key=lambda k: abs(by_strike[k]["gex"]), default=None)
        vol_trigger = max(by_strike, key=lambda k: abs(by_strike[k]["volga"]), default=None)

        return {
            "enriched":  enriched,
            "by_strike": by_strike,
            "net": {
                "gex":   net["gex"],        "dex":   net["dex"],
                "vanna": net["vanna_exp"],   "charm": net["charm_exp"],
                "volga": net["volga_exp"],   "speed": net["speed_exp"],
                "theta": net["theta_exp"],   "rho":   net["rho_exp"],
            },
            "walls": {
                "call_wall": call_wall, "put_wall": put_wall,
                "gamma_wall": gamma_wall, "vol_trigger": vol_trigger,
            },
        }

    def _make_shifted_row(self, r: OptionRow, test_spot: float,
                          dte_offset: float = 0.0) -> OptionRow:
        """Helper to create a shifted OptionRow for sweeps."""
        return OptionRow(
            option_type=r.option_type, strike=r.strike,
            days_to_exp=max(r.days_to_exp - dte_offset, 0.01),
            iv=r.iv, open_interest=r.open_interest,
            underlying_price=test_spot, contract_size=r.contract_size,
            volume=r.volume, dividend_yield=r.dividend_yield,
            exercise_style=r.exercise_style,
            bid=r.bid, ask=r.ask, last=r.last, oi_change=r.oi_change,
        )

    def _find_zero_crossing(self, pts: List[Tuple[float, float]]) -> Optional[float]:
        """Linear interpolation to find zero-crossing in a (price, value) series."""
        for i in range(1, len(pts)):
            p0, v0 = pts[i - 1]
            p1, v1 = pts[i]
            if v0 == 0:
                return round(p0, 2)
            if v0 * v1 < 0:
                w = abs(v0) / (abs(v0) + abs(v1))
                return round(p0 + (p1 - p0) * w, 2)
        return None

    def _default_grid(self, rows: List[OptionRow]) -> List[float]:
        S_mid = rows[0].underlying_price if rows else 100.0
        return [S_mid * (0.90 + 0.001 * i) for i in range(201)]

    def gamma_flip(self, rows: List[OptionRow],
                   price_grid: Optional[List[float]] = None) -> Optional[float]:
        """Find where net GEX crosses zero."""
        if price_grid is None:
            price_grid = self._default_grid(rows)
        pts = []
        for sp in price_grid:
            shifted = [self._make_shifted_row(r, sp) for r in rows]
            pts.append((sp, self.compute(shifted)["net"]["gex"]))
        return self._find_zero_crossing(pts)

    def vanna_flip(self, rows: List[OptionRow],
                   price_grid: Optional[List[float]] = None) -> Optional[float]:
        """Find where net Vanna exposure crosses zero."""
        if price_grid is None:
            price_grid = self._default_grid(rows)
        pts = []
        for sp in price_grid:
            shifted = [self._make_shifted_row(r, sp) for r in rows]
            pts.append((sp, self.compute(shifted)["net"]["vanna"]))
        return self._find_zero_crossing(pts)


# ╔═══════════════════════════════════════════════════════════════╗
# ║  SECTION 11 — FORWARD DECAY SIMULATOR                         ║
# ╚═══════════════════════════════════════════════════════════════╝

class DecaySimulator:
    """Projects exposure surface forward day-by-day, holding OI constant."""

    def __init__(self, engine: ExposureEngine):
        self.engine = engine

    def project(self, rows: List[OptionRow], days_forward: int = 5,
                step: float = 1.0) -> List[Dict]:
        snapshots = []
        for day in range(days_forward + 1):
            offset = day * step
            adjusted = [self.engine._make_shifted_row(r, r.underlying_price, offset)
                        for r in rows]
            result = self.engine.compute(adjusted)
            flip   = self.engine.gamma_flip(adjusted)
            snapshots.append({
                "day_offset": offset,
                "net_gex":    result["net"]["gex"],
                "net_dex":    result["net"]["dex"],
                "net_vanna":  result["net"]["vanna"],
                "net_charm":  result["net"]["charm"],
                "net_volga":  result["net"]["volga"],
                "gamma_flip": flip,
                "walls":      result["walls"],
            })
        return snapshots


# ╔═══════════════════════════════════════════════════════════════╗
# ║  SECTION 12 — SCENARIO / STRESS-TEST ENGINE                   ║
# ╚═══════════════════════════════════════════════════════════════╝

class ScenarioEngine:
    """Spot × Vol shock matrix for stress testing dealer exposure."""

    def __init__(self, engine: ExposureEngine):
        self.engine = engine

    def spot_vol_matrix(self, rows: List[OptionRow],
                        spot_shocks: Optional[List[float]] = None,
                        vol_shocks: Optional[List[float]] = None,
                        metric: str = "gex") -> Dict:
        if spot_shocks is None:
            spot_shocks = [-0.10, -0.05, -0.02, -0.01, 0.0, 0.01, 0.02, 0.05, 0.10]
        if vol_shocks is None:
            vol_shocks = [-0.10, -0.05, 0.0, 0.05, 0.10, 0.20]
        S0 = rows[0].underlying_price if rows else 100.0
        matrix = []
        for ds in spot_shocks:
            row_vals = []
            for dv in vol_shocks:
                shocked = [
                    OptionRow(
                        option_type=r.option_type, strike=r.strike,
                        days_to_exp=r.days_to_exp, iv=max(r.iv + dv, 0.01),
                        open_interest=r.open_interest,
                        underlying_price=r.underlying_price * (1.0 + ds),
                        contract_size=r.contract_size, volume=r.volume,
                        dividend_yield=r.dividend_yield,
                        exercise_style=r.exercise_style,
                        bid=r.bid, ask=r.ask, last=r.last, oi_change=r.oi_change,
                    )
                    for r in rows
                ]
                result = self.engine.compute(shocked)
                row_vals.append(result["net"].get(metric, 0.0))
            matrix.append(row_vals)
        return {
            "spot_shocks": spot_shocks, "vol_shocks": vol_shocks,
            "metric": metric, "base_spot": S0, "matrix": matrix,
        }


# ╔═══════════════════════════════════════════════════════════════╗
# ║  SECTION 13 — TERM STRUCTURE AGGREGATION                      ║
# ╚═══════════════════════════════════════════════════════════════╝

def term_structure(rows: List[OptionRow], engine: ExposureEngine,
                   buckets: Optional[List[Tuple[float, float]]] = None) -> List[Dict]:
    """Aggregate exposures by DTE bucket."""
    if buckets is None:
        buckets = [(0, 2), (2, 7), (7, 30), (30, 90), (90, 9999)]
    results = []
    for lo, hi in buckets:
        bucket_rows = [r for r in rows if lo <= r.days_to_exp < hi]
        if not bucket_rows:
            results.append({
                "bucket": f"{lo}-{hi}d", "count": 0,
                "net_gex": 0.0, "net_dex": 0.0,
                "net_vanna": 0.0, "net_charm": 0.0, "net_volga": 0.0,
            })
            continue
        comp = engine.compute(bucket_rows)
        results.append({
            "bucket":    f"{lo}-{hi}d", "count": len(bucket_rows),
            "net_gex":   comp["net"]["gex"],   "net_dex":   comp["net"]["dex"],
            "net_vanna": comp["net"]["vanna"], "net_charm": comp["net"]["charm"],
            "net_volga": comp["net"]["volga"],
        })
    return results


# ╔═══════════════════════════════════════════════════════════════╗
# ║  SECTION 14 — REGIME CLASSIFIER (ENHANCED)                    ║
# ║  Composite scoring from GEX + Vanna + Charm + Volga           ║
# ╚═══════════════════════════════════════════════════════════════╝

def gex_regime(net_gex: float) -> Dict:
    if net_gex < 0:
        return {
            "regime": "NEGATIVE GAMMA", "style": "Trending / Explosive",
            "note": "Market makers will AMPLIFY moves — they chase price",
            "preferred": "Debit spreads, momentum, breakout plays",
            "avoid": "Iron condors, mean reversion",
        }
    return {
        "regime": "POSITIVE GAMMA", "style": "Range-bound / Vol suppression",
        "note": "Market makers will SUPPRESS moves — they fade extremes",
        "preferred": "Credit spreads, mean reversion, selling premium",
        "avoid": "Chasing breakouts",
    }


def vanna_charm_context(net_vanna: float, net_charm: float) -> Dict:
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


def composite_regime(net: Dict) -> Dict:
    """Composite score: -100 (bearish/explosive) to +100 (bullish/pinned)."""
    gex_s   = 40 if net["gex"] > 0 else (-40 if net["gex"] < 0 else 0)
    vanna_s = 25 if net["vanna"] > 0 else (-25 if net["vanna"] < 0 else 0)
    charm_s = 20 if net["charm"] > 0 else (-20 if net["charm"] < 0 else 0)
    volga_s = 15 if net.get("volga", 0) > 0 else (-15 if net.get("volga", 0) < 0 else 0)
    total = gex_s + vanna_s + charm_s + volga_s

    if total >= 50:
        regime, style = "STRONG PIN", "Tight range, vol crush likely"
        plays = "Iron condors, butterflies, selling straddles"
    elif total >= 20:
        regime, style = "MODERATE PIN", "Range-bound with mild directional drift"
        plays = "Credit spreads, ratio writes"
    elif total >= -20:
        regime, style = "NEUTRAL / TRANSITION", "Mixed signals — regime may be shifting"
        plays = "Straddles, calendar spreads, wait for clarity"
    elif total >= -50:
        regime, style = "MODERATE TREND", "Directional bias building, vol expanding"
        plays = "Debit spreads, directional risk reversals"
    else:
        regime, style = "STRONG TREND / EXPLOSIVE", "High conviction trending, vol explosion risk"
        plays = "Momentum trades, long gamma, breakout entries"

    return {
        "composite_score": total, "regime": regime, "style": style,
        "preferred_plays": plays,
        "components": {
            "gex_score": gex_s, "vanna_score": vanna_s,
            "charm_score": charm_s, "volga_score": volga_s,
        },
        "context": {
            **gex_regime(net["gex"]),
            **vanna_charm_context(net["vanna"], net["charm"]),
        },
    }


# ╔═══════════════════════════════════════════════════════════════╗
# ║  SECTION 15 — IV SURFACE / SKEW-AWARE STRIKE IV               ║
# ║  (from institutional_expectations.py)                          ║
# ╚═══════════════════════════════════════════════════════════════╝

class IVSurface:
    """
    Practical skew-aware IV surface built directly from chain rows.
    Uses call-side IV for upside strikes, put-side IV for downside,
    blended ATM. Falls back to nearest strikes by OI weighting.
    """

    def __init__(self, rows: List[OptionRow]):
        self.rows = rows

    def representative_iv(self, spot: float) -> float:
        """OI-weighted, moneyness-adjusted representative IV."""
        weighted_sum = 0.0
        total_w = 0.0
        for r in self.rows:
            moneyness_penalty = abs(r.strike - spot) / max(spot, 1e-8)
            dist_weight = 1.0 / (1.0 + 10.0 * moneyness_penalty)
            time_weight = 1.0 / max(r.days_to_exp, 1.0)
            oi_weight = max(r.open_interest, 1)
            w = dist_weight * time_weight * oi_weight
            weighted_sum += max(r.iv, 1e-8) * w
            total_w += w
        return weighted_sum / max(total_w, 1e-8)

    def representative_dte(self) -> float:
        """OI-weighted average DTE."""
        num, den = 0.0, 0.0
        for r in self.rows:
            w = max(r.open_interest, 1)
            num += r.days_to_exp * w
            den += w
        return num / max(den, 1e-8)

    def strike_iv(self, strike: float, spot: float) -> float:
        """Skew-aware IV: call-side for upside, put-side for downside, blended ATM."""
        if not self.rows:
            return 0.0
        near = sorted(self.rows, key=lambda r: abs(r.strike - strike))
        near_atm = sorted(self.rows, key=lambda r: abs(r.strike - spot))

        if abs(strike - spot) / max(spot, 1e-8) <= 0.005:
            pool = near_atm[:6]
            return sum(max(r.iv, 1e-8) * max(r.open_interest, 1) for r in pool) / max(
                sum(max(r.open_interest, 1) for r in pool), 1e-8)

        side = "call" if strike > spot else "put"
        side_rows = [r for r in near if r.option_type.lower() == side]
        pool = side_rows[:4] if side_rows else near[:4]
        num = sum(max(r.iv, 1e-8) * max(r.open_interest, 1) for r in pool)
        den = sum(max(r.open_interest, 1) for r in pool)
        return num / max(den, 1e-8)


# ╔═══════════════════════════════════════════════════════════════╗
# ║  SECTION 16 — POSITIONING / FLOW HEURISTICS                   ║
# ╚═══════════════════════════════════════════════════════════════╝

class PositioningEngine:
    """Estimates whether today's activity is opening, closing, or churn."""

    @staticmethod
    def estimate_opening_pressure(rows: List[OptionRow]) -> Dict:
        call_open_score = 0.0
        put_open_score  = 0.0
        total_score     = 0.0

        for r in rows:
            oi = max(r.open_interest, 1)
            vol_oi = r.volume / oi
            if r.oi_change is not None:
                open_ratio = clamp(r.oi_change / max(r.volume, 1), -1.0, 1.0)
                score = 0.70 * open_ratio + 0.30 * clamp(vol_oi / 2.0, 0.0, 1.0)
            else:
                score = clamp((vol_oi - 0.5) / 1.5, -1.0, 1.0)
            weighted_score = score * max(r.volume, 1)
            if r.option_type.lower() == "call":
                call_open_score += weighted_score
            else:
                put_open_score += weighted_score
            total_score += abs(weighted_score)

        call_norm = call_open_score / max(total_score, 1.0)
        put_norm  = put_open_score  / max(total_score, 1.0)
        net_norm  = call_norm - put_norm

        if net_norm > 0.15:
            label = "CALL OPENING DOMINANT"
        elif net_norm < -0.15:
            label = "PUT OPENING DOMINANT"
        else:
            label = "MIXED / CHURN"

        return {
            "call_open_score":  round(call_norm, 4),
            "put_open_score":   round(put_norm, 4),
            "net_opening_bias": round(net_norm, 4),
            "label":            label,
        }


# ╔═══════════════════════════════════════════════════════════════╗
# ║  SECTION 17 — LIQUIDITY / MARKET IMPACT MODEL                 ║
# ╚═══════════════════════════════════════════════════════════════╝

class LiquidityEngine:
    """Converts dealer hedge pressure + order book state → move multiplier."""

    @staticmethod
    def liquidity_score(ctx: MarketContext) -> float:
        score = 1.0
        adv      = ctx.avg_daily_dollar_volume
        intraday = ctx.intraday_dollar_volume
        depth    = ctx.orderbook_depth_dollars
        spread   = ctx.bid_ask_spread_pct

        if adv is not None:
            if adv >= 80e9:      score *= 0.80
            elif adv >= 25e9:    score *= 0.90
            elif adv >= 5e9:     score *= 1.00
            else:                score *= 1.10

        if intraday is not None and adv is not None:
            participation = intraday / max(adv, 1.0)
            if participation < 0.15:   score *= 1.10
            elif participation > 0.55: score *= 0.92

        if depth is not None:
            if depth >= 2e9:     score *= 0.85
            elif depth >= 500e6: score *= 0.95
            else:                score *= 1.08

        if spread is not None:
            if spread > 0.003:   score *= 1.08
            elif spread < 0.001: score *= 0.96

        return clamp(score, 0.75, 1.30)

    @staticmethod
    def flow_impact_multiplier(net: Dict, spot: float) -> float:
        """Flow state → expansion/compression factor on expected move."""
        gex_c   = clamp(-net["gex"]   / max((spot ** 2) * 80_000.0, 1.0), -0.35, 0.35)
        vanna_c = clamp( net["vanna"] / max(spot * 8_000_000.0, 1.0),     -0.12, 0.12)
        charm_c = clamp( net["charm"] / max(spot * 8_000_000.0, 1.0),     -0.10, 0.10)
        return clamp(1.0 + gex_c + 0.5 * vanna_c + 0.4 * charm_c, 0.70, 1.45)


# ╔═══════════════════════════════════════════════════════════════╗
# ║  SECTION 18 — VOLATILITY RISK PREMIUM REGIME                  ║
# ╚═══════════════════════════════════════════════════════════════╝

class VolatilityRegimeEngine:
    @staticmethod
    def compute_vrp(iv: float, realized_vol: Optional[float]) -> Optional[float]:
        return (iv - realized_vol) if realized_vol is not None else None

    @staticmethod
    def regime_label(iv: float, realized_vol: Optional[float]) -> str:
        if realized_vol is None:
            return "UNKNOWN RV"
        vrp = iv - realized_vol
        if vrp > 0.08:  return "IV RICH / PREMIUM SELLING REGIME"
        if vrp < -0.03: return "IV CHEAP / BREAKOUT RISK"
        return "BALANCED VOL REGIME"

    @staticmethod
    def regime_multiplier(iv: float, realized_vol: Optional[float]) -> float:
        if realized_vol is None:
            return 1.0
        vrp = iv - realized_vol
        if vrp > 0.10: return 0.92
        if vrp > 0.04: return 0.96
        if vrp < -0.05: return 1.10
        if vrp < 0.0:  return 1.04
        return 1.0


# ╔═══════════════════════════════════════════════════════════════╗
# ║  SECTION 19 — INSTITUTIONAL EXPECTATION ENGINE                 ║
# ║  Full-stack: exposure → adjustment → expected move → card      ║
# ╚═══════════════════════════════════════════════════════════════╝

class InstitutionalExpectationEngine:
    """
    Full-stack expectation model:
      1) Implied move from chain IV
      2) Dealer exposure (full Greek suite via ExposureEngine)
      3) Skew-aware strike probabilities
      4) Positioning / opening pressure
      5) Liquidity adjustment
      6) Volatility risk premium regime
      7) Gamma/Vanna flip and wall context
      8) Composite regime scoring
      9) Forward decay path
    """

    def __init__(self, r: float = 0.04,
                 vol_surface: Optional[SVISurface] = None,
                 sabr_params: Optional[Dict[float, SABRParams]] = None):
        self.r = r
        self.exposure_engine = ExposureEngine(
            r=r, vol_surface=vol_surface, sabr_params=sabr_params)

    @staticmethod
    def _bias_label(score: float) -> str:
        if score >= 0.35:   return "UPSIDE"
        if score <= -0.35:  return "DOWNSIDE"
        return "NEUTRAL"

    @staticmethod
    def _gex_regime_label(net_gex: float) -> str:
        return "NEGATIVE GAMMA / TRENDING" if net_gex < 0 else "POSITIVE GAMMA / SUPPRESSIVE"

    def _directional_pressure_score(self, net: Dict, spot: float,
                                    positioning: Dict) -> float:
        dex_c   = clamp(net["dex"]   / max(spot * 6e6, 1.0), -0.55, 0.55)
        vanna_c = clamp(net["vanna"] / max(spot * 6e6, 1.0), -0.25, 0.25)
        charm_c = clamp(net["charm"] / max(spot * 6e6, 1.0), -0.20, 0.20)
        pos_c   = clamp(positioning["net_opening_bias"],       -0.25, 0.25)
        raw = 0.45 * dex_c + 0.25 * vanna_c + 0.15 * charm_c + 0.15 * pos_c
        return clamp(raw, -1.0, 1.0)

    def _center_shift(self, base_move: float, pressure_score: float) -> float:
        return base_move * 0.40 * pressure_score

    def _build_price_grid(self, spot: float, pct_width: float = 0.12,
                          steps: int = 121) -> List[float]:
        low, high = spot * (1.0 - pct_width), spot * (1.0 + pct_width)
        step = (high - low) / max(steps - 1, 1)
        return [round(low + i * step, 2) for i in range(steps)]

    def _strike_ladder(self, rows: List[OptionRow], ctx: MarketContext,
                       iv_surface: IVSurface) -> List[Dict]:
        unique_strikes = sorted({r.strike for r in rows})
        ladder = []
        rep_dte = iv_surface.representative_dte()
        T = year_fraction(rep_dte)
        q_avg = sum(r.dividend_yield for r in rows) / max(len(rows), 1)

        for K in unique_strikes:
            strike_iv = iv_surface.strike_iv(K, ctx.spot)
            p_above = prob_finish_above(ctx.spot, K, T, self.r, q_avg, strike_iv)
            p_below = prob_finish_below(ctx.spot, K, T, self.r, q_avg, strike_iv)
            p_itm = p_above if K > ctx.spot else p_below
            p_touch = prob_touch_approx(p_itm)
            ladder.append({
                "strike": round(K, 2),
                "iv_used": round(strike_iv, 4),
                "prob_finish_above": round(p_above, 4),
                "prob_finish_below": round(p_below, 4),
                "prob_touch": round(p_touch, 4),
            })
        return ladder

    def snapshot(self, rows: List[OptionRow], ctx: MarketContext) -> Dict:
        """Full institutional snapshot: exposures, expected move, regime, ladder."""
        if not rows:
            return {}

        # Normalize underlying to context spot
        normalized_rows = [
            OptionRow(
                option_type=r.option_type, strike=r.strike,
                days_to_exp=r.days_to_exp, iv=r.iv,
                open_interest=r.open_interest, underlying_price=ctx.spot,
                contract_size=r.contract_size, volume=r.volume,
                dividend_yield=r.dividend_yield, exercise_style=r.exercise_style,
                delta=r.delta, gamma=r.gamma, vanna=r.vanna, charm=r.charm,
                volga=r.volga, speed=r.speed, theta=r.theta, rho=r.rho,
                bid=r.bid, ask=r.ask, last=r.last, oi_change=r.oi_change,
            )
            for r in rows
        ]

        exposure    = self.exposure_engine.compute(normalized_rows)
        iv_surface  = IVSurface(normalized_rows)
        positioning = PositioningEngine.estimate_opening_pressure(normalized_rows)

        rep_iv  = iv_surface.representative_iv(ctx.spot)
        rep_dte = iv_surface.representative_dte()
        T       = year_fraction(rep_dte)

        # Base expected move
        raw_1sigma = expected_move_from_iv(ctx.spot, rep_iv, T)
        raw_2sigma = raw_1sigma * 2.0

        # Flow / liquidity / VRP adjustments
        flow_mult = LiquidityEngine.flow_impact_multiplier(exposure["net"], ctx.spot)
        liq_mult  = LiquidityEngine.liquidity_score(ctx)
        rv_mult   = VolatilityRegimeEngine.regime_multiplier(rep_iv, ctx.realized_vol_20d)

        session_mult = 1.0
        if ctx.is_0dte:
            session_mult = 0.85 + 0.30 * clamp(1.0 - ctx.session_progress, 0.0, 1.0)

        adjusted_1sigma = raw_1sigma * flow_mult * liq_mult * rv_mult * session_mult
        adjusted_2sigma = adjusted_1sigma * 2.0

        pressure_score = self._directional_pressure_score(
            exposure["net"], ctx.spot, positioning)
        bias = self._bias_label(pressure_score)
        center_shift = self._center_shift(adjusted_1sigma, pressure_score)
        expected_center = ctx.spot + center_shift

        # Flip points
        grid       = self._build_price_grid(ctx.spot)
        gamma_flip = self.exposure_engine.gamma_flip(normalized_rows, grid)
        vanna_flip = self.exposure_engine.vanna_flip(normalized_rows, grid)

        # Strike ladder
        ladder = self._strike_ladder(normalized_rows, ctx, iv_surface)

        # Walls
        by_strike  = exposure["by_strike"]
        walls      = exposure["walls"]

        def find_ladder(strike):
            if strike is None:
                return None
            for x in ladder:
                if abs(x["strike"] - strike) < 1e-9:
                    return x
            return None

        # VRP
        vrp20 = VolatilityRegimeEngine.compute_vrp(rep_iv, ctx.realized_vol_20d)
        vrp10 = VolatilityRegimeEngine.compute_vrp(rep_iv, ctx.realized_vol_10d)
        vrp5  = VolatilityRegimeEngine.compute_vrp(rep_iv, ctx.realized_vol_5d)

        # Composite regime (uses full Greek net)
        regime = composite_regime(exposure["net"])

        # Decay path
        decay_path = DecaySimulator(self.exposure_engine).project(normalized_rows, 5)

        # Term structure
        ts = term_structure(normalized_rows, self.exposure_engine)

        return {
            "spot": round(ctx.spot, 2),
            "representative_iv": round(rep_iv, 4),
            "representative_dte": round(rep_dte, 2),

            "raw_expected_move": {
                "move_1sigma": round(raw_1sigma, 2),
                "move_2sigma": round(raw_2sigma, 2),
                "range_1sigma": {
                    "low":  round(ctx.spot - raw_1sigma, 2),
                    "high": round(ctx.spot + raw_1sigma, 2),
                },
                "range_2sigma": {
                    "low":  round(ctx.spot - raw_2sigma, 2),
                    "high": round(ctx.spot + raw_2sigma, 2),
                },
            },

            "adjusted_expectation": {
                "move_1sigma": round(adjusted_1sigma, 2),
                "move_2sigma": round(adjusted_2sigma, 2),
                "expected_center": round(expected_center, 2),
                "expected_low":  round(expected_center - adjusted_1sigma, 2),
                "expected_high": round(expected_center + adjusted_1sigma, 2),
                "bias_score": round(pressure_score, 3),
                "bias": bias,
            },

            "adjustment_multipliers": {
                "flow_multiplier":      round(flow_mult, 3),
                "liquidity_multiplier": round(liq_mult, 3),
                "vrp_multiplier":       round(rv_mult, 3),
                "session_multiplier":   round(session_mult, 3),
            },

            "dealer_flows": {
                "gex":        round(exposure["net"]["gex"], 2),
                "dex":        round(exposure["net"]["dex"], 2),
                "vanna":      round(exposure["net"]["vanna"], 2),
                "charm":      round(exposure["net"]["charm"], 2),
                "volga":      round(exposure["net"]["volga"], 2),
                "speed":      round(exposure["net"]["speed"], 2),
                "theta":      round(exposure["net"]["theta"], 2),
                "rho":        round(exposure["net"]["rho"], 2),
                "gex_regime": self._gex_regime_label(exposure["net"]["gex"]),
                "gamma_flip": gamma_flip,
                "vanna_flip": vanna_flip,
            },

            "regime": regime,

            "volatility_regime": {
                "realized_vol_20d": None if ctx.realized_vol_20d is None else round(ctx.realized_vol_20d, 4),
                "realized_vol_10d": None if ctx.realized_vol_10d is None else round(ctx.realized_vol_10d, 4),
                "realized_vol_5d":  None if ctx.realized_vol_5d  is None else round(ctx.realized_vol_5d, 4),
                "vrp_20d": None if vrp20 is None else round(vrp20, 4),
                "vrp_10d": None if vrp10 is None else round(vrp10, 4),
                "vrp_5d":  None if vrp5  is None else round(vrp5, 4),
                "label": VolatilityRegimeEngine.regime_label(rep_iv, ctx.realized_vol_20d),
            },

            "positioning": positioning,

            "walls": {
                "call_wall": walls["call_wall"],
                "put_wall":  walls["put_wall"],
                "gamma_wall": walls["gamma_wall"],
                "vol_trigger": walls.get("vol_trigger"),
                "call_wall_stats":  None if walls["call_wall"]  is None else by_strike[walls["call_wall"]],
                "put_wall_stats":   None if walls["put_wall"]   is None else by_strike[walls["put_wall"]],
                "gamma_wall_stats": None if walls["gamma_wall"] is None else by_strike[walls["gamma_wall"]],
            },

            "wall_probabilities": {
                "call_wall":  find_ladder(walls["call_wall"]),
                "put_wall":   find_ladder(walls["put_wall"]),
                "gamma_wall": find_ladder(walls["gamma_wall"]),
            },

            "strike_probability_ladder": ladder,
            "decay_path":      decay_path,
            "term_structure":  ts,
        }

    def institutional_card(self, rows: List[OptionRow], ctx: MarketContext) -> str:
        """Human-readable institutional summary card."""
        snap = self.snapshot(rows, ctx)
        if not snap:
            return "No data."

        adj   = snap["adjusted_expectation"]
        raw   = snap["raw_expected_move"]
        flows = snap["dealer_flows"]
        vol   = snap["volatility_regime"]
        walls = snap["walls"]
        wprob = snap["wall_probabilities"]
        pos   = snap["positioning"]
        reg   = snap["regime"]

        def fmt_p(x):
            return f"{x * 100:.1f}%" if x is not None else "n/a"

        cw_t = wprob["call_wall"]["prob_touch"]  if wprob["call_wall"]  else None
        pw_t = wprob["put_wall"]["prob_touch"]   if wprob["put_wall"]   else None
        gw_t = wprob["gamma_wall"]["prob_touch"] if wprob["gamma_wall"] else None

        L = []
        L.append(f"Spot: {snap['spot']:.2f} | IV: {snap['representative_iv']:.2%} | DTE: {snap['representative_dte']:.2f}")
        L.append("")
        L.append("EXPECTED MOVE")
        L.append(f"  Raw 1σ: ±{raw['move_1sigma']:.2f}  ({raw['range_1sigma']['low']:.2f} – {raw['range_1sigma']['high']:.2f})")
        L.append(f"  Raw 2σ: ±{raw['move_2sigma']:.2f}  ({raw['range_2sigma']['low']:.2f} – {raw['range_2sigma']['high']:.2f})")
        L.append("")
        L.append("DEALER-ADJUSTED EXPECTATION")
        L.append(f"  Bias: {adj['bias']}  (score {adj['bias_score']:+.3f})")
        L.append(f"  Center: {adj['expected_center']:.2f}")
        L.append(f"  1σ Range: {adj['expected_low']:.2f} – {adj['expected_high']:.2f}")
        L.append("")
        L.append("FLOW STATE")
        L.append(f"  GEX:   {flows['gex']:+,.1f}   → {flows['gex_regime']}")
        L.append(f"  DEX:   {flows['dex']:+,.1f}")
        L.append(f"  Vanna: {flows['vanna']:+,.1f}")
        L.append(f"  Charm: {flows['charm']:+,.1f}")
        L.append(f"  Volga: {flows['volga']:+,.1f}")
        L.append(f"  Gamma Flip: {flows['gamma_flip']}  |  Vanna Flip: {flows['vanna_flip']}")
        L.append("")
        L.append("COMPOSITE REGIME")
        L.append(f"  {reg['regime']}  (score {reg['composite_score']:+d})")
        L.append(f"  {reg['style']}")
        L.append(f"  Plays: {reg['preferred_plays']}")
        L.append("")
        L.append("VOL REGIME")
        L.append(f"  {vol['label']}")
        L.append(f"  RV20: {vol['realized_vol_20d']} | VRP20: {vol['vrp_20d']}")
        L.append("")
        L.append("POSITIONING")
        L.append(f"  {pos['label']} | net bias {pos['net_opening_bias']:+.3f}")
        L.append("")
        L.append("KEY LEVELS")
        L.append(f"  Call Wall:   {walls['call_wall']}  | touch {fmt_p(cw_t)}")
        L.append(f"  Put Wall:    {walls['put_wall']}  | touch {fmt_p(pw_t)}")
        L.append(f"  Gamma Wall:  {walls['gamma_wall']} | touch {fmt_p(gw_t)}")
        if walls.get("vol_trigger"):
            L.append(f"  Vol Trigger: {walls['vol_trigger']}")

        return "\n".join(L)


# ╔═══════════════════════════════════════════════════════════════╗
# ║  SECTION 20 — CONVENIENCE / QUICK-RUN                         ║
# ╚═══════════════════════════════════════════════════════════════╝

def quick_analysis(rows: List[OptionRow], r: float = 0.04,
                   vol_surface: Optional[SVISurface] = None,
                   sabr_params: Optional[Dict[float, SABRParams]] = None) -> Dict:
    """One-call convenience: computes everything and returns a full report."""
    engine = ExposureEngine(r=r, vol_surface=vol_surface, sabr_params=sabr_params)
    result = engine.compute(rows)
    flip   = engine.gamma_flip(rows)
    v_flip = engine.vanna_flip(rows)
    decay  = DecaySimulator(engine).project(rows, days_forward=5)
    ts     = term_structure(rows, engine)
    regime = composite_regime(result["net"])
    return {
        "exposures":      result,
        "gamma_flip":     flip,
        "vanna_flip":     v_flip,
        "decay_path":     decay,
        "term_structure": ts,
        "regime":         regime,
    }


# ╔═══════════════════════════════════════════════════════════════╗
# ║  CONSOLE DEMO                                                 ║
# ╚═══════════════════════════════════════════════════════════════╝

if __name__ == "__main__":
    rows = [
        OptionRow("call", 675, 1, 0.302, 11000, 671.30, volume=2900, oi_change=1200),
        OptionRow("call", 680, 1, 0.305, 12000, 671.30, volume=3500, oi_change=1600),
        OptionRow("call", 685, 1, 0.311,  9000, 671.30, volume=2400, oi_change=800),
        OptionRow("put",  665, 1, 0.309, 10000, 671.30, volume=3100, oi_change=900),
        OptionRow("put",  660, 1, 0.315, 15000, 671.30, volume=4200, oi_change=1400),
        OptionRow("put",  655, 1, 0.321, 13000, 671.30, volume=2600, oi_change=500),
    ]

    ctx = MarketContext(
        spot=671.30, risk_free_rate=0.04,
        realized_vol_20d=0.24, realized_vol_10d=0.27, realized_vol_5d=0.29,
        avg_daily_dollar_volume=55e9, intraday_dollar_volume=18e9,
        orderbook_depth_dollars=1.2e9, bid_ask_spread_pct=0.0008,
        session_progress=0.55, is_0dte=True,
    )

    engine = InstitutionalExpectationEngine(r=ctx.risk_free_rate)
    print(engine.institutional_card(rows, ctx))
    print()

    snap = engine.snapshot(rows, ctx)
    print("FIRST 5 LADDER ROWS:")
    for row in snap["strike_probability_ladder"][:5]:
        print(f"  {row}")

    print(f"\nDECAY PATH (5 days):")
    for d in snap["decay_path"]:
        print(f"  Day +{d['day_offset']:.0f}: GEX={d['net_gex']:+,.1f}  Flip={d['gamma_flip']}")

    print(f"\nTERM STRUCTURE:")
    for b in snap["term_structure"]:
        print(f"  {b['bucket']:>10s}: count={b['count']:3d}  GEX={b['net_gex']:+,.1f}")
