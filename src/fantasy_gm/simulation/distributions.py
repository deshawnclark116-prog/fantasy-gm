"""Predictive-distribution representations consumed by simulators.

These are *containers* for distributions produced by a (future) projection model. Nothing here
estimates anything.
"""

from __future__ import annotations

from typing import Annotated, Literal

import numpy as np
from numpy.typing import NDArray
from pydantic import Field

from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.ids import PlayerId


class EmpiricalDistribution(DomainModel):
    dist_type: Literal["empirical"] = "empirical"
    samples: tuple[float, ...] = Field(min_length=1)

    def sample(self, rng: np.random.Generator, n: int) -> NDArray[np.float64]:
        return rng.choice(np.asarray(self.samples, dtype=np.float64), size=n, replace=True)


class TruncatedNormalDistribution(DomainModel):
    """Normal(mean, stdev) clipped below at ``lower`` (fantasy points can be negative)."""

    dist_type: Literal["truncated_normal"] = "truncated_normal"
    mean: float
    stdev: float = Field(ge=0.0)
    lower: float | None = None

    def sample(self, rng: np.random.Generator, n: int) -> NDArray[np.float64]:
        draws = rng.normal(self.mean, self.stdev, size=n)
        if self.lower is not None:
            draws = np.maximum(draws, self.lower)
        return draws


ConditionalDistribution = Annotated[
    EmpiricalDistribution | TruncatedNormalDistribution, Field(discriminator="dist_type")
]


class PlayerWeekDistribution(DomainModel):
    """Points distribution for one player-week: inactive (0 pts) with prob 1 - p_active,
    otherwise a draw from ``conditional``."""

    player_id: PlayerId
    season: int
    week: int = Field(ge=0, le=25)
    p_active: float = Field(ge=0.0, le=1.0)
    conditional: ConditionalDistribution
    source_model: str = Field(min_length=1)  # which upstream model produced this

    def sample(self, rng: np.random.Generator, n: int) -> NDArray[np.float64]:
        active = rng.random(n) < self.p_active
        points = self.conditional.sample(rng, n)
        return np.where(active, points, 0.0)
