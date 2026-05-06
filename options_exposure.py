# options_exposure.py  — v4 production-hardened
# Educational/demo code. Not financial advice.
#
# v9 (Patch 8): IV-aware sweep band widening. The hardcoded ±10% (_dg) and
# ±12% (_grid) bands were credit-era artifacts that worked for low-vol tickers
# but missed gamma flips for high-IV names where the flip sits 15-25% from
# spot. Empirically validated against MRNA/AMD/ARM/LLY (Patch 7.1 diagnostic
# data). Formula: pct = max(0.15, min(0.40, 3.0 * iv * sqrt(dte_years))).
# See post-Patch-7.1 handoff §Patch 8.

import math, random, time, copy, json
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Callable, Any
from enum import Enum
from functools import lru_cache

SQRT_2PI = math.sqrt(2.0 * math.pi)
PI = math.pi

UNITS = {
    "gex": "$ per 1% spot move", "dex": "$ notional delta",
    "vanna": "$ delta per 1 vol point", "charm": "$ delta per calendar day",
    "volga": "$ vega per 1 vol point", "speed": "$ gamma per 1% spot move",
    "theta": "$ per calendar day", "rho": "$ per 1% rate move",
}

# ── SCHEMA VERSION ───────────────────────────────────────────────
# Item 15: Frozen output schema. Bump MINOR for additive fields, MAJOR for breaking.
SCHEMA_VERSION = "4.0.0"


# ── OBSERVABILITY / LOGGING ──────────────────────────────────────
# Item 14: Structured audit log. Consumers can inject their own logger.

class AuditLog:
    """Collects diagnostic traces during a snapshot computation.
    Every snapshot returns its audit log so callers can inspect why the engine
    said what it said. No side effects, no global state."""
    def __init__(self):
        self.entries: List[Dict[str, Any]] = []
    def log(self, category: str, **kwargs):
        self.entries.append({"cat": category, "ts": time.monotonic(), **kwargs})
    def summary(self) -> Dict:
        cats = {}
        for e in self.entries:
            c = e["cat"]
            cats[c] = cats.get(c, 0) + 1
        return {"total_entries": len(self.entries), "by_category": cats}


# ── INPUT VALIDATION ─────────────────────────────────────────────
# Item 4: Fail-closed validation. Returns (cleaned_rows, quarantined, warnings).

class InputValidator:
    @staticmethod
    def validate_row(r) -> Tuple[bool, List[str]]:
        """Returns (is_valid, list_of_issues)."""
        issues = []
        if r.underlying_price <= 0: issues.append("non-positive underlying_price")
        if r.strike <= 0: issues.append("non-positive strike")
        if r.open_interest < 0: issues.append("negative open_interest")
        if r.days_to_exp < 0: issues.append("negative days_to_exp")
        if r.iv < 0 or r.iv > 10: issues.append(f"iv out of range: {r.iv}")
        if r.dividend_yield < -0.5 or r.dividend_yield > 1.0: issues.append(f"dividend_yield out of range: {r.dividend_yield}")
        if r.option_type.lower() not in ("call", "put"): issues.append(f"invalid option_type: {r.option_type}")
        if r.bid is not None and r.ask is not None and r.bid > r.ask + 0.01: issues.append("bid > ask")
        if r.bid is not None and r.bid < 0: issues.append("negative bid")
        if r.volume < 0: issues.append("negative volume")
        # Check for non-finite values
        for fld in [r.iv, r.underlying_price, r.strike, r.days_to_exp]:
            if not math.isfinite(fld): issues.append(f"non-finite value: {fld}")
        return len(issues) == 0, issues

    @staticmethod
    def validate_context(ctx) -> List[str]:
        issues = []
        if ctx.spot <= 0: issues.append("non-positive spot")
        if not math.isfinite(ctx.spot): issues.append("non-finite spot")
        if ctx.risk_free_rate < -0.5 or ctx.risk_free_rate > 1.0: issues.append("rate out of range")
        if ctx.session_progress < 0 or ctx.session_progress > 1: issues.append("session_progress out of [0,1]")
        return issues

    @staticmethod
    def validate_chain(rows, ctx, audit: Optional[AuditLog] = None):
        """Returns (clean_rows, quarantined_rows, ctx_warnings)."""
        clean, quarantined = [], []
        for r in rows:
            ok, issues = InputValidator.validate_row(r)
            if ok:
                clean.append(r)
            else:
                quarantined.append({"row": r, "issues": issues})
                if audit: audit.log("validation_quarantine", strike=r.strike, ot=r.option_type, issues=issues)
        ctx_issues = InputValidator.validate_context(ctx)
        if audit:
            audit.log("validation_summary", clean=len(clean), quarantined=len(quarantined), ctx_issues=ctx_issues)
        return clean, quarantined, ctx_issues


# ── DATA QUALITY / DOWNGRADE LOGIC ───────────────────────────────
# Item 12: Explicit data-quality scoring and downgrade paths.

class DataQualityEngine:
    """Scores data completeness based on what MarketData.app actually provides.
    Trade-print data is not available — removed from scoring entirely.
    OI change is computed from cache — scored when available.
    Liquidity is estimated from stock quote + candles — scored on 2 inputs not 4."""
    @staticmethod
    def score(rows, ctx) -> Dict:
        n = max(len(rows), 1)
        # OI change: computed from cache. Score = fraction of rows that have it.
        has_oi_change = sum(1 for r in rows if r.oi_change is not None)
        oi_q = has_oi_change / n
        # IV quality: MarketData gives per-contract IV from OPRA feed
        iv_q = 0.8  # MarketData IV is vendor-computed, better than flat assumption
        # RV available from OHLC bars?
        rv_q = 1.0 if ctx.realized_vol_20d is not None else (0.8 if (ctx.recent_bars and len(ctx.recent_bars) >= 10) else 0.0)
        # Liquidity: ADV + spread are estimable from stock data
        liq_inputs = sum(1 for x in [ctx.avg_daily_dollar_volume, ctx.bid_ask_spread_pct] if x is not None)
        liq_q = liq_inputs / 2.0  # only 2 inputs are realistically available
        # Bid/ask on option rows
        has_ba = sum(1 for r in rows if r.bid is not None and r.ask is not None)
        ba_q = has_ba / n
        # Chain depth: how many rows do we have? More = better wall/GEX estimates
        depth_q = min(n / 50.0, 1.0)  # 50+ rows = full score
        # Weighted: IV(25%) + RV(20%) + bid/ask(20%) + OI change(15%) + liquidity(10%) + depth(10%)
        overall = (iv_q * 0.25 + rv_q * 0.20 + ba_q * 0.20 + oi_q * 0.15 + liq_q * 0.10 + depth_q * 0.10)
        return {
            "iv_source_quality": round(iv_q, 3),
            "rv_quality": round(rv_q, 3),
            "bid_ask_quality": round(ba_q, 3),
            "oi_change_quality": round(oi_q, 3),
            "liquidity_input_quality": round(liq_q, 3),
            "chain_depth_quality": round(depth_q, 3),
            "overall": round(overall, 3),
        }

    @staticmethod
    def downgrade_flags(quality: Dict) -> List[str]:
        flags = []
        if quality["rv_quality"] < 0.1: flags.append("NO_RV_DATA: VRP regime unknown")
        if quality["bid_ask_quality"] < 0.1: flags.append("NO_BID_ASK: IV and spread data missing")
        if quality["oi_change_quality"] < 0.1: flags.append("NO_OI_CHANGE: first scan — caching OI for next run")
        if quality["chain_depth_quality"] < 0.3: flags.append("THIN_CHAIN: fewer than 15 contracts")
        if quality["overall"] < 0.3: flags.append("LOW_DATA_QUALITY: results are low-confidence")
        return flags


# ── CONFIDENCE SCORE ─────────────────────────────────────────────
# Item 5: Composite output confidence from all signal qualities.

class ConfidenceEngine:
    @staticmethod
    def compute(quality: Dict, avg_dealer_conf: float, spread_leg_pct: float) -> Dict:
        """Composite confidence. Weighted for MarketData.app data reality:
        - data_quality (60%): IV, RV, bid/ask, OI change, chain depth
        - spread_ambiguity (25%): high spread detection = less certain dealer sign
        - dealer_sign (15%): from OI-based heuristic (best we can do without trade prints)
        """
        data_conf = quality["overall"]
        sign_conf = avg_dealer_conf
        spread_penalty = max(0, 1.0 - spread_leg_pct * 0.4)  # softer penalty for indices
        composite = data_conf * 0.60 + spread_penalty * 0.25 + sign_conf * 0.15
        if composite >= 0.65: label = "HIGH"
        elif composite >= 0.45: label = "MODERATE"
        else: label = "LOW"
        return {
            "composite": round(composite, 3),
            "label": label,
            "components": {
                "data_quality": round(data_conf, 3),
                "spread_ambiguity": round(spread_penalty, 3),
                "dealer_sign": round(sign_conf, 3),
            },
        }


# ── RV POLICY ────────────────────────────────────────────────────
# Item 8: Explicit RV metric selection per use case.

class RVPolicy:
    """Defines which RV estimator is canonical for which purpose."""
    VRP_ESTIMATOR = "yang_zhang"      # Best for VRP: handles jumps + overnight
    SIMPLICITY_ESTIMATOR = "close_to_close"  # For quick checks
    IMPACT_SOURCE = "intraday_return_vol"    # For impact model: from ctx

    @staticmethod
    def get_vrp_rv(bars, window=20):
        """Canonical RV for VRP computation."""
        subset = bars[-window:] if len(bars) >= window else bars
        return RealizedVolEngine.yang_zhang(subset)

    @staticmethod
    def get_check_rv(bars, window=20):
        closes = [b.close for b in bars[-window:]] if len(bars) >= window else [b.close for b in bars]
        return RealizedVolEngine.close_to_close(closes)


# ── TOUCH PROBABILITY ROUTING ────────────────────────────────────
# Item 10: Defines when to use analytic, skew-adjusted, or MC touch.

class TouchRouter:
    """Routes touch probability queries to the appropriate model."""
    MAX_MC_STRIKES = 10   # Item 3: guardrail on MC cost
    MAX_MC_PATHS = 5000   # Item 3: cap paths per strike

    @staticmethod
    def route(S, H, T, r, q, sigma, barrier_sigma=None,
              has_events=False, jump_params=None, audit=None) -> Dict:
        """
        Decision tree:
        1. If barrier_sigma available → skew-adjusted analytic
        2. If events or jumps AND within MC budget → MC
        3. Otherwise → flat analytic
        """
        method = "analytic_flat"
        prob = BarrierModel.analytic_touch(S, H, T, r, q, sigma)

        if barrier_sigma is not None and abs(barrier_sigma - sigma) > 0.005:
            prob = BarrierModel.skew_adjusted_touch(S, H, T, r, q, sigma, barrier_sigma)
            method = "analytic_skew"

        if (has_events or jump_params) and T <= 7/365:  # only MC for short-dated
            mc_paths = min(TouchRouter.MAX_MC_PATHS, 3000)
            ji = jump_params.get("intensity", 0) if jump_params else 0
            jm = jump_params.get("mean", 0) if jump_params else 0
            js = jump_params.get("std", 0) if jump_params else 0
            if ji > 0:
                mc = BarrierModel.monte_carlo_touch(S, H, T, r, q, sigma,
                    n_paths=mc_paths, n_steps=max(int(T*365*24), 50),
                    jump_intensity=ji, jump_mean=jm, jump_std=js)
                prob = mc["prob_touch"]
                method = "mc_jump"

        if audit:
            audit.log("touch_routing", strike=H, method=method, prob=round(prob, 4))
        return {"prob_touch": round(prob, 4), "method": method}

