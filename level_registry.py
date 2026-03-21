# level_registry.py
# ═══════════════════════════════════════════════════════════════════
# Unified Level Registry with Confluence Scoring
#
# Every level in the system — daily S/R, EM boundaries, OI walls,
# gamma walls, pivots, OR boundaries, intraday rejection zones —
# lives in one registry with a quality score from 0-100 and a
# tier rating of A/B/C.
#
# Scoring model:
#   +25  daily + intraday align at same zone
#   +20  OI wall or gamma wall at same zone
#   +15  OR high/low overlaps
#   +10  EM 1σ boundary overlaps
#   +10  3+ confirmed touches
#   -10  too fresh / still forming
#   -10  inside pin/no-man's-land (crowded)
#   -15  far outside session's executable range
#
# Levels feed into: entry quality, target selection, stop placement.
# ═══════════════════════════════════════════════════════════════════

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Set, Dict

log = logging.getLogger(__name__)

# ── Constants ──
CONFLUENCE_ZONE_PCT = 0.08   # 0.08% — levels within this merge
EXECUTABLE_RANGE_MULT = 3.0  # beyond 3x session range = too far
MIN_TOUCHES_BONUS = 3
LEVEL_FRESHNESS_PENALTY_SEC = 120  # levels younger than this are penalized


@dataclass
class Level:
    """A single price level with source attribution and quality scoring."""
    price: float
    sources: Set[str] = field(default_factory=set)
    touch_count: int = 0
    first_seen_epoch: float = 0.0
    last_touched_epoch: float = 0.0
    quality_score: int = 0
    quality_tier: str = "C"       # A / B / C
    kind: str = "neutral"         # support / resistance / neutral
    active: bool = True
    # Context for scoring
    in_pin_zone: bool = False
    distance_to_spot_pct: float = 0.0

    @property
    def source_count(self) -> int:
        return len(self.sources)


# ── Source tags (used in scoring) ──
SRC_DAILY_SUPPORT = "daily_support"
SRC_DAILY_RESISTANCE = "daily_resistance"
SRC_PUT_WALL = "put_wall"
SRC_CALL_WALL = "call_wall"
SRC_GAMMA_WALL = "gamma_wall"
SRC_GAMMA_FLIP = "gamma_flip"
SRC_PIVOT = "pivot"
SRC_S1 = "S1"
SRC_R1 = "R1"
SRC_EM_LOW = "EM_1sd_low"
SRC_EM_HIGH = "EM_1sd_high"
SRC_EM_2SD_LOW = "EM_2sd_low"
SRC_EM_2SD_HIGH = "EM_2sd_high"
SRC_OR_HIGH = "OR_high"
SRC_OR_LOW = "OR_low"
SRC_INTRADAY_SUPPORT = "intraday_support"
SRC_INTRADAY_RESISTANCE = "intraday_resistance"
SRC_SESSION_HIGH = "session_high"
SRC_SESSION_LOW = "session_low"
SRC_FIB_SUPPORT = "fib_support"
SRC_FIB_RESISTANCE = "fib_resistance"
SRC_VPOC = "vpoc"
SRC_MAX_PAIN = "max_pain"

# Source category groupings for scoring
DAILY_SOURCES = {SRC_DAILY_SUPPORT, SRC_DAILY_RESISTANCE}
OI_SOURCES = {SRC_PUT_WALL, SRC_CALL_WALL, SRC_GAMMA_WALL}
OR_SOURCES = {SRC_OR_HIGH, SRC_OR_LOW}
EM_SOURCES = {SRC_EM_LOW, SRC_EM_HIGH, SRC_EM_2SD_LOW, SRC_EM_2SD_HIGH}
INTRADAY_SOURCES = {SRC_INTRADAY_SUPPORT, SRC_INTRADAY_RESISTANCE, SRC_SESSION_HIGH, SRC_SESSION_LOW}


