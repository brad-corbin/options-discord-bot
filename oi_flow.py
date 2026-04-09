# oi_flow.py
# ═══════════════════════════════════════════════════════════════════
# Unified Institutional Flow Detection Layer
#
# Two-phase flow detection:
#   Phase 1 (Intraday): Volume/OI ratio + volume bursts + direction approx
#   Phase 2 (Morning):  OI confirmation → Confirmed Buildup/Unwinding/Churn
#
# Signal hierarchy:
#   Notable (0.5-1x vol/OI)     → Log only, score modifier +3 to +5
#   Significant (1-2x vol/OI)   → Telegram alert, score boost +5 to +8
#   Extreme (2x+ vol/OI)        → Trade generation, always show income idea
#   Confirmed (morning OI delta) → Stalk alert, highest conviction scoring
#
# All state persisted to Redis via PersistentState — survives redeploy.
# Zero additional API credits for piggyback pulls.
# Forward sweeps use cached mode = 1 credit each.
# ═══════════════════════════════════════════════════════════════════

import logging
import time
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Callable, Tuple

log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════

# Volume thresholds by liquidity tier
VOLUME_TIERS = {
    "index":    {"tickers": {"SPY", "QQQ", "IWM", "DIA"}, "min_volume": 5000},
    "mega_cap": {"tickers": {"AAPL", "NVDA", "MSFT", "AMZN", "META", "TSLA", "GOOGL"},
                 "min_volume": 2000},
    "large_cap": {"tickers": {"AMD", "AVGO", "NFLX", "CRM", "BA", "LLY", "UNH",
                               "JPM", "GS", "CAT", "ORCL", "ARM"},
                  "min_volume": 1000},
    "mid_cap":  {"tickers": set(), "min_volume": 500},  # everything else
}

# Vol/OI ratio classification
VOL_OI_NOTABLE = 0.5       # 50% turnover
VOL_OI_SIGNIFICANT = 1.0   # 100% turnover
VOL_OI_EXTREME = 2.0       # 200% turnover

# Filter parameters
MAX_DIST_FROM_SPOT_PCT = 0.10   # Only strikes within 10% of spot
MIN_OPTION_MID_PRICE = 0.10     # Skip penny options
VOLUME_BURST_THRESHOLD = 1000   # Contracts added in single 5-min pull

# Alert cooldowns (seconds)
ALERT_COOLDOWN_NOTABLE = 0          # No alerts for notable
ALERT_COOLDOWN_SIGNIFICANT = 1800   # 30 min
ALERT_COOLDOWN_EXTREME = 300        # 5 min (urgent)

# Campaign thresholds
CAMPAIGN_MIN_DAYS = 2              # Minimum for "persistent" label
CAMPAIGN_STRONG_DAYS = 3           # "Institutional campaign" label

# Trade generation thresholds
TRADE_GEN_MIN_VOL_OI = 2.0         # Extreme tier
TRADE_GEN_MIN_VOLUME_MULTIPLIER = 2  # 2x the tier minimum

# Scoring impact
SCORE_NOTABLE_ALIGNED = 3
SCORE_NOTABLE_OPPOSING = -2
SCORE_SIGNIFICANT_ALIGNED = 6
SCORE_SIGNIFICANT_OPPOSING = -5
SCORE_EXTREME_ALIGNED = 10
SCORE_EXTREME_OPPOSING = -8
SCORE_CONFIRMED_ALIGNED = 10
SCORE_CONFIRMED_OPPOSING = -10
SCORE_CAMPAIGN_BONUS = 5           # Per consecutive day (max +15)

# Validator boost (can lift 2→3 but not 1→3)
VALIDATOR_BOOST_SIGNIFICANT = 0.8
VALIDATOR_BOOST_EXTREME = 1.2
VALIDATOR_BOOST_CONFIRMED = 1.5

# Full ticker list for flow tracking (union of all scanners)
FLOW_TICKERS = sorted({
    # Indexes
    "SPY", "QQQ", "IWM", "DIA",
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "AMD", "AMZN", "META", "TSLA", "GOOGL",
    # Large-cap
    "NFLX", "COIN", "AVGO", "PLTR", "CRM", "ORCL", "ARM", "SMCI",
    # Financials
    "JPM", "GS",
    # Industrials / Health / Retail
    "BA", "CAT", "LLY", "UNH",
    # Income scanner tickers
    "MRNA",
    # Macro / Sector ETFs
    "GLD", "TLT", "XLF", "XLE", "XLV", "SOXX",
    # Additional from active scanner
    "MSTR", "SOFI",
})

# Sector map for sector flow aggregation
SECTOR_MAP = {
    "SEMICONDUCTOR": {"NVDA", "AMD", "AVGO", "ARM", "SMCI", "SOXX"},
    "BIG_TECH": {"AAPL", "MSFT", "AMZN", "META", "GOOGL"},
    "FINANCIALS": {"JPM", "GS", "XLF", "COIN", "SOFI"},
    "ENERGY": {"XLE"},
    "HEALTH": {"LLY", "UNH", "MRNA", "XLV"},
    "INDUSTRIAL": {"BA", "CAT"},
    "INDEX": {"SPY", "QQQ", "IWM", "DIA"},
    "MACRO": {"GLD", "TLT"},
}


def _get_volume_tier(ticker: str) -> dict:
    """Get volume tier config for a ticker."""
    t = ticker.upper()
    for tier_name, tier_cfg in VOLUME_TIERS.items():
        if t in tier_cfg["tickers"]:
            return tier_cfg
    return VOLUME_TIERS["mid_cap"]


def _get_sector(ticker: str) -> str:
    """Map ticker to sector."""
    t = ticker.upper()
    for sector, tickers in SECTOR_MAP.items():
        if t in tickers:
            return sector
    return "OTHER"


# ═══════════════════════════════════════════════════════════
# CHAIN DATA PARSING
# ═══════════════════════════════════════════════════════════