# ── 1. MATH PRIMITIVES ──────────────────────────────────────────

def norm_pdf(x): return math.exp(-0.5*x*x)/SQRT_2PI
def norm_cdf(x): return 0.5*(1.0+math.erf(x/math.sqrt(2.0)))
def safe_sqrt(x): return math.sqrt(max(x,1e-12))
def safe_log(x): return math.log(max(x,1e-300))
def year_fraction(d): return max(d/365.0,1e-8)
def clamp(x,lo,hi): return max(lo,min(hi,x))

_GL20_X=[0.07053988969198299,0.37212681800161144,0.9165821024734295,1.707306531028367,2.7491992553094321,4.0489253138508869,5.6151749708616165,7.4590174536710633,9.5943928695810968,12.03880254696482,14.814293442630737,17.948895520519376,21.478788240285011,25.451702793186903,29.932554631700612,35.01343424047900,40.833057056728956,47.619994047346462,55.810795750063898,66.524416525615754]
_GL20_W=[0.16874680185111386,0.29125436200606828,0.26668610286831735,0.16600245326950687,0.07482581529153363,0.02491480533585698,0.00620874560986777,0.00114481558318275,0.00015574177302781,0.00001541614988887,1.0864863665179e-06,5.3301209095567e-08,1.7579811790506e-09,3.7255024025123e-11,4.7675292515982e-13,3.3728442433624e-15,1.1550143395397e-17,1.5395221405044e-20,5.2864427255691e-24,1.6564566124990e-28]

# ── 2. GBS GREEKS (with dividend yield q) ────────────────────────

def gbs_d1(S,K,T,r,q,sigma):
    sigma=max(sigma,1e-8);T=max(T,1e-8)
    return (math.log(S/K)+(r-q+0.5*sigma*sigma)*T)/(sigma*safe_sqrt(T))
def gbs_d2(S,K,T,r,q,sigma): return gbs_d1(S,K,T,r,q,sigma)-sigma*safe_sqrt(T)
def gbs_price(ot,S,K,T,r,q,sigma):
    d1=gbs_d1(S,K,T,r,q,sigma);d2=d1-sigma*safe_sqrt(T)
    if ot.lower()=="call": return S*math.exp(-q*T)*norm_cdf(d1)-K*math.exp(-r*T)*norm_cdf(d2)
    return K*math.exp(-r*T)*norm_cdf(-d2)-S*math.exp(-q*T)*norm_cdf(-d1)
def gbs_delta(ot,S,K,T,r,q,sigma):
    d1=gbs_d1(S,K,T,r,q,sigma)
    return math.exp(-q*T)*norm_cdf(d1) if ot.lower()=="call" else math.exp(-q*T)*(norm_cdf(d1)-1.0)
def gbs_theta(ot,S,K,T,r,q,sigma):
    T=max(T,1e-8);d1=gbs_d1(S,K,T,r,q,sigma);d2=d1-sigma*safe_sqrt(T)
    c=-(S*math.exp(-q*T)*norm_pdf(d1)*sigma)/(2.0*safe_sqrt(T))
    if ot.lower()=="call": v=c+q*S*math.exp(-q*T)*norm_cdf(d1)-r*K*math.exp(-r*T)*norm_cdf(d2)
    else: v=c-q*S*math.exp(-q*T)*norm_cdf(-d1)+r*K*math.exp(-r*T)*norm_cdf(-d2)
    return v/365.0
def gbs_rho(ot,S,K,T,r,q,sigma):
    d2=gbs_d2(S,K,T,r,q,sigma)
    if ot.lower()=="call": return K*T*math.exp(-r*T)*norm_cdf(d2)*0.01
    return -K*T*math.exp(-r*T)*norm_cdf(-d2)*0.01
def gbs_gamma(S,K,T,r,q,sigma):
    d1=gbs_d1(S,K,T,r,q,sigma);return math.exp(-q*T)*norm_pdf(d1)/(S*sigma*safe_sqrt(T))
def gbs_vega(S,K,T,r,q,sigma):
    """Per 1% vol move."""
    d1=gbs_d1(S,K,T,r,q,sigma);return S*math.exp(-q*T)*norm_pdf(d1)*safe_sqrt(T)*0.01
def gbs_vanna(S,K,T,r,q,sigma):
    sigma=max(sigma,1e-8);T=max(T,1e-8);d1=gbs_d1(S,K,T,r,q,sigma);d2=d1-sigma*safe_sqrt(T)
    return -math.exp(-q*T)*norm_pdf(d1)*d2/sigma
def gbs_volga(S,K,T,r,q,sigma):
    sigma=max(sigma,1e-8);T=max(T,1e-8);d1=gbs_d1(S,K,T,r,q,sigma);d2=d1-sigma*safe_sqrt(T)
    return S*math.exp(-q*T)*norm_pdf(d1)*safe_sqrt(T)*d1*d2/sigma
def gbs_charm(ot,S,K,T,r,q,sigma):
    """Charm = -∂delta/∂T. Raw BS derivative is per-year; divide by 365 for per-calendar-day (matches gbs_theta convention)."""
    sigma=max(sigma,1e-8);T=max(T,1e-8);d1=gbs_d1(S,K,T,r,q,sigma);d2=d1-sigma*safe_sqrt(T)
    cc=-math.exp(-q*T)*norm_pdf(d1)*(2.0*(r-q)*T-d2*sigma*safe_sqrt(T))/(2.0*T*sigma*safe_sqrt(T))
    if ot.lower()=="call": return (cc+q*math.exp(-q*T)*norm_cdf(d1))/365.0
    return (cc-q*math.exp(-q*T)*norm_cdf(-d1))/365.0
def gbs_veta(S,K,T,r,q,sigma):
    sigma=max(sigma,1e-8);T=max(T,1e-8);d1=gbs_d1(S,K,T,r,q,sigma);d2=d1-sigma*safe_sqrt(T)
    return -S*math.exp(-q*T)*norm_pdf(d1)*safe_sqrt(T)*(q+((r-q)*d1)/(sigma*safe_sqrt(T))-(1.0+d1*d2)/(2.0*T))
def gbs_speed(S,K,T,r,q,sigma):
    d1=gbs_d1(S,K,T,r,q,sigma);g=gbs_gamma(S,K,T,r,q,sigma)
    return -g/S*(d1/(sigma*safe_sqrt(max(T,1e-8)))+1.0)
def gbs_zomma(S,K,T,r,q,sigma):
    sigma=max(sigma,1e-8);T=max(T,1e-8);d1=gbs_d1(S,K,T,r,q,sigma);d2=d1-sigma*safe_sqrt(T)
    return gbs_gamma(S,K,T,r,q,sigma)*(d1*d2-1.0)/sigma
def gbs_color(S,K,T,r,q,sigma):
    sigma=max(sigma,1e-8);T=max(T,1e-8);d1=gbs_d1(S,K,T,r,q,sigma);d2=d1-sigma*safe_sqrt(T)
    return -math.exp(-q*T)*norm_pdf(d1)/(2.0*S*T*sigma*safe_sqrt(T))*(2.0*q*T+1.0+(2.0*(r-q)*T-d2*sigma*safe_sqrt(T))/(sigma*safe_sqrt(T))*d1)
def gbs_ultima(S,K,T,r,q,sigma):
    sigma=max(sigma,1e-8);T=max(T,1e-8);d1=gbs_d1(S,K,T,r,q,sigma);d2=d1-sigma*safe_sqrt(T)
    vr=S*math.exp(-q*T)*norm_pdf(d1)*safe_sqrt(T)
    return(-vr/(sigma*sigma))*(d1*d2*(1.0-d1*d2)+d1*d1+d2*d2)

# Legacy wrappers (q=0)
def bs_d1(S,K,T,r,s): return gbs_d1(S,K,T,r,0,s)
def bs_d2(S,K,T,r,s): return gbs_d2(S,K,T,r,0,s)
def bs_delta(o,S,K,T,r,s): return gbs_delta(o,S,K,T,r,0,s)
def bs_gamma(S,K,T,r,s): return gbs_gamma(S,K,T,r,0,s)
def bs_vega(S,K,T,r,s): return gbs_vega(S,K,T,r,0,s)
def bs_vanna(S,K,T,r,s): return gbs_vanna(S,K,T,r,0,s)
def bs_charm(o,S,K,T,r,s): return gbs_charm(o,S,K,T,r,0,s)

# ── 3. IV SOLVER (Brent) ────────────────────────────────────────

def implied_vol(ot,S,K,T,r,q,mp,tol=1e-8,mi=100):
    intr=max(0,(S*math.exp(-q*T)-K*math.exp(-r*T)) if ot.lower()=="call" else (K*math.exp(-r*T)-S*math.exp(-q*T)))
    if mp<=intr+1e-10: return None
    def f(s): return gbs_price(ot,S,K,T,r,q,s)-mp
    a,b=0.001,5.0;fa,fb=f(a),f(b)
    if fa*fb>0: return None
    c,fc=b,fb;d=e=b-a
    for _ in range(mi):
        if fb*fc>0: c,fc=a,fa;d=e=b-a
        if abs(fc)<abs(fb): a,fa=b,fb;b,fb=c,fc;c,fc=a,fa
        t1=2e-15*abs(b)+0.5*tol;m=0.5*(c-b)
        if abs(m)<=t1 or fb==0: return b
        if abs(e)>=t1 and abs(fa)>abs(fb):
            s=fb/fa
            if a==c: p=2*m*s;qq=1-s
            else: qq=fa/fc;rr=fb/fc;p=s*(2*m*qq*(qq-rr)-(b-a)*(rr-1));qq=(qq-1)*(rr-1)*(s-1)
            if p>0: qq=-qq
            p=abs(p)
            if 2*p<min(3*m*qq-abs(t1*qq),abs(e*qq)): e=d;d=p/qq
            else: d=m;e=m
        else: d=m;e=m
        a,fa=b,fb;b+=d if abs(d)>t1 else(t1 if m>0 else-t1);fb=f(b)
    return b

# ── 4. SVI SURFACE ──────────────────────────────────────────────

@dataclass
class SVIParams:
    a:float;b:float;rho:float;m:float;sigma:float
    def total_variance(self,k):
        d=k-self.m;return self.a+self.b*(self.rho*d+math.sqrt(d*d+self.sigma*self.sigma))
    def implied_vol_at(self,k,T): return safe_sqrt(max(self.total_variance(k),0)/max(T,1e-8))
    def smile(self,strikes,S,T,r,q):
        F=S*math.exp((r-q)*T);return{K:self.implied_vol_at(math.log(K/F),T)for K in strikes}
    def butterfly_arbitrage_free(self): return self.b*(1+abs(self.rho))<=4