class LevelRegistry:
    """
    Unified level registry. Ingests levels from all sources, merges
    nearby prices into single levels, scores confluence, and provides
    ranked output for entry/exit/target decisions.
    """

    def __init__(self, merge_zone_pct: float = CONFLUENCE_ZONE_PCT):
        self._levels: List[Level] = []
        self._merge_zone_pct = merge_zone_pct

    def clear(self):
        self._levels.clear()

    # ── Ingestion ──

    def add(self, price: float, source: str, kind: str = "neutral",
            touch_count: int = 1, epoch: float = 0.0):
        """Add a level. Merges into existing level if within zone tolerance."""
        if price is None or price <= 0:
            return
        tol = price * self._merge_zone_pct / 100
        for lvl in self._levels:
            if abs(lvl.price - price) <= tol:
                lvl.sources.add(source)
                lvl.touch_count = max(lvl.touch_count, touch_count)
                if epoch > lvl.last_touched_epoch:
                    lvl.last_touched_epoch = epoch
                if not lvl.first_seen_epoch or (epoch and epoch < lvl.first_seen_epoch):
                    lvl.first_seen_epoch = epoch
                # Upgrade kind if we get directional info
                if kind != "neutral" and lvl.kind == "neutral":
                    lvl.kind = kind
                return
        self._levels.append(Level(
            price=round(price, 2),
            sources={source},
            touch_count=touch_count,
            first_seen_epoch=epoch,
            last_touched_epoch=epoch,
            kind=kind,
        ))

    def ingest_thesis_levels(self, thesis_levels, spot: float = 0, epoch: float = 0):
        """Bulk ingest from a ThesisLevels dataclass."""
        lvl = thesis_levels
        _s, _r = "support", "resistance"
        _map = [
            (lvl.local_support, SRC_DAILY_SUPPORT, _s),
            (lvl.local_resistance, SRC_DAILY_RESISTANCE, _r),
            (lvl.put_wall, SRC_PUT_WALL, _s),
            (lvl.call_wall, SRC_CALL_WALL, _r),
            (lvl.gamma_wall, SRC_GAMMA_WALL, "neutral"),
            (lvl.gamma_flip, SRC_GAMMA_FLIP, "neutral"),
            (lvl.pivot, SRC_PIVOT, "neutral"),
            (lvl.s1, SRC_S1, _s),
            (lvl.r1, SRC_R1, _r),
            (lvl.em_low, SRC_EM_LOW, _s),
            (lvl.em_high, SRC_EM_HIGH, _r),
            (lvl.em_2sd_low, SRC_EM_2SD_LOW, _s),
            (lvl.em_2sd_high, SRC_EM_2SD_HIGH, _r),
            (lvl.fib_support, SRC_FIB_SUPPORT, _s),
            (lvl.fib_resistance, SRC_FIB_RESISTANCE, _r),
            (lvl.vpoc, SRC_VPOC, "neutral"),
            (lvl.max_pain, SRC_MAX_PAIN, "neutral"),
        ]
        for price, src, kind in _map:
            if price is not None and price > 0:
                self.add(price, src, kind, epoch=epoch)

    def ingest_opening_range(self, or_obj, epoch: float = 0):
        """Ingest opening range high/low as levels."""
        if or_obj and or_obj.is_complete:
            if or_obj.high > 0:
                self.add(or_obj.high, SRC_OR_HIGH, "resistance", epoch=epoch)
            if or_obj.low > 0:
                self.add(or_obj.low, SRC_OR_LOW, "support", epoch=epoch)

    def ingest_intraday_levels(self, intraday_levels: list, epoch: float = 0):
        """Ingest from thesis monitor's intraday level list."""
        for il in intraday_levels:
            if not il.active:
                continue
            src = SRC_INTRADAY_SUPPORT if il.kind == "support" else SRC_INTRADAY_RESISTANCE
            self.add(il.price, src, il.kind, touch_count=il.touches, epoch=epoch)

    # ── Scoring ──

    def score_all(self, spot: float, session_range: float = 0,
                  pin_low: float = None, pin_high: float = None,
                  now_epoch: float = 0):
        """Score every level based on confluence, freshness, context."""
        import time as _t
        now = now_epoch or _t.time()
        for lvl in self._levels:
            score = 0
            srcs = lvl.sources
            # +25: daily + intraday align
            if srcs & DAILY_SOURCES and srcs & INTRADAY_SOURCES:
                score += 25
            # +20: OI wall or gamma wall present
            if srcs & OI_SOURCES:
                score += 20
            # +15: OR boundary overlaps
            if srcs & OR_SOURCES:
                score += 15
            # +10: EM boundary overlaps
            if srcs & EM_SOURCES:
                score += 10
            # +10: 3+ confirmed touches
            if lvl.touch_count >= MIN_TOUCHES_BONUS:
                score += 10
            # +5 per additional source beyond 2
            if len(srcs) > 2:
                score += (len(srcs) - 2) * 5
            # -10: too fresh
            if lvl.first_seen_epoch and (now - lvl.first_seen_epoch) < LEVEL_FRESHNESS_PENALTY_SEC:
                score -= 10
            # -10: inside pin zone (crowded)
            if pin_low is not None and pin_high is not None:
                if pin_low <= lvl.price <= pin_high:
                    score -= 10
                    lvl.in_pin_zone = True
            # -15: too far from spot (outside executable range)
            if spot > 0 and session_range > 0:
                dist = abs(lvl.price - spot)
                if dist > session_range * EXECUTABLE_RANGE_MULT:
                    score -= 15
            # Distance to spot
            if spot > 0:
                lvl.distance_to_spot_pct = (lvl.price - spot) / spot * 100
            lvl.quality_score = max(0, min(100, score))
            # Tier assignment
            if lvl.quality_score >= 45:
                lvl.quality_tier = "A"
            elif lvl.quality_score >= 25:
                lvl.quality_tier = "B"
            else:
                lvl.quality_tier = "C"

    # ── Queries ──

    def get_all(self, min_score: int = 0) -> List[Level]:
        """All levels sorted by quality score descending."""
        return sorted(
            [l for l in self._levels if l.quality_score >= min_score and l.active],
            key=lambda l: l.quality_score,
            reverse=True,
        )

    def get_support_levels(self, spot: float, min_score: int = 0) -> List[Level]:
        """Support levels below spot, sorted nearest first."""
        return sorted(
            [l for l in self._levels if l.price < spot and l.kind in ("support", "neutral")
             and l.quality_score >= min_score and l.active],
            key=lambda l: spot - l.price,
        )

    def get_resistance_levels(self, spot: float, min_score: int = 0) -> List[Level]:
        """Resistance levels above spot, sorted nearest first."""
        return sorted(
            [l for l in self._levels if l.price > spot and l.kind in ("resistance", "neutral")
             and l.quality_score >= min_score and l.active],
            key=lambda l: l.price - spot,
        )

    def get_nearest(self, spot: float, direction: str = "both") -> Dict[str, Optional[Level]]:
        """Get nearest support and/or resistance."""
        sups = self.get_support_levels(spot)
        ress = self.get_resistance_levels(spot)
        result = {"support": None, "resistance": None}
        if direction in ("both", "support") and sups:
            result["support"] = sups[0]
        if direction in ("both", "resistance") and ress:
            result["resistance"] = ress[0]
        return result

    def get_targets(self, spot: float, direction: str, count: int = 3, min_score: int = 10) -> List[Level]:
        """Get next N target levels in trade direction, ranked by proximity then quality."""
        if direction == "SHORT":
            candidates = self.get_support_levels(spot, min_score=min_score)
        else:
            candidates = self.get_resistance_levels(spot, min_score=min_score)
        return candidates[:count]

    def get_level_at(self, price: float) -> Optional[Level]:
        """Find the level nearest to a price, if within merge zone."""
        tol = price * self._merge_zone_pct / 100
        best = None
        best_dist = float('inf')
        for lvl in self._levels:
            d = abs(lvl.price - price)
            if d <= tol and d < best_dist:
                best = lvl
                best_dist = d
        return best

    def level_count(self) -> int:
        return len([l for l in self._levels if l.active])

    def summary(self, spot: float, top_n: int = 8) -> str:
        """Human-readable summary of top levels."""
        levels = self.get_all()[:top_n]
        lines = [f"Level Registry ({self.level_count()} levels)"]
        for l in levels:
            side = "▼" if l.price < spot else "▲" if l.price > spot else "◆"
            dist = (l.price - spot) / spot * 100
            srcs = ", ".join(sorted(l.sources)[:3])
            lines.append(f"  {side} ${l.price:.2f} [{l.quality_tier}:{l.quality_score}] "
                         f"({dist:+.2f}%) {srcs}")
        return "\n".join(lines)
