"""Score a stat line under arbitrary league scoring rules, with a full breakdown."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.league import AllowedTier, ScoringRules, StatKey
from fantasy_gm.domain.nfl import Position


class ScoreLine(DomainModel):
    source: str  # stat key, bonus description or tier description
    quantity: Decimal
    rate: Decimal
    points: Decimal


class ScoreBreakdown(DomainModel):
    total: Decimal
    lines: tuple[ScoreLine, ...]


def _tier_points(tiers: tuple[AllowedTier, ...], allowed: Decimal) -> AllowedTier:
    for tier in tiers:
        upper = tier.max_allowed
        if allowed >= tier.min_allowed and (upper is None or allowed <= upper):
            return tier
    raise ValueError(f"no tier covers allowed amount {allowed}")


def score_stat_line(
    stats: Mapping[StatKey, Decimal | int], position: Position, rules: ScoringRules
) -> ScoreBreakdown:
    lines: list[ScoreLine] = []
    tiered = {
        StatKey.DST_POINTS_ALLOWED: rules.dst_points_allowed_tiers,
        StatKey.DST_YARDS_ALLOWED: rules.dst_yards_allowed_tiers,
    }
    for stat, raw in sorted(stats.items()):
        qty = Decimal(raw)
        tiers = tiered.get(stat)
        if tiers and position is Position.DST:
            tier = _tier_points(tiers, qty)
            lines.append(
                ScoreLine(source=f"{stat}_tier", quantity=qty, rate=tier.points, points=tier.points)
            )
            continue
        rate = rules.rate(stat, position)
        if rate:
            lines.append(ScoreLine(source=stat.value, quantity=qty, rate=rate, points=qty * rate))
    for bonus in rules.bonuses:
        if bonus.positions is not None and position not in bonus.positions:
            continue
        value = Decimal(stats.get(bonus.stat, 0))
        if value >= bonus.threshold:
            lines.append(
                ScoreLine(
                    source=f"bonus:{bonus.stat}>={bonus.threshold}",
                    quantity=Decimal(1),
                    rate=bonus.points,
                    points=bonus.points,
                )
            )
    return ScoreBreakdown(total=sum((ln.points for ln in lines), Decimal(0)), lines=tuple(lines))