@dataclass
class SVISurface:
    slices:Dict[float,SVIParams]=field(default_factory=dict)
    def add_slice(self,T,p): self.slices[T]=p
    def implied_vol(self,K,S,T,r,q):
        F=S*math.exp((r-q)*T);k=math.log(K/F);ts=sorted(self.slices.keys())
        if not ts: raise ValueError("No SVI slices")
        if T<=ts[0]: return self.slices[ts[0]].implied_vol_at(k,T)
        if T>=ts[-1]: return self.slices[ts[-1]].implied_vol_at(k,T)
        for i in range(len(ts)-1):
            T0,T1=ts[i],ts[i+1]
            if T0<=T<=T1:
                w0=self.slices[T0].total_variance(k);w1=self.slices[T1].total_variance(k)
                a=(T-T0)/(T1-T0);return safe_sqrt(max(w0*(1-a)+w1*a,0)/max(T,1e-8))
        return self.slices[ts[-1]].implied_vol_at(k,T)

# ── 5. SABR MODEL ───────────────────────────────────────────────

@dataclass
class SABRParams:
    alpha:float;beta:float=0.5;rho:float=-0.25;nu:float=0.4
    def implied_vol(self,F,K,T):
        if abs(F-K)<1e-10: return self._atm(F,T)
        return self._oof(F,K,T)
    def _atm(self,F,T):
        a,b,rho,nu=self.alpha,self.beta,self.rho,self.nu;Fb=F**(1-b)
        return(a/Fb)*(1+(((1-b)**2*a**2)/(24*Fb**2)+(rho*b*nu*a)/(4*Fb)+nu**2*(2-3*rho**2)/24)*T)
    def _oof(self,F,K,T):
        a,b,rho,nu=self.alpha,self.beta,self.rho,self.nu
        FK=F*K;FKb=FK**((1-b)/2);lnFK=math.log(F/K)
        z=(nu/a)*FKb*lnFK
        if abs(z)<1e-10: xz=1
        else: xz=z/math.log((math.sqrt(1-2*rho*z+z*z)+z-rho)/(1-rho))
        pf=a/(FKb*(1+(1-b)**2*lnFK**2/24+(1-b)**4*lnFK**4/1920))
        cr=1+((1-b)**2*a**2/(24*FKb**2)+rho*b*nu*a/(4*FKb)+nu**2*(2-3*rho**2)/24)*T
        return pf*xz*cr
    def smile(self,F,strikes,T): return{K:self.implied_vol(F,K,T)for K in strikes}

# ── 6. HESTON (hardened) ────────────────────────────────────────

@dataclass
class HestonParams:
    """Heston stochastic vol model. PRODUCTION NOTE (Item 11): Use as optional
    validation/secondary model only. Do NOT make default pricing engine unless
    calibrated daily with diagnostics. Feller check and numerical guards are in place,
    but integration can still misbehave for extreme parameters."""
    v0:float;theta:float;kappa:float;xi:float;rho:float
    def feller_satisfied(self): return 2*self.kappa*self.theta>self.xi**2
    def feller_ratio(self): return(2*self.kappa*self.theta)/max(self.xi**2,1e-12)
    def _cexp(self,z):
        import cmath
        try:
            if z.real>500: return complex(1e200,0)
            if z.real<-500: return complex(0,0)
            return cmath.exp(z)
        except: return complex(0,0)
    def _clog(self,z):
        import cmath
        try: return cmath.log(z)
        except: return complex(0,0)
    def characteristic_fn(self,u,S,K,T,r,q,j):
        v0,th,ka,xi,rho=self.v0,self.theta,self.kappa,self.xi,self.rho
        if j==1: ua=u-1j;b=ka-rho*xi
        else: ua=u;b=ka
        al=-0.5*(ua*ua+1j*ua);be=b-rho*xi*1j*ua;ga=0.5*xi*xi
        disc=be*be-4*al*ga;d=disc**0.5
        rp=(be+d)/(2*ga) if abs(ga)>1e-15 else complex(0,0)
        rm=(be-d)/(2*ga) if abs(ga)>1e-15 else complex(0,0)
        g=rm/rp if abs(rp)>1e-15 else complex(0,0)
        edT=self._cexp(-d*T);den=1-g*edT
        if abs(den)<1e-30: return complex(0,0)
        C=ka*(rm*T-(2/max(xi**2,1e-12))*self._clog(den/(1-g)))
        D=rm*(1-edT)/den
        ls=math.log(S)+(r-q)*T
        return self._cexp(C*th+D*v0+1j*u*ls)
    def price(self,ot,S,K,T,r,q):
        import cmath
        lK=math.log(K)
        def intg(u,j):
            try:
                phi=self.characteristic_fn(u-0.5j,S,K,T,r,q,j)
                v=(cmath.exp(-1j*u*lK)*phi/(1j*u)).real
                return v if math.isfinite(v) else 0
            except: return 0
        P1=P2=0.5
        for i in range(len(_GL20_X)):
            u=_GL20_X[i];w=_GL20_W[i]
            if u<1e-15: continue
            P1+=w*intg(u,1)/PI;P2+=w*intg(u,2)/PI
        P1=clamp(P1,0,1);P2=clamp(P2,0,1)
        cp=max(S*math.exp(-q*T)*P1-K*math.exp(-r*T)*P2,0)
        return cp if ot.lower()=="call" else max(cp-S*math.exp(-q*T)+K*math.exp(-r*T),0)
    def implied_vol_bs(self,ot,S,K,T,r,q):
        try: return implied_vol(ot,S,K,T,r,q,self.price(ot,S,K,T,r,q))
        except: return None

# ── 7. BAW AMERICAN (hardened) ───────────────────────────────────

def baw_american_price(ot,S,K,T,r,q,sigma):
    eu=gbs_price(ot,S,K,T,r,q,sigma)
    if ot.lower()=="call" and q<=0: return eu
    if ot.lower()=="put" and r<=0: return eu
    T=max(T,1e-8);sigma=max(sigma,1e-8)
    if T<1e-6: return max(eu,(S-K)if ot.lower()=="call" else(K-S))
    M=2*r/(sigma*sigma);N=2*(r-q)/(sigma*sigma);Kc=1-math.exp(-r*T)
    if abs(Kc)<1e-12: return eu
    dsq=(N-1)**2+4*M/Kc
    if dsq<0: return eu
    if ot.lower()=="call":
        q2=(-(N-1)+math.sqrt(dsq))/2
        if q2<=0: return eu
        Ss=_baw_cc(S,K,T,r,q,sigma,q2)
        if S>=Ss: return max(S-K,eu)
        A2=(Ss/q2)*(1-math.exp(-q*T)*norm_cdf(gbs_d1(Ss,K,T,r,q,sigma)))
        return eu+A2*(S/Ss)**q2
    else:
        q1=(-(N-1)-math.sqrt(dsq))/2
        if q1>=0: return eu
        Ss=_baw_cp(S,K,T,r,q,sigma,q1)
        if S<=Ss: return max(K-S,eu)
        A1=-(Ss/q1)*(1-math.exp(-q*T)*norm_cdf(-gbs_d1(Ss,K,T,r,q,sigma)))
        return eu+A1*(S/Ss)**q1

def _baw_cc(S,K,T,r,q,sigma,q2,mi=100,tol=1e-7):
    Ss=K+gbs_price("call",K,K,T,r,q,sigma);best_S=Ss;best_e=1e30
    for _ in range(mi):
        Ss=max(Ss,K*1.0001);d1=gbs_d1(Ss,K,T,r,q,sigma);ec=gbs_price("call",Ss,K,T,r,q,sigma)
        L=ec+(1-math.exp(-q*T)*norm_cdf(d1))*Ss/q2;R=Ss-K;e=abs(L-R)
        if e<best_e: best_S=Ss;best_e=e
        if e<tol: break
        sl=max((1-1/q2)*(1-math.exp(-q*T)*norm_cdf(d1)),1e-10);Ss-=(L-R)/sl
    return best_S

def _baw_cp(S,K,T,r,q,sigma,q1,mi=100,tol=1e-7):
    Ss=K-gbs_price("put",K,K,T,r,q,sigma);best_S=Ss;best_e=1e30
    for _ in range(mi):
        Ss=clamp(Ss,0.001,K*0.9999);d1=gbs_d1(Ss,K,T,r,q,sigma);ep=gbs_price("put",Ss,K,T,r,q,sigma)
        L=ep-(1-math.exp(-q*T)*norm_cdf(-d1))*Ss/q1;R=K-Ss;e=abs(L-R)
        if e<best_e: best_S=Ss;best_e=e
        if e<tol: break
        sl=max(-(1-1/q1)*(1-math.exp(-q*T)*norm_cdf(-d1)),1e-10);Ss-=(L-R)/sl
    return best_S

# ── 8. REALIZED VOL ENGINE ──────────────────────────────────────

@dataclass
class OHLC:
    open:float;high:float;low:float;close:float;prev_close:Optional[float]=None

class RealizedVolEngine:
    @staticmethod
    def close_to_close(closes,af=252.0):
        if len(closes)<3: return None
        lr=[math.log(closes[i]/closes[i-1])for i in range(1,len(closes))if closes[i-1]>0]
        if len(lr)<2: return None
        m=sum(lr)/len(lr);v=sum((r-m)**2 for r in lr)/(len(lr)-1)
        return math.sqrt(v*af)
    @staticmethod
    def parkinson(bars,af=252.0):
        if not bars: return None
        n=len(bars);t=sum(math.log(b.high/b.low)**2 for b in bars if b.high>0 and b.low>0)
        return math.sqrt(t/(4*n*math.log(2))*af)
    @staticmethod
    def garman_klass(bars,af=252.0):
        if not bars: return None
        n=len(bars);t=0
        for b in bars:
            if b.high<=0 or b.low<=0 or b.close<=0 or b.open<=0: continue
            hl=math.log(b.high/b.low);co=math.log(b.close/b.open)
            t+=0.5*hl*hl-(2*math.log(2)-1)*co*co
        return math.sqrt(max(t/n,0)*af)
    @staticmethod
    def yang_zhang(bars,af=252.0):
        if len(bars)<2: return None
        n=len(bars);fl=[]
        for i,b in enumerate(bars):
            pc=b.prev_close if b.prev_close is not None else(bars[i-1].close if i>0 else b.open)
            fl.append((pc,b.open,b.high,b.low,b.close))
        on=[math.log(o/pc)for pc,o,h,l,c in fl if pc>0 and o>0]
        oc=[math.log(c/o)for pc,o,h,l,c in fl if o>0 and c>0]
        if len(on)<2 or len(oc)<2: return None
        mo=sum(on)/len(on);vo=sum((x-mo)**2 for x in on)/(len(on)-1)
        mc=sum(oc)/len(oc);vc=sum((x-mc)**2 for x in oc)/(len(oc)-1)
        rs=sum(math.log(h/c)*math.log(h/o)+math.log(l/c)*math.log(l/o)for pc,o,h,l,c in fl if h>0 and l>0 and o>0 and c>0)/n
        k=0.34/(1.34+(n+1)/(n-1));return math.sqrt(max(vo+k*vc+(1-k)*rs,0)*af)
    @classmethod
    def multi_window(cls,bars,windows=None):
        if windows is None: windows=[5,10,20,60]
        return{f"rv_{w}d":cls.yang_zhang(bars[-w:]if len(bars)>=w else bars)for w in windows}