def parse_chain_volume_oi(chain_data: dict, spot: float) -> List[dict]:
    """
    Extract per-strike volume, OI, and direction approximation from
    a MarketData chain response.

    Returns list of dicts, one per strike/side:
      {strike, side, volume, oi, vol_oi_ratio, last, mid, bid, ask,
       bidSize, askSize, direction_approx, dist_from_spot_pct}
    """
    sym_list = chain_data.get("optionSymbol") or []
    n = len(sym_list)
    if n == 0:
        return []

    def col(name, default=None):
        v = chain_data.get(name, default)
        return v if isinstance(v, list) else [default] * n

    strikes = col("strike", None)
    sides = col("side", "")
    volumes = col("volume", 0)
    ois = col("openInterest", 0)
    lasts = col("last", 0)
    mids = col("mid", 0)
    bids = col("bid", 0)
    asks = col("ask", 0)
    bid_sizes = col("bidSize", 0)
    ask_sizes = col("askSize", 0)

    results = []
    for i in range(n):
        strike = strikes[i]
        side = str(sides[i] or "").lower()
        if strike is None or side not in ("call", "put"):
            continue

        vol = int(volumes[i] or 0)
        oi = int(ois[i] or 0)
        last = float(lasts[i] or 0)
        mid = float(mids[i] or 0)
        bid = float(bids[i] or 0)
        ask = float(asks[i] or 0)
        bid_sz = int(bid_sizes[i] or 0)
        ask_sz = int(ask_sizes[i] or 0)

        # Distance filter
        if spot > 0:
            dist_pct = abs(strike - spot) / spot
            if dist_pct > MAX_DIST_FROM_SPOT_PCT:
                continue
        else:
            dist_pct = 0

        # Mid price filter
        if mid < MIN_OPTION_MID_PRICE and last < MIN_OPTION_MID_PRICE:
            continue

        # Vol/OI ratio
        vol_oi = vol / oi if oi > 0 else (999.0 if vol > 0 else 0)

        # Direction approximation
        direction_approx = "unknown"
        if mid > 0 and last > 0:
            if last > mid * 1.01:
                direction_approx = "buyer_initiated"
            elif last < mid * 0.99:
                direction_approx = "seller_initiated"
            else:
                direction_approx = "neutral"

        # Book imbalance
        book_imbalance = "balanced"
        if bid_sz > 0 and ask_sz > 0:
            ratio = bid_sz / ask_sz
            if ratio > 2.0:
                book_imbalance = "bid_heavy"  # demand
            elif ratio < 0.5:
                book_imbalance = "ask_heavy"  # supply

        results.append({
            "strike": float(strike),
            "side": side,
            "volume": vol,
            "oi": oi,
            "vol_oi_ratio": round(vol_oi, 2),
            "last": last,
            "mid": mid,
            "bid": bid,
            "ask": ask,
            "bidSize": bid_sz,
            "askSize": ask_sz,
            "direction_approx": direction_approx,
            "book_imbalance": book_imbalance,
            "dist_from_spot_pct": round(dist_pct * 100, 2),
        })

    return results


# ═══════════════════════════════════════════════════════════
# CORE FLOW DETECTION
# ═══════════════════════════════════════════════════════════

