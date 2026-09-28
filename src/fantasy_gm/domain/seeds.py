"""Storeable RNG seed specification (pure data; generators live in ``fantasy_gm.simulation``)."""

from __future__ import annotations

from pydantic import Field

from fantasy_gm.domain.base import DomainModel


class SeedSpec(DomainModel):
    """A root seed plus a label path. Child streams are derived by label, never by call order,
    so adding a new consumer of randomness cannot perturb existing streams."""

    root_seed: int = Field(ge=0, lt=2**63)
    path: tuple[str, ...] = ()

    def child(self, *labels: str) -> SeedSpec:
        return SeedSpec(root_seed=self.root_seed, path=(*self.path, *labels))