# ── 9. DATA MODEL ───────────────────────────────────────────────

class ExerciseStyle(Enum):
    EUROPEAN="european";AMERICAN="american"
class TradeSide(Enum):
    BUY="buy";SELL="sell";UNKNOWN="unknown"

@dataclass
class TradeCondition:
    price:float;size:int;bid_at_time:Optional[float]=None;ask_at_time:Optional[float]=None
    is_sweep:bool=False;is_block:bool=False;exchange:str=""

@dataclass
class OptionRow:
    option_type:str;strike:float;days_to_exp:float;iv:float;open_interest:int;underlying_price:float
    contract_size:int=100;volume:int=0;dividend_yield:float=0.0
    exercise_style:ExerciseStyle=ExerciseStyle.EUROPEAN
    bid:Optional[float]=None;ask:Optional[float]=None;last:Optional[float]=None
    oi_change:Optional[int]=None;trades:Optional[List[TradeCondition]]=None
    inferred_side:Optional[TradeSide]=None;dealer_sign_confidence:float=0.5
    delta:Optional[float]=None;gamma:Optional[float]=None;vanna:Optional[float]=None
    charm:Optional[float]=None;volga:Optional[float]=None;speed:Optional[float]=None
    theta:Optional[float]=None;rho:Optional[float]=None

@dataclass
class ScheduledEvent:
    name:str;days_until:float;implied_move:float;confidence:float=0.8
    product:str=""  # e.g. "SPX","AAPL","QQQ" — for product-specific filtering
    event_type:str="generic"  # "earnings","fomc","cpi","expiry","dividend" — affects variance model

@dataclass
class MarketContext:
    spot:float;risk_free_rate:float=0.04
    realized_vol_20d:Optional[float]=None;realized_vol_10d:Optional[float]=None;realized_vol_5d:Optional[float]=None
    avg_daily_dollar_volume:Optional[float]=None;intraday_dollar_volume:Optional[float]=None
    orderbook_depth_dollars:Optional[float]=None;bid_ask_spread_pct:Optional[float]=None
    session_progress:float=0.50;is_0dte:bool=False
    recent_bars:Optional[List[OHLC]]=None;intraday_return_vol:Optional[float]=None
    events:Optional[List[ScheduledEvent]]=None

# ── 10. TRADE SIGN ENGINE ───────────────────────────────────────

class TradeSignEngine:
    @staticmethod
    def classify_trade(tc):
        if tc.bid_at_time is not None and tc.ask_at_time is not None:
            sp=tc.ask_at_time-tc.bid_at_time
            if sp>0:
                if tc.price>=tc.ask_at_time-0.01: return TradeSide.BUY,0.90 if tc.is_sweep else 0.80
                if tc.price<=tc.bid_at_time+0.01: return TradeSide.SELL,0.90 if tc.is_sweep else 0.80
                ratio=(tc.price-tc.bid_at_time)/sp
                if ratio>0.6: return TradeSide.BUY,0.55+0.25*(ratio-0.5)
                if ratio<0.4: return TradeSide.SELL,0.55+0.25*(0.5-ratio)
        return TradeSide.UNKNOWN,0.50
    @staticmethod
    def classify_row(row):
        if not row.trades:
            if row.oi_change is not None and row.volume>0:
                or_=clamp(row.oi_change/max(row.volume,1),-1,1)
                if or_>0.3: return TradeSide.BUY,0.55
                if or_<-0.3: return TradeSide.SELL,0.55
            return TradeSide.UNKNOWN,0.50
        bv=sv=uv=0;cs=0
        for tc in row.trades:
            s,c=TradeSignEngine.classify_trade(tc)
            if s==TradeSide.BUY: bv+=tc.size
            elif s==TradeSide.SELL: sv+=tc.size
            else: uv+=tc.size
            cs+=c*tc.size
        t=bv+sv+uv
        if t==0: return TradeSide.UNKNOWN,0.50
        ac=cs/t
        if bv>sv*1.3: return TradeSide.BUY,min(ac,0.95)
        if sv>bv*1.3: return TradeSide.SELL,min(ac,0.95)
        return TradeSide.UNKNOWN,max(ac*0.6,0.50)
    @staticmethod
    def detect_sweeps(row): return sum(1 for tc in(row.trades or[])if tc.is_sweep)
    @staticmethod
    def classify_opening_closing(row):
        """Per-row opening vs closing classification using OI change + volume.
        Returns: ('opening'|'closing'|'mixed', confidence 0-1)"""
        if row.oi_change is None or row.volume==0: return 'mixed',0.3
        ratio=row.oi_change/max(row.volume,1)
        if ratio>0.5: return 'opening',min(0.5+ratio*0.4,0.95)
        if ratio<-0.3: return 'closing',min(0.5+abs(ratio)*0.4,0.90)
        return 'mixed',0.4
    @staticmethod
    def infer_spread_legs(rows,volume_tol=0.20,max_strike_gap_pct=0.10,liquid_index=False):
        """Multi-signal spread detection with ratio spreads, combos, and rolls.
        For liquid indices (SPY/QQQ/SPX), uses tighter volume_tol to avoid
        false-matching the naturally high activity at every strike."""
        if liquid_index:
            volume_tol = 0.08  # much tighter — only clear matched trades
            max_strike_gap_pct = 0.03  # narrower gap for index spreads
        groups=[];used=set()
        for i,r1 in enumerate(rows):
            if i in used: continue
            g=[i]
            for j,r2 in enumerate(rows):
                if j<=i or j in used: continue
                # Same expiry spreads
                if r1.days_to_exp==r2.days_to_exp:
                    v1,v2=max(r1.volume,1),max(r2.volume,1)
                    mid=max(r1.underlying_price,1)
                    strike_gap=abs(r1.strike-r2.strike)/mid
                    if strike_gap>max_strike_gap_pct: continue
                    if r1.strike==r2.strike and r1.option_type==r2.option_type: continue
                    # 1:1 ratio check
                    if abs(v1-v2)/max(v1,v2)<=volume_tol:
                        g.append(j);continue
                    # 1:2 or 2:1 ratio check (ratio spreads)
                    if min(v1,v2)>0:
                        ratio=max(v1,v2)/min(v1,v2)
                        if 1.8<=ratio<=2.2 or 2.8<=ratio<=3.2:
                            g.append(j);continue
                # Roll detection: same strike+type, different expiry, similar volume
                elif r1.strike==r2.strike and r1.option_type==r2.option_type:
                    v1,v2=max(r1.volume,1),max(r2.volume,1)
                    if abs(v1-v2)/max(v1,v2)<=volume_tol:
                        g.append(j)  # likely a calendar roll
            if len(g)>1:
                for idx in g: used.add(idx)
                # Classify spread type
                types=set(rows[idx].option_type.lower() for idx in g)
                expiries=set(rows[idx].days_to_exp for idx in g)
                if len(expiries)>1: stype="roll"
                elif len(types)==2: stype="combo"
                elif len(g)==2:
                    v1,v2=rows[g[0]].volume,rows[g[1]].volume
                    r=max(v1,v2)/max(min(v1,v2),1)
                    stype="ratio" if r>1.5 else "vertical"
                else: stype="complex"
                groups.append({"indices":g,"type":stype})
        return groups
    @staticmethod
    def enrich_rows(rows, liquid_index=False):
        """Classify each row, detect spreads, and penalize confidence on spread legs."""
        for r in rows:
            s,c=TradeSignEngine.classify_row(r);r.inferred_side=s;r.dealer_sign_confidence=c
        spread_groups=TradeSignEngine.infer_spread_legs(rows, liquid_index=liquid_index)
        for group in spread_groups:
            for idx in group["indices"]:
                rows[idx].dealer_sign_confidence*=0.6
        return rows, spread_groups

# ── 11. HELPERS ──────────────────────────────────────────────────

def expected_move_from_iv(S,iv,T): return S*max(iv,1e-8)*math.sqrt(max(T,1e-8))
def prob_finish_above(S,K,T,r,q,s): return norm_cdf(gbs_d2(S,K,T,r,q,s))
def prob_finish_below(S,K,T,r,q,s): return norm_cdf(-gbs_d2(S,K,T,r,q,s))
def prob_touch_approx(p): return clamp(2*p,0,1)

# ── 12. EXPOSURE ENGINE ─────────────────────────────────────────