class FlowDetector:
    """
    Unified institutional flow detection.

    Hooks into every chain pull (piggyback, zero cost).
    Runs dedicated forward sweeps at scheduled times.
    Persists all state to Redis via PersistentState.
    """

    def __init__(self, persistent_state, post_fn: Callable = None):
        """
        persistent_state: PersistentState instance
        post_fn: function(message) — post to Telegram
        """
        self._state = persistent_state
        self._post = post_fn

    # ─────────────────────────────────────────────────────
    # PHASE 1: INTRADAY VOLUME DETECTION
    # ─────────────────────────────────────────────────────

    def check_intraday_flow(self, ticker: str, expiry: str,
                            chain_data: dict, spot: float) -> List[dict]:
        """
        Called on every chain pull. Checks volume/OI ratios, detects
        bursts, approximates direction. Returns list of flow alerts.

        Zero additional API credits — uses data already fetched.
        """
        ticker = ticker.upper()
        tier = _get_volume_tier(ticker)
        min_vol = tier["min_volume"]

        parsed = parse_chain_volume_oi(chain_data, spot)
        if not parsed:
            return []

        # ── Volume burst detection ──
        prev_snapshot = self._state.get_volume_snapshot(ticker, expiry)
        current_vol_map = {}
        for p in parsed:
            key = f"{p['strike']}|{p['side']}"
            current_vol_map[key] = p["volume"]
        self._state.save_volume_snapshot(ticker, expiry, current_vol_map)

        alerts = []
        today_str = date.today().isoformat()

        for p in parsed:
            vol = p["volume"]
            oi = p["oi"]
            vol_oi = p["vol_oi_ratio"]

            # Gate: minimum volume
            if vol < min_vol:
                continue

            # Classify flow level
            if vol_oi >= VOL_OI_EXTREME:
                flow_level = "extreme"
            elif vol_oi >= VOL_OI_SIGNIFICANT:
                flow_level = "significant"
            elif vol_oi >= VOL_OI_NOTABLE:
                flow_level = "notable"
            else:
                continue

            # Volume burst check
            burst = 0
            if prev_snapshot:
                key = f"{p['strike']}|{p['side']}"
                prev_vol = prev_snapshot.get(key, 0)
                burst = vol - prev_vol
                if burst < 0:
                    burst = 0  # volume doesn't decrease intraday

            is_burst = burst >= VOLUME_BURST_THRESHOLD

            # Directional inference
            if p["side"] == "call":
                if p["direction_approx"] == "buyer_initiated":
                    directional = "BULLISH (call buying)"
                elif p["direction_approx"] == "seller_initiated":
                    directional = "BEARISH (call selling/writing)"
                else:
                    directional = "BULLISH lean (call volume)"
            else:  # put
                if p["direction_approx"] == "buyer_initiated":
                    directional = "BEARISH (put buying)"
                elif p["direction_approx"] == "seller_initiated":
                    directional = "BULLISH (put selling/writing)"
                else:
                    directional = "BEARISH lean (put volume)"

            # New strike detection (OI=0 but volume > minimum)
            is_new_strike = oi == 0 and vol >= min_vol

            # Cooldown check
            cooldown_key = f"{ticker}:{p['strike']}:{p['side']}:{flow_level}"
            cooldown_secs = {
                "notable": ALERT_COOLDOWN_NOTABLE,
                "significant": ALERT_COOLDOWN_SIGNIFICANT,
                "extreme": ALERT_COOLDOWN_EXTREME,
            }.get(flow_level, 1800)

            should_alert = True
            if cooldown_secs > 0:
                should_alert = self._state.check_and_set_cooldown(
                    cooldown_key, cooldown_secs
                )

            alert = {
                "ticker": ticker,
                "expiry": expiry,
                "strike": p["strike"],
                "side": p["side"],
                "volume": vol,
                "oi": oi,
                "vol_oi_ratio": vol_oi,
                "flow_level": flow_level,
                "direction_approx": p["direction_approx"],
                "directional_bias": directional,
                "book_imbalance": p["book_imbalance"],
                "dist_from_spot_pct": p["dist_from_spot_pct"],
                "spot": spot,
                "burst": burst,
                "is_burst": is_burst,
                "is_new_strike": is_new_strike,
                "should_alert": should_alert,
                "timestamp": datetime.now().isoformat(),
            }

            alerts.append(alert)

            # Save volume flag for tomorrow's OI confirmation
            if flow_level in ("significant", "extreme"):
                self._state.append_volume_flag(today_str, {
                    "ticker": ticker,
                    "expiry": expiry,
                    "strike": p["strike"],
                    "side": p["side"],
                    "volume": vol,
                    "oi": oi,
                    "vol_oi_ratio": vol_oi,
                    "flow_level": flow_level,
                    "directional_bias": directional,
                    "spot": spot,
                    "date": today_str,
                })

        # Sort by vol_oi_ratio descending
        alerts.sort(key=lambda a: a["vol_oi_ratio"], reverse=True)

        # Cap at top 5 per ticker per check
        return alerts[:5]

    # ─────────────────────────────────────────────────────
    # PHASE 2: MORNING OI CONFIRMATION
    # ─────────────────────────────────────────────────────

    def run_morning_confirmation(self, chain_fn: Callable,
                                 spot_fn: Callable,
                                 expirations_fn: Callable) -> List[dict]:
        """
        Run at 8:15 AM CT. Compares today's settled OI against yesterday's
        baseline at every strike that had a volume flag yesterday.

        Returns list of confirmed flow dicts.
        """
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        # Handle Monday — check Friday
        if date.today().weekday() == 0:
            yesterday = (date.today() - timedelta(days=3)).isoformat()

        flags = self._state.get_volume_flags(yesterday)
        if not flags:
            log.info("OI confirmation: no volume flags from yesterday")
            return []

        log.info(f"OI confirmation: checking {len(flags)} volume flags from {yesterday}")

        confirmed = []
        tickers_checked = set()

        for flag in flags:
            ticker = flag.get("ticker", "")
            expiry = flag.get("expiry", "")
            strike = flag.get("strike", 0)
            side = flag.get("side", "")

            if not ticker or not expiry:
                continue

            # Get yesterday's OI baseline
            yesterday_oi_data = self._state.get_yesterday_oi_baseline(ticker, expiry)
            if not yesterday_oi_data:
                continue

            # Get today's fresh OI (need chain pull)
            # Only fetch chain once per ticker/expiry
            cache_key = f"{ticker}:{expiry}"
            if cache_key not in tickers_checked:
                tickers_checked.add(cache_key)
                try:
                    spot = spot_fn(ticker) if spot_fn else 0
                    chain = chain_fn(ticker, expiry) if chain_fn else None
                    if chain and isinstance(chain, dict) and chain.get("s") == "ok":
                        # Save today's OI as baseline
                        today_oi = self._parse_oi_from_chain(chain)
                        self._state.save_oi_baseline(ticker, expiry, today_oi)
                except Exception as e:
                    log.debug(f"OI confirmation chain fetch failed for {ticker}: {e}")
                    continue

            # Compare
            today_baseline = self._state.get_oi_baseline(ticker, expiry)
            if not today_baseline:
                continue

            key = f"{float(strike)}|{side}"
            yesterday_oi = yesterday_oi_data.get(key, 0)
            today_oi = today_baseline.get(key, 0)
            oi_change = today_oi - yesterday_oi

            if yesterday_oi == 0 and today_oi == 0:
                continue

            # Classify
            if yesterday_oi > 0:
                oi_change_pct = oi_change / yesterday_oi
            else:
                oi_change_pct = 1.0 if oi_change > 0 else 0

            if oi_change > 100:  # meaningful increase
                flow_type = "confirmed_buildup"
            elif oi_change < -100:  # meaningful decrease
                flow_type = "confirmed_unwinding"
            else:
                flow_type = "churn"

            # Price context
            yesterday_spot = flag.get("spot", 0)
            try:
                today_spot = spot_fn(ticker) if spot_fn else 0
            except Exception:
                today_spot = 0

            price_change = 0
            if yesterday_spot > 0 and today_spot > 0:
                price_change = (today_spot - yesterday_spot) / yesterday_spot * 100

            # Divergence detection
            divergence = False
            if flow_type == "confirmed_buildup":
                if side == "call" and price_change < -0.5:
                    divergence = True  # calls added, stock down = accumulation
                elif side == "put" and price_change > 0.5:
                    divergence = True  # puts added, stock up = hedging into strength

            confirmation = {
                "ticker": ticker,
                "expiry": expiry,
                "strike": strike,
                "side": side,
                "flow_type": flow_type,
                "yesterday_oi": yesterday_oi,
                "today_oi": today_oi,
                "oi_change": oi_change,
                "oi_change_pct": round(oi_change_pct * 100, 1),
                "yesterday_volume": flag.get("volume", 0),
                "yesterday_vol_oi_ratio": flag.get("vol_oi_ratio", 0),
                "yesterday_flow_level": flag.get("flow_level", ""),
                "yesterday_directional": flag.get("directional_bias", ""),
                "yesterday_spot": yesterday_spot,
                "today_spot": today_spot,
                "price_change_pct": round(price_change, 2),
                "divergence": divergence,
                "date": date.today().isoformat(),
            }

            confirmed.append(confirmation)

            # Update campaign
            if flow_type != "churn":
                day_entry = {
                    "date": date.today().isoformat(),
                    "volume": flag.get("volume", 0),
                    "oi_change": oi_change,
                    "vol_oi_ratio": flag.get("vol_oi_ratio", 0),
                    "spot": today_spot or yesterday_spot,
                }
                campaign = self._state.update_flow_campaign(
                    ticker, strike, side, expiry, day_entry, flow_type
                )
                confirmation["campaign"] = campaign

        log.info(f"OI confirmation complete: {len(confirmed)} confirmed "
                 f"({sum(1 for c in confirmed if c['flow_type'] == 'confirmed_buildup')} buildup, "
                 f"{sum(1 for c in confirmed if c['flow_type'] == 'confirmed_unwinding')} unwinding, "
                 f"{sum(1 for c in confirmed if c['flow_type'] == 'churn')} churn)")

        return confirmed

    def _parse_oi_from_chain(self, chain_data: dict) -> Dict[str, int]:
        """Extract {strike|side: oi} from chain data."""
        sym_list = chain_data.get("optionSymbol") or []
        n = len(sym_list)
        if n == 0:
            return {}

        def col(name, default=None):
            v = chain_data.get(name, default)
            return v if isinstance(v, list) else [default] * n

        strikes = col("strike", None)
        sides = col("side", "")
        ois = col("openInterest", 0)

        result = {}
        for i in range(n):
            strike = strikes[i]
            side = str(sides[i] or "").lower()
            oi = int(ois[i] or 0)
            if strike is None or side not in ("call", "put"):
                continue
            result[f"{float(strike)}|{side}"] = oi
        return result

    # ─────────────────────────────────────────────────────
    # STALK ALERT GENERATION
    # ─────────────────────────────────────────────────────

    def generate_stalk_alerts(self, confirmations: List[dict],
                              support_fn: Callable = None) -> List[dict]:
        """
        Generate stalk alerts from confirmed flow.
        Three types: DO NOT CHASE / WATCH FOR TRIGGER / ROOM LEFT

        support_fn: function(ticker) → list of support/resistance dicts
        """
        stalks = []

        for conf in confirmations:
            if conf["flow_type"] == "churn":
                continue

            ticker = conf["ticker"]
            price_change = conf.get("price_change_pct", 0)
            strike = conf["strike"]
            side = conf["side"]
            today_spot = conf.get("today_spot", 0)

            # Determine stalk type
            if conf["flow_type"] == "confirmed_buildup":
                if side == "call":
                    expected_direction = "BULLISH"
                else:
                    expected_direction = "BEARISH"
            else:  # confirmed_unwinding
                if side == "put":
                    expected_direction = "BULLISH (put unwinding)"
                else:
                    expected_direction = "BEARISH (call unwinding)"

            # Classify by price movement
            if expected_direction.startswith("BULLISH"):
                if price_change > 2.0:
                    stalk_type = "do_not_chase"
                elif price_change < 0.5:
                    stalk_type = "watch_for_trigger"
                else:
                    stalk_type = "room_left"
            else:  # bearish
                if price_change < -2.0:
                    stalk_type = "do_not_chase"
                elif price_change > -0.5:
                    stalk_type = "watch_for_trigger"
                else:
                    stalk_type = "room_left"

            # Get support/resistance levels if available
            levels = {}
            if support_fn:
                try:
                    levels = support_fn(ticker) or {}
                except Exception:
                    pass

            # Campaign context
            campaign = conf.get("campaign", {})
            consecutive = campaign.get("consecutive_days", 1)
            total_oi = campaign.get("total_oi_change", conf.get("oi_change", 0))

            stalk = {
                "ticker": ticker,
                "expiry": conf["expiry"],
                "strike": strike,
                "side": side,
                "stalk_type": stalk_type,
                "flow_type": conf["flow_type"],
                "expected_direction": expected_direction,
                "oi_change": conf["oi_change"],
                "oi_change_pct": conf["oi_change_pct"],
                "yesterday_volume": conf.get("yesterday_volume", 0),
                "price_change_pct": price_change,
                "today_spot": today_spot,
                "divergence": conf.get("divergence", False),
                "campaign_days": consecutive,
                "campaign_total_oi": total_oi,
                "support_levels": levels.get("supports", []),
                "resistance_levels": levels.get("resistances", []),
                "date": date.today().isoformat(),
            }

            stalks.append(stalk)
            self._state.save_stalk_alert(ticker, stalk)

        return stalks

    # ─────────────────────────────────────────────────────
    # SECTOR FLOW AGGREGATION
    # ─────────────────────────────────────────────────────

    def detect_sector_flow(self, alerts: List[dict]) -> List[dict]:
        """
        Detect when 2+ tickers in the same sector show same-direction flow.
        Returns list of sector flow signals.
        """
        sector_flow = {}

        for alert in alerts:
            if alert.get("flow_level") not in ("significant", "extreme"):
                continue

            ticker = alert["ticker"]
            sector = _get_sector(ticker)
            bias = alert.get("directional_bias", "")

            # Simplify to bull/bear
            direction = "bullish" if "BULLISH" in bias.upper() else "bearish"

            key = f"{sector}:{direction}"
            if key not in sector_flow:
                sector_flow[key] = {
                    "sector": sector,
                    "direction": direction,
                    "tickers": [],
                    "total_volume": 0,
                }
            sf = sector_flow[key]
            if ticker not in [t["ticker"] for t in sf["tickers"]]:
                sf["tickers"].append({
                    "ticker": ticker,
                    "strike": alert["strike"],
                    "side": alert["side"],
                    "volume": alert["volume"],
                    "vol_oi_ratio": alert["vol_oi_ratio"],
                })
                sf["total_volume"] += alert["volume"]

        # Only return sectors with 2+ names
        return [sf for sf in sector_flow.values() if len(sf["tickers"]) >= 2]

    # ─────────────────────────────────────────────────────
    # ROLL DETECTION
    # ─────────────────────────────────────────────────────

    def detect_rolls(self, confirmations: List[dict]) -> List[dict]:
        """
        Detect institutional rolls: OI decrease at one strike + OI increase
        at nearby strike, same side, same expiry.
        """
        rolls = []
        by_ticker_side_exp = {}

        for conf in confirmations:
            if conf["flow_type"] == "churn":
                continue
            key = f"{conf['ticker']}:{conf['side']}:{conf['expiry']}"
            if key not in by_ticker_side_exp:
                by_ticker_side_exp[key] = []
            by_ticker_side_exp[key].append(conf)

        for key, confs in by_ticker_side_exp.items():
            buildups = [c for c in confs if c["flow_type"] == "confirmed_buildup"]
            unwinds = [c for c in confs if c["flow_type"] == "confirmed_unwinding"]

            for unwind in unwinds:
                for buildup in buildups:
                    # Check if they're close in size (within 30%)
                    unwound = abs(unwind["oi_change"])
                    built = abs(buildup["oi_change"])
                    if unwound == 0:
                        continue
                    size_ratio = built / unwound
                    if 0.7 <= size_ratio <= 1.3:
                        direction = "UP" if buildup["strike"] > unwind["strike"] else "DOWN"
                        rolls.append({
                            "ticker": unwind["ticker"],
                            "side": unwind["side"],
                            "expiry": unwind["expiry"],
                            "from_strike": unwind["strike"],
                            "to_strike": buildup["strike"],
                            "contracts": int((unwound + built) / 2),
                            "direction": direction,
                            "signal": f"Still {'bullish' if unwind['side'] == 'call' else 'bearish'}, "
                                      f"{'raising' if direction == 'UP' else 'lowering'} target",
                        })

        return rolls

    # ─────────────────────────────────────────────────────
    # EXPIRY CLUSTERING
    # ─────────────────────────────────────────────────────

    def detect_expiry_clustering(self, alerts: List[dict],
                                  economic_events: List[dict] = None) -> List[dict]:
        """
        Detect when multiple tickers have flow concentrated on the same expiry.
        Cross-reference against known economic events.
        """
        expiry_flow = {}

        for alert in alerts:
            if alert.get("flow_level") not in ("significant", "extreme"):
                continue
            exp = alert.get("expiry", "")
            if not exp:
                continue
            if exp not in expiry_flow:
                expiry_flow[exp] = {"expiry": exp, "tickers": set(), "total_volume": 0}
            expiry_flow[exp]["tickers"].add(alert["ticker"])
            expiry_flow[exp]["total_volume"] += alert["volume"]

        clusters = []
        for exp, data in expiry_flow.items():
            if len(data["tickers"]) >= 3:  # 3+ tickers on same expiry
                cluster = {
                    "expiry": exp,
                    "ticker_count": len(data["tickers"]),
                    "tickers": sorted(data["tickers"]),
                    "total_volume": data["total_volume"],
                    "nearby_events": [],
                }
                # Cross-reference economic calendar
                if economic_events:
                    try:
                        exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
                        for evt in economic_events:
                            evt_date = datetime.strptime(
                                evt.get("date", "")[:10], "%Y-%m-%d"
                            ).date()
                            if abs((evt_date - exp_date).days) <= 2:
                                cluster["nearby_events"].append(evt.get("event", ""))
                    except (ValueError, TypeError):
                        pass

                clusters.append(cluster)

        return sorted(clusters, key=lambda c: c["ticker_count"], reverse=True)

    # ─────────────────────────────────────────────────────
    # SCORING HELPERS
    # ─────────────────────────────────────────────────────

    def get_flow_score_for_income(self, ticker: str, short_strike: float,
                                   trade_type: str,
                                   expiry: str = None) -> dict:
        """
        Get flow-based scoring adjustment for an income trade.

        Returns:
          {score_adj, reason, flow_level, recommended_expiry, expiry_note}
        """
        ticker = ticker.upper()
        result = {"score_adj": 0, "reasons": [], "flow_level": "none",
                  "recommended_expiry": None, "expiry_note": ""}

        # Check active campaigns
        campaigns = self._state.get_all_flow_campaigns(ticker)
        if not campaigns:
            return result

        # Find most relevant campaign
        best_campaign = None
        best_relevance = 0

        for c in campaigns:
            # Relevance = proximity to short strike + recency + size
            strike_dist = abs(c.get("strike", 0) - short_strike)
            if c.get("spot", short_strike) > 0:
                strike_dist_pct = strike_dist / c["spot"]
            else:
                strike_dist_pct = 1.0

            if strike_dist_pct > 0.10:  # too far from our trade
                continue

            consecutive = c.get("consecutive_days", 0)
            relevance = consecutive * 10 + abs(c.get("total_oi_change", 0)) / 1000

            if relevance > best_relevance:
                best_relevance = relevance
                best_campaign = c

        if not best_campaign:
            return result

        flow_type = best_campaign.get("flow_type", "")
        consecutive = best_campaign.get("consecutive_days", 1)
        camp_side = best_campaign.get("side", "")

        # Determine alignment
        if trade_type == "bull_put":
            # Bull put wants bullish flow
            if camp_side == "call" and flow_type == "confirmed_buildup":
                aligned = True
            elif camp_side == "put" and flow_type == "confirmed_unwinding":
                aligned = True
            elif camp_side == "put" and flow_type == "confirmed_buildup":
                aligned = False  # put buildup opposes bull put
            elif camp_side == "call" and flow_type == "confirmed_unwinding":
                aligned = False  # call unwinding opposes bull put
            else:
                aligned = None
        else:  # bear_call
            if camp_side == "put" and flow_type == "confirmed_buildup":
                aligned = True
            elif camp_side == "call" and flow_type == "confirmed_unwinding":
                aligned = True
            elif camp_side == "call" and flow_type == "confirmed_buildup":
                aligned = False
            elif camp_side == "put" and flow_type == "confirmed_unwinding":
                aligned = False
            else:
                aligned = None

        if aligned is None:
            return result

        if aligned:
            base_score = SCORE_CONFIRMED_ALIGNED
            campaign_bonus = min(consecutive * SCORE_CAMPAIGN_BONUS,
                                 SCORE_CAMPAIGN_BONUS * 3)
            result["score_adj"] = base_score + campaign_bonus
            result["flow_level"] = "confirmed_aligned"
            result["reasons"].append(
                f"✅ Confirmed {flow_type.replace('confirmed_', '')} "
                f"({consecutive}D campaign, {best_campaign.get('total_oi_change', 0):+,} OI) "
                f"— institutions aligned with your trade"
            )
        else:
            base_score = SCORE_CONFIRMED_OPPOSING
            result["score_adj"] = base_score
            result["flow_level"] = "confirmed_opposing"
            result["reasons"].append(
                f"⚠️ Confirmed {flow_type.replace('confirmed_', '')} "
                f"({consecutive}D, {best_campaign.get('total_oi_change', 0):+,} OI) "
                f"— institutions OPPOSING your trade"
            )

        # Expiry recommendation
        if best_campaign.get("expiry"):
            result["recommended_expiry"] = best_campaign["expiry"]
            result["expiry_note"] = (
                f"Institutional flow concentrated at {best_campaign['expiry']} expiry "
                f"— strong edge for income trades aligned with this positioning"
            )

        return result

    def get_flow_score_for_swing(self, ticker: str, fib_price: float,
                                  direction: str) -> dict:
        """
        Get flow-based scoring adjustment for a swing signal.

        Returns: {score_adj, reasons}
        """
        ticker = ticker.upper()
        result = {"score_adj": 0, "reasons": []}

        campaigns = self._state.get_all_flow_campaigns(ticker)
        if not campaigns:
            return result

        for c in campaigns:
            # Check if campaign strike is near the fib level
            if fib_price > 0:
                dist = abs(c.get("strike", 0) - fib_price) / fib_price
                if dist > 0.03:  # more than 3% away
                    continue

            camp_side = c.get("side", "")
            flow_type = c.get("flow_type", "")
            consecutive = c.get("consecutive_days", 1)

            # Alignment check
            if direction == "bull":
                aligned = (camp_side == "call" and "buildup" in flow_type) or \
                          (camp_side == "put" and "unwinding" in flow_type)
            else:
                aligned = (camp_side == "put" and "buildup" in flow_type) or \
                          (camp_side == "call" and "unwinding" in flow_type)

            if aligned:
                bonus = min(8 + consecutive * 2, 15)
                result["score_adj"] += bonus
                result["reasons"].append(
                    f"🏛️ Institutional {flow_type.replace('confirmed_', '')} "
                    f"at ${c['strike']:.0f} {camp_side} ({consecutive}D campaign) "
                    f"— confirms fib level (+{bonus})"
                )
            else:
                penalty = -5
                result["score_adj"] += penalty
                result["reasons"].append(
                    f"⚠️ Institutional {flow_type.replace('confirmed_', '')} "
                    f"at ${c['strike']:.0f} {camp_side} — headwind ({penalty})"
                )
            break  # Use most relevant campaign only

        return result

    def get_validator_boost(self, ticker: str, direction: str,
                            spot: float) -> float:
        """
        Get flow-based boost for EntryValidator scoring.
        Returns 0 to 1.5 — enough to lift 2→3 but not 1→3.
        """
        ticker = ticker.upper()
        campaigns = self._state.get_all_flow_campaigns(ticker)
        if not campaigns:
            return 0.0

        best_boost = 0.0
        for c in campaigns:
            # Check proximity to spot
            strike = c.get("strike", 0)
            if spot > 0 and abs(strike - spot) / spot > 0.05:
                continue

            camp_side = c.get("side", "")
            flow_type = c.get("flow_type", "")
            consecutive = c.get("consecutive_days", 1)

            if direction == "bull":
                aligned = (camp_side == "call" and "buildup" in flow_type) or \
                          (camp_side == "put" and "unwinding" in flow_type)
            else:
                aligned = (camp_side == "put" and "buildup" in flow_type) or \
                          (camp_side == "call" and "unwinding" in flow_type)

            if aligned:
                if consecutive >= CAMPAIGN_STRONG_DAYS:
                    boost = VALIDATOR_BOOST_CONFIRMED
                elif consecutive >= CAMPAIGN_MIN_DAYS:
                    boost = VALIDATOR_BOOST_EXTREME
                else:
                    boost = VALIDATOR_BOOST_SIGNIFICANT
                best_boost = max(best_boost, boost)

        return best_boost

    # ─────────────────────────────────────────────────────
    # TRADE GENERATION (Extreme flow)
    # ─────────────────────────────────────────────────────

    def generate_flow_trade_ideas(self, alerts: List[dict]) -> List[dict]:
        """
        For Extreme tier flow, generate income trade ideas.
        Always generated regardless of score — user always sees the idea.
        """
        ideas = []
        tier = None

        for alert in alerts:
            if alert.get("flow_level") != "extreme":
                continue

            ticker = alert["ticker"]
            tier_cfg = _get_volume_tier(ticker)
            min_vol = tier_cfg["min_volume"]

            # Extra gate for trade generation
            if alert["volume"] < min_vol * TRADE_GEN_MIN_VOLUME_MULTIPLIER:
                continue

            side = alert["side"]
            strike = alert["strike"]
            spot = alert["spot"]
            expiry = alert.get("expiry", "")
            directional = alert.get("directional_bias", "")

            # Generate income trade aligned with flow
            if "BULLISH" in directional.upper():
                trade_type = "bull_put"
                # Short put below the flow strike
                suggested_short = round(strike * 0.97, 0)  # ~3% below
                suggested_long = suggested_short - 2
            else:
                trade_type = "bear_call"
                suggested_short = round(strike * 1.03, 0)  # ~3% above
                suggested_long = suggested_short + 2

            ideas.append({
                "ticker": ticker,
                "trade_type": trade_type,
                "suggested_short_strike": suggested_short,
                "suggested_long_strike": suggested_long,
                "recommended_expiry": expiry,
                "flow_trigger": {
                    "strike": strike,
                    "side": side,
                    "volume": alert["volume"],
                    "oi": alert["oi"],
                    "vol_oi_ratio": alert["vol_oi_ratio"],
                    "directional_bias": directional,
                },
                "note": (
                    f"Flow-generated idea: {alert['volume']:,} {side}s "
                    f"at ${strike:.0f} ({alert['vol_oi_ratio']:.1f}x vol/OI). "
                    f"This is an institutional thesis, not a technical setup."
                ),
            })

        return ideas

    # ─────────────────────────────────────────────────────
    # FORMATTING
    # ─────────────────────────────────────────────────────

    def format_intraday_alert(self, alert: dict) -> str:
        """Format a single intraday flow alert for Telegram."""
        level = alert["flow_level"]
        if level == "extreme":
            emoji = "🚨"
            label = "EXTREME FLOW"
        elif level == "significant":
            emoji = "🔥"
            label = "SIGNIFICANT FLOW"
        else:
            emoji = "📊"
            label = "NOTABLE FLOW"

        side_emoji = "📗" if alert["side"] == "call" else "📕"
        dist_dir = "above" if alert["dist_from_spot_pct"] > 0 else "below"

        burst_tag = " ⚡ BURST" if alert.get("is_burst") else ""
        new_tag = " 🆕 NEW STRIKE" if alert.get("is_new_strike") else ""

        lines = [
            f"{emoji} {label} — {alert['ticker']}{burst_tag}{new_tag}",
            "━" * 28,
            f"{side_emoji} ${alert['strike']:.0f} {alert['side'].upper()} "
            f"({abs(alert['dist_from_spot_pct']):.1f}% {dist_dir} spot ${alert['spot']:.2f})",
            f"Volume: {alert['volume']:,} | OI: {alert['oi']:,} "
            f"({alert['vol_oi_ratio']:.1f}x turnover)",
            f"Exp: {alert.get('expiry', 'N/A')}",
            f"Direction: {alert['directional_bias']}",
            f"Book: {alert['book_imbalance'].replace('_', ' ')}",
        ]

        if alert.get("is_burst"):
            lines.append(f"Burst: +{alert['burst']:,} contracts in last interval")

        return "\n".join(lines)

    def format_confirmation_summary(self, confirmations: List[dict],
                                     rolls: List[dict] = None,
                                     sector_flow: List[dict] = None) -> str:
        """Format morning OI confirmation summary for Telegram."""
        if not confirmations:
            return ""

        buildups = [c for c in confirmations if c["flow_type"] == "confirmed_buildup"]
        unwinds = [c for c in confirmations if c["flow_type"] == "confirmed_unwinding"]

        lines = [
            "🏛️ MORNING OI CONFIRMATION",
            "━" * 28,
            f"Checked {len(confirmations)} volume flags from yesterday",
        ]

        if buildups:
            lines.append("")
            lines.append("✅ CONFIRMED BUILDUP (new positions opened):")
            for c in buildups[:5]:
                side_emoji = "📗" if c["side"] == "call" else "📕"
                div_tag = " 🔀 DIVERGENCE" if c.get("divergence") else ""
                campaign = c.get("campaign", {})
                camp_tag = ""
                if campaign.get("consecutive_days", 0) >= CAMPAIGN_STRONG_DAYS:
                    camp_tag = f" 🏗️ {campaign['consecutive_days']}D CAMPAIGN"
                elif campaign.get("consecutive_days", 0) >= CAMPAIGN_MIN_DAYS:
                    camp_tag = f" 📅 Day {campaign['consecutive_days']}"
                lines.append(
                    f"  {side_emoji} {c['ticker']} ${c['strike']:.0f} {c['side'].upper()} "
                    f"({c['expiry']}) — OI {c['oi_change']:+,} "
                    f"({c['oi_change_pct']:+.0f}%){camp_tag}{div_tag}"
                )

        if unwinds:
            lines.append("")
            lines.append("🔻 CONFIRMED UNWINDING (positions closed):")
            for c in unwinds[:5]:
                side_emoji = "📗" if c["side"] == "call" else "📕"
                lines.append(
                    f"  {side_emoji} {c['ticker']} ${c['strike']:.0f} {c['side'].upper()} "
                    f"({c['expiry']}) — OI {c['oi_change']:+,} "
                    f"({c['oi_change_pct']:+.0f}%)"
                )

        if rolls:
            lines.append("")
            lines.append("🔄 ROLLS DETECTED:")
            for r in rolls[:3]:
                lines.append(
                    f"  {r['ticker']} {r['side'].upper()} "
                    f"${r['from_strike']:.0f} → ${r['to_strike']:.0f} "
                    f"(~{r['contracts']:,} contracts {r['direction']})"
                )
                lines.append(f"    → {r['signal']}")

        if sector_flow:
            lines.append("")
            lines.append("🏭 SECTOR FLOW:")
            for sf in sector_flow[:3]:
                tickers = ", ".join(t["ticker"] for t in sf["tickers"])
                lines.append(
                    f"  {sf['sector']}: {sf['direction'].upper()} "
                    f"across {len(sf['tickers'])} names ({tickers})"
                )

        return "\n".join(lines)

    def format_stalk_alert(self, stalk: dict) -> str:
        """Format a stalk alert for Telegram."""
        t = stalk
        ticker = t["ticker"]

        if t["stalk_type"] == "do_not_chase":
            header = f"👁️ STALK ALERT — {ticker} (DO NOT CHASE)"
            action = "⛔ Do NOT chase. Wait for pullback."
        elif t["stalk_type"] == "watch_for_trigger":
            header = f"👁️ STALK ALERT — {ticker} (WATCH FOR TRIGGER)"
            action = "🎯 Watch for trigger. Institutions positioned but move hasn't fired."
        else:
            header = f"👁️ STALK ALERT — {ticker} (ROOM LEFT)"
            action = f"📍 Room remaining. Partial move ({t['price_change_pct']:+.1f}%)."

        side_emoji = "📗" if t["side"] == "call" else "📕"
        div_tag = "\n🔀 DIVERGENCE — institutions buying into weakness = accumulation" if t.get("divergence") else ""

        campaign_tag = ""
        if t.get("campaign_days", 0) >= CAMPAIGN_STRONG_DAYS:
            campaign_tag = f"\n🏗️ INSTITUTIONAL CAMPAIGN — {t['campaign_days']} consecutive days, {t['campaign_total_oi']:+,} total OI"
        elif t.get("campaign_days", 0) >= CAMPAIGN_MIN_DAYS:
            campaign_tag = f"\n📅 Persistent flow — Day {t['campaign_days']}"

        lines = [
            header,
            "━" * 28,
            f"✅ Confirmed {t['flow_type'].replace('confirmed_', '')} yesterday: "
            f"OI {t['oi_change']:+,} ({t['oi_change_pct']:+.0f}%) "
            f"at ${t['strike']:.0f} {t['side'].upper()} ({t['expiry']})",
            f"Price since: {t['price_change_pct']:+.1f}%"
            + (f" (spot ${t['today_spot']:.2f})" if t.get("today_spot") else ""),
            "",
            action,
            f"{side_emoji} Direction: {t['expected_direction']}",
        ]

        if div_tag:
            lines.append(div_tag)
        if campaign_tag:
            lines.append(campaign_tag)

        # Add support/resistance levels
        supports = t.get("support_levels", [])
        resistances = t.get("resistance_levels", [])
        if supports:
            lines.append("")
            for s in supports[:2]:
                if isinstance(s, dict):
                    lines.append(f"📍 Support: ${s.get('price', 0):.2f} ({s.get('label', '')})")
                else:
                    lines.append(f"📍 Support: ${s:.2f}")
        if resistances:
            for r in resistances[:2]:
                if isinstance(r, dict):
                    lines.append(f"📍 Resistance: ${r.get('price', 0):.2f} ({r.get('label', '')})")
                else:
                    lines.append(f"📍 Resistance: ${r:.2f}")

        return "\n".join(lines)

    def format_flow_trade_idea(self, idea: dict) -> str:
        """Format a flow-generated trade idea for Telegram."""
        trigger = idea["flow_trigger"]
        trade = "Bull Put Spread" if idea["trade_type"] == "bull_put" else "Bear Call Spread"
        dir_emoji = "🟢" if idea["trade_type"] == "bull_put" else "🔴"

        lines = [
            f"🚨 FLOW-GENERATED INCOME IDEA — {idea['ticker']}",
            "━" * 28,
            f"Trigger: {trigger['volume']:,} {trigger['side']}s at "
            f"${trigger['strike']:.0f} ({trigger['vol_oi_ratio']:.1f}x vol/OI)",
            f"Direction: {trigger['directional_bias']}",
            "",
            f"{dir_emoji} Suggested: {trade} "
            f"${idea['suggested_short_strike']:.0f}/${idea['suggested_long_strike']:.0f}",
            f"🧭 Recommended expiry: {idea['recommended_expiry']} "
            f"(where flow concentrated — strong edge)",
            "",
            "⚠️ This is an institutional thesis, not a technical setup.",
            "Run /score to see the full structural scorecard.",
        ]
        return "\n".join(lines)

    def format_sector_flow_alert(self, sector: dict) -> str:
        """Format a sector flow alert."""
        tickers = ", ".join(t["ticker"] for t in sector["tickers"])
        total_vol = sector["total_volume"]
        return (
            f"🏭 SECTOR FLOW: {sector['sector']} — {sector['direction'].upper()}\n"
            f"Tickers: {tickers} ({len(sector['tickers'])} names)\n"
            f"Combined volume: {total_vol:,}\n"
            f"Signal: Institutional sector-wide positioning"
        )

    def format_expiry_cluster_alert(self, cluster: dict) -> str:
        """Format an expiry clustering alert."""
        tickers = ", ".join(cluster["tickers"][:10])
        events = ", ".join(cluster.get("nearby_events", [])[:3])
        event_line = f"\n📅 Nearby event: {events}" if events else ""
        return (
            f"📅 EXPIRY CLUSTERING: {cluster['expiry']}\n"
            f"Heavy flow across {cluster['ticker_count']} tickers: {tickers}\n"
            f"Combined volume: {cluster['total_volume']:,}"
            f"{event_line}\n"
            f"Signal: Institutions positioning through this date"
        )
