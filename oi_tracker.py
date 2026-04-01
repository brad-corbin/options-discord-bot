# oi_tracker.py
# ═══════════════════════════════════════════════════════════════════
# Daily OI Change Tracker — Institutional Flow Detection (Free)
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# v5.1: Enriched with spot-relative context, wall detection, zones,
#        and daily sweep for broader coverage.
# ═══════════════════════════════════════════════════════════════════

import json
import logging
import time
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Callable

log = logging.getLogger(__name__)

OI_DAILY_TTL         = 7 * 86400
OI_MOVER_THRESHOLD   = 0.15
OI_SPIKE_THRESHOLD   = 0.30
OI_MIN_TOTAL         = 5000
OI_TOP_STRIKES       = 10
OI_SUMMARY_HOUR_CT   = 9
OI_SUMMARY_MINUTE_CT = 0

OI_SWEEP_TICKERS = [
    "SPY", "QQQ", "IWM", "DIA",
    "AAPL", "MSFT", "NVDA", "AMD", "AMZN", "META", "TSLA", "GOOGL",
    "NFLX", "COIN", "AVGO", "PLTR", "CRM", "ORCL", "ARM", "SMCI",
    "MSTR", "SOFI",
    "XLF", "XLE", "XLV", "SOXX", "GLD", "TLT",
    "JPM", "GS", "BA", "CAT", "LLY", "UNH",
]
OI_SWEEP_HOUR_CT   = 16
OI_SWEEP_MINUTE_CT = 10