class ExposureEngine:
    """Dealer-side exposure with unified IV, cached resolution, standardized units."""
    def __init__(self,r=0.04,vol_surface=None,sabr_params=None):
        self.r=r;self.vol_surface=vol_surface;self.sabr_params=sabr_params;self._ivc={}
    def resolve_iv(self,S,K,T,q,row_iv):
        """Unified IV: SVI -> SABR -> row fallback. Used by exposure AND ladder.
        SVI is tried first; if it fails or is absent, SABR is tried; then row IV.
        This is a priority stack, not mutual exclusion — SABR can catch SVI failures."""
        ck=(round(S,4),round(K,4),round(T,8),round(q,6))
        if ck in self._ivc: return self._ivc[ck]
        res=max(row_iv,1e-8);resolved=False
        if self.vol_surface:
            try: res=self.vol_surface.implied_vol(K,S,T,self.r,q);resolved=True
            except: pass
        if not resolved and self.sabr_params:
            ts=sorted(self.sabr_params.keys());nt=min(ts,key=lambda t:abs(t-T),default=None)
            if nt is not None:
                try: res=self.sabr_params[nt].implied_vol(S*math.exp((self.r-q)*T),K,T);resolved=True
                except: pass
        self._ivc[ck]=res;return res
    def clear_cache(self): self._ivc.clear()
    def _enrich(self,row):
        S=row.underlying_price;K=row.strike;T=year_fraction(row.days_to_exp);q=row.dividend_yield
        sigma=self.resolve_iv(S,K,T,q,row.iv);ot=row.option_type.lower()
        delta=row.delta if row.delta is not None else gbs_delta(ot,S,K,T,self.r,q,sigma)
        gamma=row.gamma if row.gamma is not None else gbs_gamma(S,K,T,self.r,q,sigma)
        vanna=row.vanna if row.vanna is not None else gbs_vanna(S,K,T,self.r,q,sigma)
        charm=row.charm if row.charm is not None else gbs_charm(ot,S,K,T,self.r,q,sigma)
        volga=row.volga if row.volga is not None else gbs_volga(S,K,T,self.r,q,sigma)
        speed=row.speed if row.speed is not None else gbs_speed(S,K,T,self.r,q,sigma)
        theta=row.theta if row.theta is not None else gbs_theta(ot,S,K,T,self.r,q,sigma)
        rho=row.rho if row.rho is not None else gbs_rho(ot,S,K,T,self.r,q,sigma)
        price=(baw_american_price(ot,S,K,T,self.r,q,sigma)if row.exercise_style==ExerciseStyle.AMERICAN else gbs_price(ot,S,K,T,self.r,q,sigma))
        conf=row.dealer_sign_confidence if row.dealer_sign_confidence else 0.5
        return{"type":ot,"strike":K,"days_to_exp":row.days_to_exp,"T":T,"iv":sigma,"oi":row.open_interest,"volume":row.volume,"S":S,"q":q,"contract_size":row.contract_size,"delta":delta,"gamma":gamma,"vanna":vanna,"charm":charm,"volga":volga,"speed":speed,"theta":theta,"rho":rho,"price":price,"exercise_style":row.exercise_style.value,"bid":row.bid,"ask":row.ask,"last":row.last,"oi_change":row.oi_change,"dealer_sign_confidence":conf,"inferred_side":row.inferred_side}
    def _exposures(self,d):
        S,oi,mul,ot=d["S"],d["oi"],d["contract_size"],d["type"];conf=d["dealer_sign_confidence"]
        # Patch 9 (convention flip): dealer-side defaults are SqueezeMetrics convention —
        # dealer is LONG calls (ds/gs = +1) and SHORT puts (ds/gs = -1). This matches
        # the dominant SPX flow post-2008: customers buy OTM puts and sell OTM calls,
        # so dealers take the inverse of those positions. See "The Implied Order Book"
        # (SqueezeMetrics 2020) p1 + p4 + p8 for the canonical statement of this
        # convention.
        ds=1 if ot=="call" else -1;gs=1 if ot=="call" else -1
        # Per-contract override: when transaction-level flow analysis shows this
        # specific contract bucked the default customer-side assumption, flip the
        # dealer side. Default for calls is customer-SOLD, so flip on observed BUY.
        # Default for puts is customer-BOUGHT, so flip on observed SELL. (Pre-Patch-9
        # this was a single symmetric `==SELL` check, which only worked correctly for
        # one of the two option types under any given convention. The asymmetric flip
        # below is the consistent fix.)
        if conf>0.65 and (
            (ot=="call" and d["inferred_side"]==TradeSide.BUY) or
            (ot=="put"  and d["inferred_side"]==TradeSide.SELL)
        ):
            ds=-ds;gs=-gs
        return{**d,"dex":ds*d["delta"]*oi*mul*S,"gex":gs*d["gamma"]*oi*mul*S**2*0.01,"vanna_exp":ds*d["vanna"]*oi*mul*S,"charm_exp":ds*d["charm"]*oi*mul*S,"volga_exp":gs*d["volga"]*oi*mul*0.01,"speed_exp":gs*d["speed"]*oi*mul*S**2*0.01,"theta_exp":ds*d["theta"]*oi*mul,"rho_exp":ds*d["rho"]*oi*mul}
    def compute(self,rows):
        enriched=[self._exposures(self._enrich(r))for r in rows]
        bs={}
        for x in enriched:
            K=x["strike"]
            if K not in bs: bs[K]={"gex":0,"dex":0,"vanna":0,"charm":0,"volga":0,"speed":0,"theta":0,"rho":0,"call_oi":0,"put_oi":0,"call_vol":0,"put_vol":0,"call_oi_change":0,"put_oi_change":0}
            b=bs[K];b["gex"]+=x["gex"];b["dex"]+=x["dex"];b["vanna"]+=x["vanna_exp"];b["charm"]+=x["charm_exp"];b["volga"]+=x["volga_exp"];b["speed"]+=x["speed_exp"];b["theta"]+=x["theta_exp"];b["rho"]+=x["rho_exp"]
            if x["type"]=="call": b["call_oi"]+=x["oi"];b["call_vol"]+=x["volume"];b["call_oi_change"]+=x["oi_change"]or 0
            else: b["put_oi"]+=x["oi"];b["put_vol"]+=x["volume"];b["put_oi_change"]+=x["oi_change"]or 0
        ek=["gex","dex","vanna_exp","charm_exp","volga_exp","speed_exp","theta_exp","rho_exp"]
        net={k:sum(x[k]for x in enriched)for k in ek}
        # Fix #12: walls use GEX-weighted OI rather than raw OI for better level identification.
        # A strike with moderate OI but high gamma (near ATM) is a stronger wall than
        # a far-OTM strike with enormous but low-gamma open interest.
        def _weighted_call_wall():
            best=None;best_val=0
            for k,v in bs.items():
                val=v["call_oi"]*max(abs(v["gex"]),1e-8)
                if val>best_val: best_val=val;best=k
            return best
        def _weighted_put_wall():
            best=None;best_val=0
            for k,v in bs.items():
                val=v["put_oi"]*max(abs(v["gex"]),1e-8)
                if val>best_val: best_val=val;best=k
            return best
        cw=_weighted_call_wall();pw=_weighted_put_wall()
        gw=max(bs,key=lambda k:abs(bs[k]["gex"]),default=None);vt=max(bs,key=lambda k:abs(bs[k]["volga"]),default=None)
        return{"enriched":enriched,"by_strike":bs,"net":{"gex":net["gex"],"dex":net["dex"],"vanna":net["vanna_exp"],"charm":net["charm_exp"],"volga":net["volga_exp"],"speed":net["speed_exp"],"theta":net["theta_exp"],"rho":net["rho_exp"]},"walls":{"call_wall":cw,"put_wall":pw,"gamma_wall":gw,"vol_trigger":vt},"units":UNITS}
    def _mk(self,r,sp,dto=0):
        """Create shifted OptionRow for sweep/decay/scenario.
        DESIGN: vendor Greek overrides (delta, gamma, etc.) are intentionally NOT carried
        over because shifting spot or DTE invalidates them. Model Greeks are recalculated
        at the new (spot, T) by _enrich(). This is the correct behavior for sweeps."""
        return OptionRow(option_type=r.option_type,strike=r.strike,days_to_exp=max(r.days_to_exp-dto,0.01),iv=r.iv,open_interest=r.open_interest,underlying_price=sp,contract_size=r.contract_size,volume=r.volume,dividend_yield=r.dividend_yield,exercise_style=r.exercise_style,bid=r.bid,ask=r.ask,last=r.last,oi_change=r.oi_change,dealer_sign_confidence=r.dealer_sign_confidence,inferred_side=r.inferred_side)
    def _fz(self,pts):
        for i in range(1,len(pts)):
            p0,v0=pts[i-1];p1,v1=pts[i]
            if v0==0: return round(p0,2)
            if v0*v1<0: w=abs(v0)/(abs(v0)+abs(v1));return round(p0+(p1-p0)*w,2)
        return None
    def _dg(self,rows,pct=0.25):
        # v9 (Patch 8): widen ExposureEngine default sweep band from ±10% to ±25%.
        # _dg is used by ExposureEngine.gamma_flip / vanna_flip when no grid is
        # passed, and by DecaySimulator. ExposureEngine has no UnifiedIVSurface in
        # scope, so _dg stays blanket-pct rather than IV-aware (the IV-aware path
        # lives in InstitutionalExpectationEngine._grid). 201 steps preserved.
        S=rows[0].underlying_price if rows else 100;return[S*(1-pct+2*pct/200*i)for i in range(201)]
    def gamma_flip(self,rows,pg=None):
        if pg is None: pg=self._dg(rows)
        return self._fz([(sp,self.compute([self._mk(r,sp)for r in rows])["net"]["gex"])for sp in pg])
    def vanna_flip(self,rows,pg=None):
        if pg is None: pg=self._dg(rows)
        return self._fz([(sp,self.compute([self._mk(r,sp)for r in rows])["net"]["vanna"])for sp in pg])

# ── 13. DECAY / SCENARIO / TERM STRUCTURE ────────────────────────

class DecaySimulator:
    def __init__(self,e): self.e=e
    def project(self,rows,df=5,step=1.0):
        sn=[]
        for d in range(df+1):
            o=d*step;adj=[self.e._mk(r,r.underlying_price,o)for r in rows];res=self.e.compute(adj)
            sn.append({"day_offset":o,"net_gex":res["net"]["gex"],"net_dex":res["net"]["dex"],"net_vanna":res["net"]["vanna"],"net_charm":res["net"]["charm"],"net_volga":res["net"]["volga"],"gamma_flip":self.e.gamma_flip(adj),"walls":res["walls"]})
        return sn

class ScenarioEngine:
    def __init__(self,e): self.e=e
    def spot_vol_matrix(self,rows,ss=None,vs=None,metric="gex"):
        if ss is None: ss=[-0.10,-0.05,-0.02,0,0.02,0.05,0.10]
        if vs is None: vs=[-0.10,-0.05,0,0.05,0.10,0.20]
        S0=rows[0].underlying_price if rows else 100;mx=[]
        for ds in ss:
            rv=[]
            for dv in vs:
                sh=[OptionRow(option_type=r.option_type,strike=r.strike,days_to_exp=r.days_to_exp,iv=max(r.iv+dv,0.01),open_interest=r.open_interest,underlying_price=r.underlying_price*(1+ds),contract_size=r.contract_size,volume=r.volume,dividend_yield=r.dividend_yield,exercise_style=r.exercise_style,dealer_sign_confidence=r.dealer_sign_confidence,inferred_side=r.inferred_side)for r in rows]
                rv.append(self.e.compute(sh)["net"].get(metric,0))
            mx.append(rv)
        return{"spot_shocks":ss,"vol_shocks":vs,"metric":metric,"base_spot":S0,"matrix":mx}

def term_structure(rows,engine,buckets=None):
    if buckets is None: buckets=[(0,2),(2,7),(7,30),(30,90),(90,9999)]
    res=[]
    for lo,hi in buckets:
        br=[r for r in rows if lo<=r.days_to_exp<hi]
        if not br: res.append({"bucket":f"{lo}-{hi}d","count":0,"net_gex":0,"net_dex":0,"net_vanna":0,"net_charm":0,"net_volga":0});continue
        c=engine.compute(br);res.append({"bucket":f"{lo}-{hi}d","count":len(br),"net_gex":c["net"]["gex"],"net_dex":c["net"]["dex"],"net_vanna":c["net"]["vanna"],"net_charm":c["net"]["charm"],"net_volga":c["net"]["volga"]})
    return res

# ── 14. EVENT-VOL LAYER ─────────────────────────────────────────

class EventVolEngine:
    # Event-type variance multipliers: earnings are spikier, macro is more diffuse
    _TYPE_SCALE={"earnings":1.0,"fomc":0.85,"cpi":0.80,"expiry":0.60,"dividend":0.30,"generic":0.90}
    @staticmethod
    def event_variance(e):
        scale=EventVolEngine._TYPE_SCALE.get(e.event_type,0.90)
        return(e.implied_move**2)*e.confidence*scale
    @staticmethod
    def adjust_iv(base_iv,T,events=None,product=None):
        """Add event variance. If product is specified, only include matching events."""
        if not events: return base_iv
        bv=base_iv**2*T
        ev=sum(EventVolEngine.event_variance(e)for e in events
               if e.days_until<=T*365 and(product is None or not e.product or e.product==product))
        return safe_sqrt((bv+ev)/max(T,1e-8))
    @staticmethod
    def strip_event_vol(total_iv,T,events=None,product=None):
        if not events: return total_iv
        tv=total_iv**2*T
        ev=sum(EventVolEngine.event_variance(e)for e in events
               if e.days_until<=T*365 and(product is None or not e.product or e.product==product))
        return safe_sqrt(max(tv-ev,0)/max(T,1e-8))
    @staticmethod
    def event_adjusted_expected_move(S,base_iv,T,events=None,product=None):
        ai=EventVolEngine.adjust_iv(base_iv,T,events,product)
        return{"base_move":expected_move_from_iv(S,base_iv,T),"adjusted_move":expected_move_from_iv(S,ai,T),"base_iv":base_iv,"adjusted_iv":ai}

