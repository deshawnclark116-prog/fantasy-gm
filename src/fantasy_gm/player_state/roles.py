"""Observed (empirical) latent-role vectors from raw usage. Measurement, not projection."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from fantasy_gm.domain.ids import PlayerId
from fantasy_gm.domain.nfl import Position, UsageSnapshot
from fantasy_gm.domain.roles import (
    ROLE_DIMENSIONS,
    LatentRoleVector,
    RoleDimension,
    RoleEstimate,
    RoleEstimateStatus,
    dimensions_for,
)

# (usage, recency_index) -> (evidence_weight, recency_weight)
GameWeight = Callable[[UsageSnapshot, int], tuple[float, float]]


def recency_weight(half_life_games: float) -> GameWeight:
    def weight(_: UsageSnapshot, index: int) -> tuple[float, float]:
        return 1.0, float(0.5 ** (index / half_life_games))

    return weight


def _sum(u: UsageSnapshot, metrics: tuple[object, ...]) -> float | None:
    total = 0.0
    for m in metrics:
        value = u.metrics.get(m)  # type: ignore[call-overload]
        if value is None:
            return None  # any missing component -> unobserved in this game (never zero)
        total += value
    return total


def observed_role_vector(
    player_id: PlayerId,
    position: Position,
    usage: Sequence[UsageSnapshot],
    as_of: datetime,
    weight: GameWeight | None = None,
) -> LatentRoleVector:
    """Pooled, recency-weighted ratio per dimension: sum(w*num) / sum(w*den).

    ``usage`` should be most-recent-first. Games missing a dimension's inputs are skipped for
    that dimension only.
    """
    weigh = weight or recency_weight(4.0)
    estimates: dict[RoleDimension, RoleEstimate] = {}
    for dim in dimensions_for(position):
        spec = ROLE_DIMENSIONS[dim]
        if not spec.supported:
            estimates[dim] = RoleEstimate(
                dimension=dim, family=spec.family, status=RoleEstimateStatus.NOT_YET_SUPPORTED
            )
            continue
        num_total = den_total = n_eff = 0.0
        games = 0
        sources = []
        for i, u in enumerate(usage):
            num = _sum(u, spec.numerator)
            den = _sum(u, spec.denominator)
            if num is None or den is None or den <= 0:
                continue
            evidence_w, recency_w = weigh(u, i)
            if evidence_w <= 0:
                continue
            w = evidence_w * recency_w
            num_total += w * num
            den_total += w * den
            n_eff += evidence_w
            games += 1
            sources.append(u.observation_id)
        if games == 0 or den_total <= 0:
            estimates[dim] = RoleEstimate(
                dimension=dim, family=spec.family, status=RoleEstimateStatus.UNOBSERVED
            )
            continue
        estimates[dim] = RoleEstimate(
            dimension=dim,
            family=spec.family,
            status=RoleEstimateStatus.OBSERVED,
            value=num_total / den_total,
            games=games,
            effective_games=n_eff,
            pooled_numerator=num_total,
            pooled_denominator=den_total,
            source_observation_ids=tuple(sources),
        )
    return LatentRoleVector(
        player_id=player_id, position=position, as_of=as_of, estimates=estimates
    )