class OITracker:
    def __init__(self, store_get_fn: Callable, store_set_fn: Callable):
        self._get = store_get_fn
        self._set = store_set_fn
        self._today: Dict[str, dict] = {}
        self._today_date: str = ""
        self._last_summary_date: str = ""
        self._last_sweep_date: str = ""

    def _daily_key(self, ticker: str, date_str: str) -> str:
        return f"oi_daily:{ticker.upper()}:{date_str}"

    def _ensure_today(self):
        today = date.today().isoformat()
        if today != self._today_date:
            if self._today_date and self._today:
                self._flush_to_store(self._today_date)
            self._today = {}
            self._today_date = today

    def _yesterday_str(self) -> str:
        yesterday = date.today() - timedelta(days=1)
        if yesterday.weekday() == 6:
            yesterday -= timedelta(days=2)
        elif yesterday.weekday() == 5:
            yesterday -= timedelta(days=1)
        return yesterday.isoformat()

    def _classify_strike(self, strike: float, side: str, spot: float) -> str:
        if spot <= 0:
            return "?"
        dist_pct = abs(strike - spot) / spot * 100
        if dist_pct <= 0.5:
            return "ATM"
        if side == "call":
            return "OTM" if strike > spot else "ITM"
        else:
            return "OTM" if strike < spot else "ITM"

    def _find_wall(self, strikes: dict, side: str) -> Optional[dict]:
        best = None
        for k, v in strikes.items():
            parts = k.split("|")
            if len(parts) != 2 or parts[1] != side:
                continue
            if best is None or v > best["oi"]:
                best = {"strike": float(parts[0]), "oi": v}
        return best

    def _find_concentration_zones(self, strikes: dict, side: str,
                                   spot: float, zone_pct: float = 1.5) -> list:
        filtered = []
        for k, v in strikes.items():
            parts = k.split("|")
            if len(parts) != 2 or parts[1] != side:
                continue
            filtered.append((float(parts[0]), v))
        if not filtered:
            return []
        filtered.sort(key=lambda x: x[0])
        zones = []
        cz = {"low": filtered[0][0], "high": filtered[0][0],
              "total_oi": filtered[0][1], "count": 1}
        for strike, oi in filtered[1:]:
            if cz["high"] > 0 and (strike - cz["high"]) / cz["high"] * 100 <= zone_pct:
                cz["high"] = strike
                cz["total_oi"] += oi
                cz["count"] += 1
            else:
                zones.append(cz)
                cz = {"low": strike, "high": strike, "total_oi": oi, "count": 1}
        zones.append(cz)
        for z in zones:
            mid = (z["low"] + z["high"]) / 2
            z["mid"] = round(mid, 2)
            z["moneyness"] = self._classify_strike(mid, side, spot)
            z["dist_from_spot_pct"] = round(abs(mid - spot) / spot * 100, 1) if spot > 0 else 0
            z["direction"] = "above" if mid > spot else "below" if spot > 0 else "?"
        zones.sort(key=lambda x: x["total_oi"], reverse=True)
        return zones

    # ── Recording ──

    def record_chain(self, ticker: str, expiration: str, chain_data: dict,
                     spot: float = 0.0):
        self._ensure_today()
        if not chain_data or not chain_data.get("optionSymbol"):
            return
        ticker = ticker.upper()
        n = len(chain_data.get("optionSymbol", []))
        if n == 0:
            return

        def col(name, default=None):
            v = chain_data.get(name, default)
            return v if isinstance(v, list) else [default] * n

        strikes = col("strike", None)
        sides = col("side", "")
        oi_list = col("openInterest", 0)
        call_oi = 0
        put_oi = 0
        strike_oi: Dict[str, int] = {}
        for i in range(n):
            strike = strikes[i]
            side = str(sides[i] or "").lower()
            oi = int(oi_list[i] or 0)
            if strike is None or side not in ("call", "put") or oi <= 0:
                continue
            if side == "call":
                call_oi += oi
            else:
                put_oi += oi
            key = f"{float(strike):.2f}|{side}"
            strike_oi[key] = oi

        if ticker not in self._today:
            self._today[ticker] = {
                "call_oi": 0, "put_oi": 0,
                "strikes": {}, "expirations": set(),
                "updated_at": time.time(), "spot": spot,
            }
        entry = self._today[ticker]
        if spot > 0:
            entry["spot"] = spot
        if expiration not in entry.get("expirations", set()):
            entry["call_oi"] += call_oi
            entry["put_oi"] += put_oi
            for k, v in strike_oi.items():
                entry["strikes"][k] = entry["strikes"].get(k, 0) + v
            entry["expirations"].add(expiration)
            entry["updated_at"] = time.time()

    def _flush_to_store(self, date_str: str):
        for ticker, data in self._today.items():
            total = data["call_oi"] + data["put_oi"]
            if total < OI_MIN_TOTAL:
                continue
            sorted_strikes = sorted(data["strikes"].items(),
                                     key=lambda x: x[1], reverse=True)
            top = sorted_strikes[:OI_TOP_STRIKES * 3]
            put_wall = self._find_wall(data["strikes"], "put")
            call_wall = self._find_wall(data["strikes"], "call")
            snapshot = {
                "date": date_str,
                "total_oi": total,
                "call_oi": data["call_oi"],
                "put_oi": data["put_oi"],
                "call_pct": round(data["call_oi"] / total * 100, 1) if total > 0 else 50,
                "put_pct": round(data["put_oi"] / total * 100, 1) if total > 0 else 50,
                "top_strikes": [{"strike_key": k, "oi": v} for k, v in top],
                "put_wall": put_wall,
                "call_wall": call_wall,
                "spot": data.get("spot", 0),
                "expiration_count": len(data.get("expirations", set())),
                "updated_at": data["updated_at"],
            }
            try:
                self._set(self._daily_key(ticker, date_str),
                         json.dumps(snapshot), ttl=OI_DAILY_TTL)
            except Exception as e:
                log.debug(f"OI tracker flush failed for {ticker}: {e}")

    def flush(self):
        self._ensure_today()
        if self._today_date and self._today:
            self._flush_to_store(self._today_date)
            log.info(f"OI tracker: flushed {len(self._today)} tickers for {self._today_date}")

    # ── Comparison ──

    def _get_snapshot(self, ticker: str, date_str: str) -> Optional[dict]:
        try:
            raw = self._get(self._daily_key(ticker, date_str))
            if raw:
                return json.loads(raw)
        except Exception:
            pass
        return None

    def get_ticker_change(self, ticker: str) -> Optional[dict]:
        self._ensure_today()
        ticker = ticker.upper()
        today_data = self._today.get(ticker)
        if not today_data:
            return None
        today_total = today_data["call_oi"] + today_data["put_oi"]
        if today_total < OI_MIN_TOTAL:
            return None
        spot = today_data.get("spot", 0)
        prior = self._get_snapshot(ticker, self._yesterday_str())
        if not prior:
            return None
        prior_total = prior.get("total_oi", 0)
        if prior_total < OI_MIN_TOTAL:
            return None

        total_change = today_total - prior_total
        total_change_pct = total_change / prior_total if prior_total > 0 else 0
        call_change = today_data["call_oi"] - prior.get("call_oi", 0)
        put_change = today_data["put_oi"] - prior.get("put_oi", 0)
        prior_call = prior.get("call_oi", 1) or 1
        prior_put = prior.get("put_oi", 1) or 1
        call_change_pct = call_change / prior_call
        put_change_pct = put_change / prior_put

        # Strike-level changes with context
        today_strikes = today_data.get("strikes", {})
        prior_strikes = {s["strike_key"]: s["oi"] for s in prior.get("top_strikes", [])}
        strike_changes = []
        for key, oi in today_strikes.items():
            prev = prior_strikes.get(key, 0)
            if prev > 0:
                delta = oi - prev
                if abs(delta) > 500:
                    parts = key.split("|")
                    strike_val = float(parts[0]) if parts else 0
                    side = parts[1] if len(parts) > 1 else "?"
                    strike_changes.append({
                        "strike_key": key,
                        "strike": strike_val,
                        "side": side,
                        "current_oi": oi,
                        "prior_oi": prev,
                        "change": delta,
                        "change_pct": round(delta / prev * 100, 1),
                        "moneyness": self._classify_strike(strike_val, side, spot),
                        "dist_from_spot_pct": round(abs(strike_val - spot) / spot * 100, 1) if spot > 0 else 0,
                        "direction": "above" if strike_val > spot else "below" if spot > 0 else "?",
                    })
        strike_changes.sort(key=lambda x: abs(x["change"]), reverse=True)

        # Walls today vs yesterday
        today_put_wall = self._find_wall(today_strikes, "put")
        today_call_wall = self._find_wall(today_strikes, "call")
        prior_put_wall = prior.get("put_wall")
        prior_call_wall = prior.get("call_wall")
        put_wall_shift = None
        if today_put_wall and prior_put_wall:
            if today_put_wall["strike"] != prior_put_wall["strike"]:
                put_wall_shift = {
                    "from": prior_put_wall["strike"], "to": today_put_wall["strike"],
                    "direction": "up" if today_put_wall["strike"] > prior_put_wall["strike"] else "down",
                    "oi": today_put_wall["oi"],
                }
        call_wall_shift = None
        if today_call_wall and prior_call_wall:
            if today_call_wall["strike"] != prior_call_wall["strike"]:
                call_wall_shift = {
                    "from": prior_call_wall["strike"], "to": today_call_wall["strike"],
                    "direction": "up" if today_call_wall["strike"] > prior_call_wall["strike"] else "down",
                    "oi": today_call_wall["oi"],
                }

        # Concentration zones
        call_zones = self._find_concentration_zones(today_strikes, "call", spot)
        put_zones = self._find_concentration_zones(today_strikes, "put", spot)

        # Flow bias
        if call_change_pct > 0.10 and put_change_pct < 0.05:
            flow_bias = "BULLISH"
        elif put_change_pct > 0.10 and call_change_pct < 0.05:
            flow_bias = "BEARISH"
        elif total_change_pct > OI_MOVER_THRESHOLD:
            flow_bias = "ACCUMULATION"
        elif total_change_pct < -OI_MOVER_THRESHOLD:
            flow_bias = "UNWINDING"
        else:
            flow_bias = "NEUTRAL"

        return {
            "ticker": ticker, "spot": spot,
            "today_total": today_total, "prior_total": prior_total,
            "total_change": total_change,
            "total_change_pct": round(total_change_pct * 100, 1),
            "call_oi": today_data["call_oi"],
            "call_change": call_change,
            "call_change_pct": round(call_change_pct * 100, 1),
            "put_oi": today_data["put_oi"],
            "put_change": put_change,
            "put_change_pct": round(put_change_pct * 100, 1),
            "call_put_ratio": round(today_data["call_oi"] / today_data["put_oi"], 2) if today_data["put_oi"] > 0 else 999,
            "flow_bias": flow_bias,
            "top_strike_changes": strike_changes[:OI_TOP_STRIKES],
            "put_wall": today_put_wall, "call_wall": today_call_wall,
            "put_wall_shift": put_wall_shift, "call_wall_shift": call_wall_shift,
            "call_zones": call_zones[:3], "put_zones": put_zones[:3],
            "is_mover": abs(total_change_pct) >= OI_MOVER_THRESHOLD,
            "is_spike": abs(total_change_pct) >= OI_SPIKE_THRESHOLD,
        }

    def get_daily_movers(self) -> List[dict]:
        self._ensure_today()
        movers = []
        for ticker in self._today:
            change = self.get_ticker_change(ticker)
            if change and change.get("is_mover"):
                movers.append(change)
        movers.sort(key=lambda x: abs(x.get("total_change_pct", 0)), reverse=True)
        return movers

    # ── Summary Formatting ──

    def format_morning_summary(self) -> str:
        movers = self.get_daily_movers()
        if not movers:
            return "📊 OI Tracker: No significant OI changes detected vs prior session."

        lines = ["📊 ── OI CHANGE SUMMARY ──", ""]

        for m in movers[:8]:
            bias_emoji = {"BULLISH": "🟢", "BEARISH": "🔴",
                         "ACCUMULATION": "📈", "UNWINDING": "📉",
                         "NEUTRAL": "⚪"}.get(m["flow_bias"], "❓")
            spike_tag = " 🔥SPIKE" if m.get("is_spike") else ""
            spot_str = f" @ ${m['spot']:.2f}" if m.get("spot", 0) > 0 else ""

            lines.append(f"{bias_emoji} {m['ticker']}{spot_str}: "
                        f"{m['total_change_pct']:+.1f}% "
                        f"({m['total_change']:+,}) "
                        f"— {m['flow_bias']}{spike_tag}")
            lines.append(f"   Calls: {m['call_change_pct']:+.1f}% | "
                        f"Puts: {m['put_change_pct']:+.1f}% | "
                        f"C/P: {m['call_put_ratio']:.2f}")

            # Walls with shift detection
            pw = m.get("put_wall")
            cw = m.get("call_wall")
            pws = m.get("put_wall_shift")
            cws = m.get("call_wall_shift")
            wall_parts = []
            if pw:
                pw_dist = round(abs(pw["strike"] - m["spot"]) / m["spot"] * 100, 1) if m.get("spot", 0) > 0 else 0
                pw_str = f"Put ${pw['strike']:.0f} ({pw_dist}% below)"
                if pws:
                    pw_str += f" ← from ${pws['from']:.0f}"
                wall_parts.append(pw_str)
            if cw:
                cw_dist = round(abs(cw["strike"] - m["spot"]) / m["spot"] * 100, 1) if m.get("spot", 0) > 0 else 0
                cw_str = f"Call ${cw['strike']:.0f} ({cw_dist}% above)"
                if cws:
                    cw_str += f" ← from ${cws['from']:.0f}"
                wall_parts.append(cw_str)
            if wall_parts:
                lines.append(f"   🧱 Walls: {' | '.join(wall_parts)}")

            # Top 3 strike changes with context
            top_changes = m.get("top_strike_changes", [])[:3]
            if top_changes:
                for sc in top_changes:
                    lines.append(
                        f"   📍 ${sc['strike']:.0f} {sc['side'].upper()} "
                        f"{sc['change']:+,} ({sc['change_pct']:+.1f}%) "
                        f"— {sc['moneyness']} {sc['dist_from_spot_pct']}% {sc['direction']}"
                    )
            lines.append("")

        lines.append(f"Tracking {len(self._today)} tickers | "
                    f"{len(movers)} movers detected")
        return "\n".join(lines)

    def format_ticker_detail(self, ticker: str) -> str:
        change = self.get_ticker_change(ticker.upper())
        if not change:
            data = self._today.get(ticker.upper())
            if data:
                total = data["call_oi"] + data["put_oi"]
                return (f"📊 {ticker.upper()} OI: {total:,} total "
                       f"({data['call_oi']:,} calls / {data['put_oi']:,} puts)\n"
                       f"No prior session data for comparison yet.")
            return f"📊 {ticker.upper()}: No OI data recorded today."

        spot_str = f"${change['spot']:.2f}" if change.get("spot", 0) > 0 else "N/A"
        lines = [
            f"📊 ── {change['ticker']} OI DETAIL ──",
            f"Spot: {spot_str}", "",
            f"Total OI: {change['today_total']:,} ({change['total_change_pct']:+.1f}% vs prior)",
            f"  Calls: {change['call_oi']:,} ({change['call_change_pct']:+.1f}%)",
            f"  Puts:  {change['put_oi']:,} ({change['put_change_pct']:+.1f}%)",
            f"  C/P Ratio: {change['call_put_ratio']:.2f}",
            f"  Flow: {change['flow_bias']}",
        ]

        pw = change.get("put_wall")
        cw = change.get("call_wall")
        pws = change.get("put_wall_shift")
        cws = change.get("call_wall_shift")
        if pw or cw:
            lines.extend(["", "🧱 Walls:"])
            if pw:
                pw_dist = round(abs(pw["strike"] - change["spot"]) / change["spot"] * 100, 1) if change.get("spot", 0) > 0 else 0
                shift = f" ← shifted {pws['direction']} from ${pws['from']:.0f}" if pws else ""
                lines.append(f"  Put wall:  ${pw['strike']:.0f} ({pw['oi']:,} OI, {pw_dist}% below){shift}")
            if cw:
                cw_dist = round(abs(cw["strike"] - change["spot"]) / change["spot"] * 100, 1) if change.get("spot", 0) > 0 else 0
                shift = f" ← shifted {cws['direction']} from ${cws['from']:.0f}" if cws else ""
                lines.append(f"  Call wall: ${cw['strike']:.0f} ({cw['oi']:,} OI, {cw_dist}% above){shift}")

        call_zones = change.get("call_zones", [])
        put_zones = change.get("put_zones", [])
        if call_zones or put_zones:
            lines.extend(["", "📍 Concentration zones:"])
            for z in call_zones[:2]:
                lines.append(f"  Call: ${z['low']:.0f}-${z['high']:.0f} "
                           f"({z['total_oi']:,} OI, {z['moneyness']} {z['dist_from_spot_pct']}% {z['direction']})")
            for z in put_zones[:2]:
                lines.append(f"  Put:  ${z['low']:.0f}-${z['high']:.0f} "
                           f"({z['total_oi']:,} OI, {z['moneyness']} {z['dist_from_spot_pct']}% {z['direction']})")

        if change.get("top_strike_changes"):
            lines.extend(["", "Top strike changes:"])
            for sc in change["top_strike_changes"][:5]:
                lines.append(
                    f"  ${sc['strike']:.0f} {sc['side'].upper()}: {sc['change']:+,} "
                    f"({sc['change_pct']:+.1f}%) — {sc['moneyness']} "
                    f"{sc['dist_from_spot_pct']}% {sc['direction']} spot"
                )
        return "\n".join(lines)

    # ── Daily Sweep ──

    def run_daily_sweep(self, chain_fn: Callable, spot_fn: Callable):
        today_str = date.today().isoformat()
        if today_str == self._last_sweep_date:
            return
        log.info(f"OI sweep starting: {len(OI_SWEEP_TICKERS)} tickers")
        self._last_sweep_date = today_str
        recorded = 0
        errors = 0
        for ticker in OI_SWEEP_TICKERS:
            try:
                spot = spot_fn(ticker)
                if not spot or spot <= 0:
                    continue
                chains = chain_fn(ticker)
                if not chains:
                    continue
                for exp, dte, chain_data in chains:
                    if chain_data:
                        self.record_chain(ticker, exp, chain_data, spot=spot)
                        recorded += 1
            except Exception as e:
                errors += 1
                log.debug(f"OI sweep error for {ticker}: {e}")
        log.info(f"OI sweep complete: {recorded} chains from "
                f"{len(OI_SWEEP_TICKERS)} tickers ({errors} errors)")
        self.flush()

    def should_post_summary(self) -> bool:
        try:
            import pytz
            ct = pytz.timezone("America/Chicago")
            now = datetime.now(ct)
            if now.weekday() >= 5:
                return False
            today_str = now.strftime("%Y-%m-%d")
            if today_str == self._last_summary_date:
                return False
            if now.hour == OI_SUMMARY_HOUR_CT and now.minute <= 5:
                self._last_summary_date = today_str
                return True
        except Exception:
            pass
        return False

    def should_run_sweep(self) -> bool:
        try:
            import pytz
            ct = pytz.timezone("America/Chicago")
            now = datetime.now(ct)
            if now.weekday() >= 5:
                return False
            today_str = now.strftime("%Y-%m-%d")
            if today_str == self._last_sweep_date:
                return False
            if now.hour == OI_SWEEP_HOUR_CT and abs(now.minute - OI_SWEEP_MINUTE_CT) <= 2:
                return True
        except Exception:
            pass
        return False

    @property
    def status(self) -> dict:
        return {
            "tickers_tracked": len(self._today),
            "today_date": self._today_date,
            "last_summary_date": self._last_summary_date,
            "last_sweep_date": self._last_sweep_date,
            "ticker_list": sorted(self._today.keys()),
            "sweep_tickers": len(OI_SWEEP_TICKERS),
        }