# ── 15. BARRIER / PATH MODEL ────────────────────────────────────

class BarrierModel:
    @staticmethod
    def analytic_touch(S,H,T,r,q,sigma):
        """Continuous-monitoring barrier-touch via reflection principle.
        NOTE: This assumes flat vol (GBM). For skew-consistent touch probabilities,
        use monte_carlo_touch with a calibrated local vol or stochastic vol model.
        The analytic result is a clean lower bound for most skew regimes."""
        if sigma<=0 or T<=0: return 0
        if H==S: return 1
        sigma=max(sigma,1e-8);T=max(T,1e-8)
        lam=(r-q-0.5*sigma**2)/(sigma**2);sT=sigma*safe_sqrt(T)
        lnHS=math.log(H/S)
        a=(lnHS+(r-q+0.5*sigma**2)*T)/sT;b=(lnHS-(r-q+0.5*sigma**2)*T)/sT
        try: pw=(H/S)**(2*lam)
        except: pw=0
        if H>S: p=norm_cdf(-a)+pw*norm_cdf(b)
        else: p=norm_cdf(a)+pw*norm_cdf(-b)
        return clamp(p,0,1)
    @staticmethod
    def skew_adjusted_touch(S,H,T,r,q,atm_sigma,barrier_sigma):
        """Use barrier-strike IV (not ATM IV) for more accurate touch probability.
        barrier_sigma should be the IV at strike=H from the vol surface."""
        return BarrierModel.analytic_touch(S,H,T,r,q,barrier_sigma)
    @staticmethod
    def monte_carlo_touch(S,H,T,r,q,sigma,n_paths=10000,n_steps=252,jump_intensity=0,jump_mean=0,jump_std=0,seed=None,product_type="equity"):
        """MC touch with optional Merton jump-diffusion."""
        if seed is not None: random.seed(seed)
        dt=T/n_steps;sdt=math.sqrt(dt);drift=(r-q-0.5*sigma**2)*dt
        if jump_intensity>0: drift-=jump_intensity*(math.exp(jump_mean+0.5*jump_std**2)-1)*dt
        touches=0
        for _ in range(n_paths):
            s=S;hit=False
            for _ in range(n_steps):
                ds=drift+sigma*sdt*random.gauss(0,1)
                if jump_intensity>0 and random.random()<jump_intensity*dt: ds+=random.gauss(jump_mean,jump_std)
                s*=math.exp(ds)
                if(H>S and s>=H)or(H<S and s<=H): hit=True;break
            if hit: touches+=1
        p=touches/n_paths;se=math.sqrt(p*(1-p)/max(n_paths,1))
        return{"prob_touch":p,"std_error":se,"n_paths":n_paths,"method":"MC"+(" + jumps"if jump_intensity>0 else"")}

# ── 16. REGIME CLASSIFIER ───────────────────────────────────────

def gex_regime(g):
    if g<0: return{"regime":"NEGATIVE GAMMA","style":"Trending / Explosive","note":"MMs amplify moves","preferred":"Debit spreads, momentum","avoid":"Iron condors"}
    return{"regime":"POSITIVE GAMMA","style":"Range-bound / Vol suppression","note":"MMs suppress moves","preferred":"Credit spreads, mean reversion","avoid":"Chasing breakouts"}
def vanna_charm_context(v,c):
    return{"vanna":"Vanna tailwind"if v>0 else"Vanna headwind","charm":"Charm tailwind"if c>0 else"Charm headwind"}
def composite_regime(net):
    gs=40 if net["gex"]>0 else(-40 if net["gex"]<0 else 0);vs=25 if net["vanna"]>0 else(-25 if net["vanna"]<0 else 0)
    cs=20 if net["charm"]>0 else(-20 if net["charm"]<0 else 0);vos=15 if net.get("volga",0)>0 else(-15 if net.get("volga",0)<0 else 0)
    t=gs+vs+cs+vos
    if t>=50: rg,st,pl="STRONG PIN","Tight range, vol crush","Iron condors, butterflies"
    elif t>=20: rg,st,pl="MODERATE PIN","Range-bound, mild drift","Credit spreads"
    elif t>=-20: rg,st,pl="NEUTRAL / TRANSITION","Mixed signals","Straddles, calendars"
    elif t>=-50: rg,st,pl="MODERATE TREND","Directional bias","Debit spreads"
    else: rg,st,pl="STRONG TREND / EXPLOSIVE","High conviction trending","Momentum, long gamma"
    return{"composite_score":t,"regime":rg,"style":st,"preferred_plays":pl,"components":{"gex":gs,"vanna":vs,"charm":cs,"volga":vos},"context":{**gex_regime(net["gex"]),**vanna_charm_context(net["vanna"],net["charm"])}}

# ── 17. UNIFIED IV SURFACE ──────────────────────────────────────

class UnifiedIVSurface:
    """Uses ExposureEngine.resolve_iv() so ladder + exposure share same IV."""
    def __init__(self,rows,engine): self.rows=rows;self.engine=engine
    def representative_iv(self,spot):
        # Fix #14: Use sqrt(1/dte) weighting instead of 1/dte.
        # This still favours near-term IVs but prevents a 1-DTE contract from
        # getting 30x the weight of a 30-DTE contract (which distorts representative IV).
        # sqrt(1/1)=1.0  vs  sqrt(1/30)=0.18  — a 5.4x ratio instead of 30x.
        import math as _m
        ws=tw=0
        for r in self.rows:
            T=year_fraction(r.days_to_exp);iv=self.engine.resolve_iv(spot,r.strike,T,r.dividend_yield,r.iv)
            mp=abs(r.strike-spot)/max(spot,1e-8);dw=1/(1+10*mp)
            tw_=_m.sqrt(1/max(r.days_to_exp,1))  # gentler time-weighting
            ow=max(r.open_interest,1)
            w=dw*tw_*ow;ws+=iv*w;tw+=w
        return ws/max(tw,1e-8)
    def representative_dte(self):
        n=d=0
        for r in self.rows: w=max(r.open_interest,1);n+=r.days_to_exp*w;d+=w
        return n/max(d,1e-8)
    def strike_iv(self,strike,spot):
        if not self.rows: return 0
        near=sorted(self.rows,key=lambda r:abs(r.strike-strike))
        if abs(strike-spot)/max(spot,1e-8)<=0.005: pool=sorted(self.rows,key=lambda r:abs(r.strike-spot))[:6]
        else:
            side="call"if strike>spot else"put";sr=[r for r in near if r.option_type.lower()==side]
            pool=sr[:4]if sr else near[:4]
        n=d=0
        for r in pool:
            T=year_fraction(r.days_to_exp);iv=self.engine.resolve_iv(spot,r.strike,T,r.dividend_yield,r.iv)
            ow=max(r.open_interest,1);n+=iv*ow;d+=ow
        return n/max(d,1e-8)

# ── 18. POSITIONING + LIQUIDITY + VOL REGIME ─────────────────────

class PositioningEngine:
    @staticmethod
    def estimate_opening_pressure(rows):
        cs=ps=ts=0
        for r in rows:
            oi=max(r.open_interest,1);vi=r.volume/oi
            if r.oi_change is not None: or_=clamp(r.oi_change/max(r.volume,1),-1,1);sc=0.7*or_+0.3*clamp(vi/2,0,1)
            else: sc=clamp((vi-0.5)/1.5,-1,1)
            ws=sc*max(r.volume,1)
            if r.option_type.lower()=="call": cs+=ws
            else: ps+=ws
            ts+=abs(ws)
        cn=cs/max(ts,1);pn=ps/max(ts,1);nn=cn-pn
        lb="CALL OPENING DOMINANT"if nn>0.15 else("PUT OPENING DOMINANT"if nn<-0.15 else"MIXED / CHURN")
        return{"call_open_score":round(cn,4),"put_open_score":round(pn,4),"net_opening_bias":round(nn,4),"label":lb}

class LiquidityEngine:
    @staticmethod
    def liquidity_score(ctx):
        s=1.0;adv=ctx.avg_daily_dollar_volume;idv=ctx.intraday_dollar_volume;dep=ctx.orderbook_depth_dollars;sp=ctx.bid_ask_spread_pct
        if adv is not None:
            if adv>=80e9: s*=0.80
            elif adv>=25e9: s*=0.90
            elif adv>=5e9: s*=1.00
            else: s*=1.10
        if idv is not None and adv is not None:
            p=idv/max(adv,1)
            if p<0.15: s*=1.10
            elif p>0.55: s*=0.92
        if dep is not None:
            if dep>=2e9: s*=0.85
            elif dep>=500e6: s*=0.95
            else: s*=1.08
        if sp is not None:
            if sp>0.003: s*=1.08
            elif sp<0.001: s*=0.96
        return clamp(s,0.75,1.30)
    @staticmethod
    def calibrated_impact(net,ctx):
        """Semi-calibrated impact model. Uses realized spread, return vol, and
        ADV participation to estimate an impact coefficient, then applies
        dealer flow pressure through that coefficient.
        Falls back to heuristic tiers only when calibration inputs are absent."""
        spot=ctx.spot
        # Step 1: Estimate impact coefficient eta ($ move per $ flow)
        # Almgren-Chriss style: eta ~ sigma_daily / sqrt(ADV)
        # We use intraday return vol as sigma proxy
        eta=None
        if ctx.intraday_return_vol is not None and ctx.avg_daily_dollar_volume is not None:
            sigma_d=max(ctx.intraday_return_vol,1e-6)
            adv=max(ctx.avg_daily_dollar_volume,1e6)
            eta=sigma_d/math.sqrt(adv)*1e4  # normalize to sensible scale
        # Step 2: Realized spread adjustment
        spread_adj=1.0
        if ctx.bid_ask_spread_pct is not None:
            # Wider spread = less resilient book = more impact
            spread_adj=1.0+clamp((ctx.bid_ask_spread_pct-0.001)*50,-0.15,0.20)
        # Step 3: Depth adjustment
        depth_adj=1.0
        if ctx.orderbook_depth_dollars is not None:
            # Thinner book = more impact
            depth_adj=clamp(1e9/max(ctx.orderbook_depth_dollars,1e6),0.80,1.25)
        # Step 4: ADV participation adjustment
        part_adj=1.0
        if ctx.intraday_dollar_volume is not None and ctx.avg_daily_dollar_volume is not None:
            part=ctx.intraday_dollar_volume/max(ctx.avg_daily_dollar_volume,1)
            part_adj=1.0+clamp((0.35-part)*0.3,-0.10,0.10)  # low participation = more impact
        # Step 5: Flow pressure through coefficient
        if eta is not None:
            # Use calibrated coefficient
            gex_pressure=clamp(-net["gex"]*eta/max(spot,1),-0.40,0.40)
            vanna_pressure=clamp(net["vanna"]*eta*0.3/max(spot,1),-0.15,0.15)
            charm_pressure=clamp(net["charm"]*eta*0.2/max(spot,1),-0.12,0.12)
        else:
            # Fallback: original heuristic
            gex_pressure=clamp(-net["gex"]/max(spot**2*80000,1),-0.35,0.35)
            vanna_pressure=clamp(net["vanna"]/max(spot*8e6,1),-0.12,0.12)
            charm_pressure=clamp(net["charm"]/max(spot*8e6,1),-0.10,0.10)
        base=1.0+gex_pressure+0.5*vanna_pressure+0.4*charm_pressure
        base*=spread_adj*depth_adj*part_adj
        return clamp(base,0.65,1.55)

class VolatilityRegimeEngine:
    @staticmethod
    def compute_vrp(iv,rv): return(iv-rv)if rv is not None else None
    @staticmethod
    def regime_label(iv,rv):
        if rv is None: return"UNKNOWN RV"
        vrp=iv-rv
        if vrp>0.08: return"IV RICH / PREMIUM SELLING"
        if vrp<-0.03: return"IV CHEAP / BREAKOUT RISK"
        return"BALANCED VOL"
    @staticmethod
    def regime_multiplier(iv,rv):
        if rv is None: return 1.0
        vrp=iv-rv
        if vrp>0.10: return 0.92
        if vrp>0.04: return 0.96
        if vrp<-0.05: return 1.10
        if vrp<0: return 1.04
        return 1.0

# ── 19. INSTITUTIONAL EXPECTATION ENGINE ─────────────────────────

class InstitutionalExpectationEngine:
    """Full-stack: exposure -> flow adjustment -> expected move -> card."""
    def __init__(self,r=0.04,vol_surface=None,sabr_params=None):
        self.r=r;self.ee=ExposureEngine(r=r,vol_surface=vol_surface,sabr_params=sabr_params)
    def _bl(self,s): return"UPSIDE"if s>=0.35 else("DOWNSIDE"if s<=-0.35 else"NEUTRAL")
    def _gl(self,g): return"NEG GAMMA / TRENDING"if g<0 else"POS GAMMA / SUPPRESSIVE"
    def _ps(self,net,spot,pos,rows=None):
        # Scale by chain-level dollar-weighted OI so the normalizer adapts to
        # chain depth rather than assuming ~M DEX (only correct for large-caps).
        if rows:
            chain_scale = max(sum(r.open_interest*r.underlying_price*r.contract_size
                                  for r in rows),spot*1e4)
        else:
            chain_scale = max(spot*6e6,1)
        dc=clamp(net["dex"]/chain_scale,-0.55,0.55);vc=clamp(net["vanna"]/chain_scale,-0.25,0.25)
        cc=clamp(net["charm"]/chain_scale,-0.20,0.20);pc=clamp(pos["net_opening_bias"],-0.25,0.25)
        return clamp(0.45*dc+0.25*vc+0.15*cc+0.15*pc,-1,1)
    def _grid(self,spot,pct=None,steps=121,iv=None,dte_years=None):
        # v9 (Patch 8): IV-aware band widening. When pct is not specified and
        # both iv and dte_years are passed, scale to ~3σ daily move with floor
        # 0.15 and ceiling 0.40. Validated against MRNA(0.21)/AMD(0.34)/
        # ARM(0.40,clamped)/LLY(0.15,clamped) Patch 7.1 dataset. Callers without
        # iv context fall through to a 0.25 blanket default — matches _dg.
        if pct is None:
            if iv is not None and dte_years is not None:
                sigma=iv*(dte_years**0.5)
                pct=max(0.15,min(0.40,3.0*sigma))
            else:
                pct=0.25
        lo=spot*(1-pct);hi=spot*(1+pct);st=(hi-lo)/max(steps-1,1)
        return[round(lo+i*st,2)for i in range(steps)]
    def _ladder(self,rows,ctx,ivs,events=None,audit=None):
        us=sorted({r.strike for r in rows});rd=ivs.representative_dte();T=year_fraction(rd)
        qa=sum(r.dividend_yield for r in rows)/max(len(rows),1);ld=[]
        has_events=bool(events)
        for K in us:
            siv=ivs.strike_iv(K,ctx.spot)
            if events: siv=EventVolEngine.adjust_iv(siv,T,events)
            pa=prob_finish_above(ctx.spot,K,T,self.r,qa,siv);pb=prob_finish_below(ctx.spot,K,T,self.r,qa,siv)
            # Item 10: Route touch probability through TouchRouter
            touch_result=TouchRouter.route(ctx.spot,K,T,self.r,qa,siv,
                barrier_sigma=None,has_events=has_events,audit=audit)
            ld.append({"strike":round(K,2),"iv_used":round(siv,4),"prob_finish_above":round(pa,4),
                "prob_finish_below":round(pb,4),"prob_touch":touch_result["prob_touch"],
                "touch_method":touch_result["method"]})
        return ld
    def snapshot(self,rows,ctx,liquid_index=False):
        if not rows: return{}
        audit=AuditLog()
        # Item 4: Validate inputs
        clean,quarantined,ctx_issues=InputValidator.validate_chain(rows,ctx,audit)
        if not clean: return{"error":"all rows quarantined","quarantined":len(quarantined),"schema_version":SCHEMA_VERSION}
        if ctx_issues: audit.log("ctx_warning",issues=ctx_issues)
        # Item 2: Pure copies — never mutate originals
        enriched_rows=[OptionRow(option_type=r.option_type,strike=r.strike,days_to_exp=r.days_to_exp,iv=r.iv,open_interest=r.open_interest,underlying_price=r.underlying_price,contract_size=r.contract_size,volume=r.volume,dividend_yield=r.dividend_yield,exercise_style=r.exercise_style,bid=r.bid,ask=r.ask,last=r.last,oi_change=r.oi_change,trades=r.trades,inferred_side=r.inferred_side,dealer_sign_confidence=r.dealer_sign_confidence,delta=r.delta,gamma=r.gamma,vanna=r.vanna,charm=r.charm,volga=r.volga,speed=r.speed,theta=r.theta,rho=r.rho)for r in clean]
        # Item 1: Enrich trade signs + spread detection (loosened for liquid indices)
        enriched_rows,spread_groups=TradeSignEngine.enrich_rows(enriched_rows, liquid_index=liquid_index)
        spread_leg_indices=set()
        for g in spread_groups:
            for idx in g["indices"]: spread_leg_indices.add(idx)
            audit.log("spread_detected",type=g["type"],legs=len(g["indices"]))
        spread_leg_pct=len(spread_leg_indices)/max(len(enriched_rows),1)
        # Item 8: Compute RV locally using canonical policy — never mutate ctx
        rv20=ctx.realized_vol_20d;rv10=ctx.realized_vol_10d;rv5=ctx.realized_vol_5d
        rv_source="external"
        if ctx.recent_bars and rv20 is None:
            rv20=RVPolicy.get_vrp_rv(ctx.recent_bars,20)
            rv10=RVPolicy.get_vrp_rv(ctx.recent_bars,10)
            rv5=RVPolicy.get_vrp_rv(ctx.recent_bars,5)
            rv_source="yang_zhang_from_bars"
        audit.log("rv_source",source=rv_source,rv20=rv20,rv10=rv10,rv5=rv5)
        # Normalize to ctx.spot
        nr=[OptionRow(option_type=r.option_type,strike=r.strike,days_to_exp=r.days_to_exp,iv=r.iv,open_interest=r.open_interest,underlying_price=ctx.spot,contract_size=r.contract_size,volume=r.volume,dividend_yield=r.dividend_yield,exercise_style=r.exercise_style,delta=r.delta,gamma=r.gamma,vanna=r.vanna,charm=r.charm,volga=r.volga,speed=r.speed,theta=r.theta,rho=r.rho,bid=r.bid,ask=r.ask,last=r.last,oi_change=r.oi_change,trades=r.trades,inferred_side=r.inferred_side,dealer_sign_confidence=r.dealer_sign_confidence)for r in enriched_rows]
        exp=self.ee.compute(nr);ivs=UnifiedIVSurface(nr,self.ee);pos=PositioningEngine.estimate_opening_pressure(nr)
        riv=ivs.representative_iv(ctx.spot);rdte=ivs.representative_dte();T=year_fraction(rdte);ev=ctx.events
        audit.log("iv_source",surface="svi" if self.ee.vol_surface else("sabr" if self.ee.sabr_params else "row"))
        aiv=EventVolEngine.adjust_iv(riv,T,ev)if ev else riv
        if ev: audit.log("event_vol",base_iv=round(riv,4),adjusted_iv=round(aiv,4),n_events=len(ev))
        r1s=expected_move_from_iv(ctx.spot,riv,T);a1b=expected_move_from_iv(ctx.spot,aiv,T)
        # Item 2/3: Impact model with audit
        fm=LiquidityEngine.calibrated_impact(exp["net"],ctx);lm=LiquidityEngine.liquidity_score(ctx)
        audit.log("impact_model",flow_mult=round(fm,4),liq_mult=round(lm,4))
        rm=VolatilityRegimeEngine.regime_multiplier(aiv,rv20)
        sm=1.0
        if ctx.is_0dte: sm=0.85+0.30*clamp(1-ctx.session_progress,0,1)
        a1s=a1b*fm*lm*rm*sm;pr=self._ps(exp["net"],ctx.spot,pos,rows=nr);bias=self._bl(pr)
        cs=a1s*0.40*pr;ec=ctx.spot+cs
        # v9 (Patch 8): pass IV+DTE so _grid can widen sweep band proportionally
        # for high-IV tickers. Same grid is reused by both gamma_flip and
        # vanna_flip, so IV-aware widening flows to both — intentional.
        # Using riv (raw representative IV), not aiv (event-adjusted): keeps
        # the sweep band stable across earnings/FOMC windows. Empirical formula
        # in _grid was validated against riv-equivalent IVs from Patch 7.1.
        grid=self._grid(ctx.spot,iv=riv,dte_years=T);gf=self.ee.gamma_flip(nr,grid);vf=self.ee.vanna_flip(nr,grid)
        # Item 10: Touch routing for ladder
        ladder=self._ladder(nr,ctx,ivs,ev,audit)
        bst=exp["by_strike"];walls=exp["walls"]
        def fl(strike):
            if strike is None: return None
            for x in ladder:
                if abs(x["strike"]-strike)<1e-9: return x
            return None
        vrp20=VolatilityRegimeEngine.compute_vrp(aiv,rv20)
        vrp10=VolatilityRegimeEngine.compute_vrp(aiv,rv10)
        vrp5=VolatilityRegimeEngine.compute_vrp(aiv,rv5)
        reg=composite_regime(exp["net"]);decay=DecaySimulator(self.ee).project(nr,5);ts=term_structure(nr,self.ee)
        nb=sum(1 for r in nr if r.inferred_side==TradeSide.BUY);ns=sum(1 for r in nr if r.inferred_side==TradeSide.SELL)
        ac=sum(r.dealer_sign_confidence for r in nr)/max(len(nr),1)
        audit.log("trade_sign_summary",buy=nb,sell=ns,avg_conf=round(ac,3),spread_leg_pct=round(spread_leg_pct,3))
        # Item 5: Composite confidence
        quality=DataQualityEngine.score(nr,ctx)
        confidence=ConfidenceEngine.compute(quality,ac,spread_leg_pct)
        # Item 12: Downgrade flags
        downgrades=DataQualityEngine.downgrade_flags(quality)
        if downgrades: audit.log("downgrades",flags=downgrades)
        # Item 15: Frozen schema output
        return{"schema_version":SCHEMA_VERSION,"spot":round(ctx.spot,2),"representative_iv":round(riv,4),"event_adjusted_iv":round(aiv,4),"representative_dte":round(rdte,2),"raw_expected_move":{"move_1sigma":round(r1s,2),"move_2sigma":round(r1s*2,2),"range_1sigma":{"low":round(ctx.spot-r1s,2),"high":round(ctx.spot+r1s,2)}},"adjusted_expectation":{"move_1sigma":round(a1s,2),"move_2sigma":round(a1s*2,2),"expected_center":round(ec,2),"expected_low":round(ec-a1s,2),"expected_high":round(ec+a1s,2),"bias_score":round(pr,3),"bias":bias},"multipliers":{"flow":round(fm,3),"liquidity":round(lm,3),"vrp":round(rm,3),"session":round(sm,3)},"dealer_flows":{**{k:round(v,2)for k,v in exp["net"].items()},"gex_regime":self._gl(exp["net"]["gex"]),"gamma_flip":gf,"vanna_flip":vf},"trade_sign":{"inferred_buy_rows":nb,"inferred_sell_rows":ns,"avg_confidence":round(ac,3),"spread_leg_pct":round(spread_leg_pct,3),"spread_groups":[g["type"]for g in spread_groups]},"confidence":confidence,"data_quality":quality,"downgrades":downgrades,"regime":reg,"volatility_regime":{"realized_vol_20d":None if rv20 is None else round(rv20,4),"realized_vol_10d":None if rv10 is None else round(rv10,4),"realized_vol_5d":None if rv5 is None else round(rv5,4),"vrp_20d":None if vrp20 is None else round(vrp20,4),"vrp_10d":None if vrp10 is None else round(vrp10,4),"vrp_5d":None if vrp5 is None else round(vrp5,4),"label":VolatilityRegimeEngine.regime_label(aiv,rv20),"rv_source":rv_source},"positioning":pos,"walls":{"call_wall":walls["call_wall"],"put_wall":walls["put_wall"],"gamma_wall":walls["gamma_wall"],"vol_trigger":walls.get("vol_trigger"),"call_wall_stats":None if walls["call_wall"]is None else bst[walls["call_wall"]],"put_wall_stats":None if walls["put_wall"]is None else bst[walls["put_wall"]],"gamma_wall_stats":None if walls["gamma_wall"]is None else bst[walls["gamma_wall"]]},"wall_probabilities":{"call_wall":fl(walls["call_wall"]),"put_wall":fl(walls["put_wall"]),"gamma_wall":fl(walls["gamma_wall"])},"strike_probability_ladder":ladder,"decay_path":decay,"term_structure":ts,"units":UNITS,"quarantined":len(quarantined),"audit_log":audit.summary()}
    def institutional_card(self,rows,ctx):
        s=self.snapshot(rows,ctx)
        if not s or "error" in s: return s.get("error","No data.")
        a=s["adjusted_expectation"];r=s["raw_expected_move"];f=s["dealer_flows"];v=s["volatility_regime"]
        w=s["walls"];wp=s["wall_probabilities"];p=s["positioning"];rg=s["regime"];ts=s["trade_sign"]
        conf=s["confidence"];dg=s["downgrades"]
        fp=lambda x:f"{x*100:.1f}%"if x is not None else"n/a"
        cwt=wp["call_wall"]["prob_touch"]if wp["call_wall"]else None
        pwt=wp["put_wall"]["prob_touch"]if wp["put_wall"]else None
        gwt=wp["gamma_wall"]["prob_touch"]if wp["gamma_wall"]else None
        L=[f"[v{SCHEMA_VERSION}] Spot: {s['spot']:.2f} | IV: {s['representative_iv']:.2%}"+
           (f" (event-adj: {s['event_adjusted_iv']:.2%})"if s['event_adjusted_iv']!=s['representative_iv']else"")+
           f" | DTE: {s['representative_dte']:.2f}",
           f"Confidence: {conf['label']} ({conf['composite']:.0%}) | Quarantined: {s.get('quarantined',0)}",""]
        if dg: L.append("⚠ DOWNGRADES: "+"; ".join(dg));L.append("")
        L+=[
           "EXPECTED MOVE",f"  Raw 1σ: ±{r['move_1sigma']:.2f}  ({r['range_1sigma']['low']:.2f} – {r['range_1sigma']['high']:.2f})","",
           "DEALER-ADJUSTED EXPECTATION",f"  Bias: {a['bias']}  (score {a['bias_score']:+.3f})",f"  Center: {a['expected_center']:.2f}",f"  1σ Range: {a['expected_low']:.2f} – {a['expected_high']:.2f}","",
           "FLOW STATE",f"  GEX:   {f['gex']:+,.1f}   → {f['gex_regime']}",f"  DEX:   {f['dex']:+,.1f}",f"  Vanna: {f['vanna']:+,.1f}   Volga: {f['volga']:+,.1f}",f"  Charm: {f['charm']:+,.1f}",f"  Gamma Flip: {f['gamma_flip']}  |  Vanna Flip: {f['vanna_flip']}","",
           "TRADE SIGN",f"  Buy: {ts['inferred_buy_rows']}  Sell: {ts['inferred_sell_rows']}  Conf: {ts['avg_confidence']:.1%}  Spreads: {ts['spread_leg_pct']:.0%}","",
           "REGIME",f"  {rg['regime']}  (score {rg['composite_score']:+d})",f"  {rg['style']}","",
           "VOL REGIME",f"  {v['label']} (source: {v.get('rv_source','N/A')})",f"  RV20: {v['realized_vol_20d']} | VRP20: {v['vrp_20d']}","",
           "POSITIONING",f"  {p['label']} | net bias {p['net_opening_bias']:+.3f}","",
           "KEY LEVELS",f"  Call Wall:   {w['call_wall']}  | touch {fp(cwt)}",f"  Put Wall:    {w['put_wall']}  | touch {fp(pwt)}",f"  Gamma Wall:  {w['gamma_wall']} | touch {fp(gwt)}"]
        if w.get("vol_trigger"): L.append(f"  Vol Trigger: {w['vol_trigger']}")
        return"\n".join(L)

# ── 20. CONVENIENCE ──────────────────────────────────────────────

def quick_analysis(rows,r=0.04,vol_surface=None,sabr_params=None):
    e=ExposureEngine(r=r,vol_surface=vol_surface,sabr_params=sabr_params);res=e.compute(rows)
    return{"exposures":res,"gamma_flip":e.gamma_flip(rows),"vanna_flip":e.vanna_flip(rows),"decay_path":DecaySimulator(e).project(rows,5),"term_structure":term_structure(rows,e),"regime":composite_regime(res["net"])}

# ── REGRESSION TESTS (Item 13) ──────────────────────────────────

def run_regression_tests():
    """Edge case tests. Returns (passed, failed, details)."""
    passed=[];failed=[]
    def check(name,cond):
        if cond: passed.append(name)
        else: failed.append(name)
    # Near-zero DTE
    r=OptionRow("call",100,0.01,0.30,1000,100);e=ExposureEngine()
    res=e.compute([r]);check("near_zero_dte",res["net"]["gex"]!=0)
    # Deep ITM call
    r=OptionRow("call",50,30,0.25,500,100);res=e.compute([r])
    check("deep_itm_call",abs(res["net"]["dex"])>0)
    # Deep OTM put
    r=OptionRow("put",50,30,0.25,500,100);res=e.compute([r])
    check("deep_otm_put",abs(res["net"]["gex"])>=0)  # should not crash
    # Zero rate
    e0=ExposureEngine(r=0.0);r=OptionRow("put",100,30,0.25,1000,100)
    res=e0.compute([r]);check("zero_rate",math.isfinite(res["net"]["gex"]))
    # Dividend-heavy
    r=OptionRow("call",100,30,0.25,1000,100,dividend_yield=0.08)
    res=e.compute([r]);check("high_dividend",math.isfinite(res["net"]["gex"]))
    # American put
    r=OptionRow("put",105,30,0.30,1000,100,exercise_style=ExerciseStyle.AMERICAN,dividend_yield=0.03)
    res=e.compute([r]);check("american_put",res["enriched"][0]["price"]>0)
    # BAW edge: very short T
    p=baw_american_price("put",100,105,0.001,0.05,0.03,0.30)
    check("baw_tiny_T",math.isfinite(p) and p>=0)
    # Empty chain
    res=e.compute([]);check("empty_chain",res["net"]["gex"]==0)
    # Single expiry
    rows=[OptionRow("call",100,7,0.25,500,100),OptionRow("put",100,7,0.25,500,100)]
    res=e.compute(rows);check("single_expiry",len(res["by_strike"])>0)
    # Bad input validation
    bad=OptionRow("call",-10,30,0.25,1000,100)
    ok,issues=InputValidator.validate_row(bad)
    check("validation_negative_strike",not ok and len(issues)>0)
    bad2=OptionRow("call",100,30,15.0,-50,0)  # iv=15, OI=-50, spot=0
    ok2,issues2=InputValidator.validate_row(bad2)
    check("validation_multi_bad",not ok2 and len(issues2)>=2)
    # Heston Feller
    hp=HestonParams(0.04,0.04,2.0,0.5,-0.7)
    check("heston_feller_false",not hp.feller_satisfied())
    hp2=HestonParams(0.04,0.04,5.0,0.3,-0.7)
    check("heston_feller_true",hp2.feller_satisfied())
    # RV engine with minimal bars
    b=[OHLC(100,101,99,100.5)]
    check("rv_single_bar",RealizedVolEngine.yang_zhang(b) is None)
    # Barrier at spot
    check("barrier_at_spot",BarrierModel.analytic_touch(100,100,0.1,0.04,0,0.25)==1.0)
    # Schema version present
    rows=[OptionRow("call",100,7,0.25,500,100)]
    ctx=MarketContext(spot=100)
    eng=InstitutionalExpectationEngine()
    snap=eng.snapshot(rows,ctx)
    check("schema_version",snap.get("schema_version")==SCHEMA_VERSION)
    check("confidence_present","confidence" in snap)
    check("downgrades_present","downgrades" in snap)
    check("audit_present","audit_log" in snap)
    return{"passed":len(passed),"failed":len(failed),"details":{"passed":passed,"failed":failed}}
