"""Reference simulators: sample *given* distributions, independently across players.

Independence is a known simplification (real outcomes correlate: QB/WR stacks, game script,
shared weather). It is recorded as ``simulator_version`` so later correlated simulators are
distinguishable in the ledger. These are CPU-bound: call them from async code only through
``fantasy_gm.simulation.offload``.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

import numpy as np
from numpy.typing import NDArray

from fantasy_gm.domain.base import sha256_hex
from fantasy_gm.domain.ids import PlayerId
from fantasy_gm.domain.seeds import SeedSpec
from fantasy_gm.runtime import runtime_fingerprint
from fantasy_gm.simulation.distributions import PlayerWeekDistribution
from fantasy_gm.simulation.interfaces import (
    MatchupRequest,
    MatchupResult,
    ReproducibilityEnvelope,
    WeeklyOutcomeRequest,
    WeeklyOutcomeResult,
)
from fantasy_gm.simulation.rng import generator_for

_QUANTILES = (0.05, 0.25, 0.5, 0.75, 0.95)
_NO_CONFIG_HASH = sha256_hex("{}")  # reference simulators have no tunable configuration


def _sample_players(
    lineup: tuple[PlayerWeekDistribution, ...], n: int, seed: SeedSpec
) -> Mapping[PlayerId, NDArray[np.float64]]:
    out: dict[PlayerId, NDArray[np.float64]] = {}
    for dist in lineup:
        # One stream per player-week: adding/removing another player never changes this draw
        # (common random numbers across candidate lineups).
        rng = generator_for(seed.child("player", dist.player_id, str(dist.season), str(dist.week)))
        arr = dist.sample(rng, n)
        arr.setflags(write=False)  # the array's own contents are read-only ...
        out[dist.player_id] = arr
    return MappingProxyType(out)  # ... and so is the mapping itself (ADR 0019)


def _artifacts(*lineups: tuple[PlayerWeekDistribution, ...]) -> tuple[str, ...]:
    return tuple(
        sorted({d.source_artifact_hash for lu in lineups for d in lu if d.source_artifact_hash})
    )


class IndependentWeeklyOutcomeSimulator:
    name = "independent_weekly_outcome"
    version = "0.1.1"

    def simulate(self, request: WeeklyOutcomeRequest) -> WeeklyOutcomeResult:
        ids = [d.player_id for d in request.distributions]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate player in request")
        return WeeklyOutcomeResult(
            run=ReproducibilityEnvelope(
                runtime=runtime_fingerprint(),
                simulator=self.name,
                simulator_version=self.version,
                config_hash=_NO_CONFIG_HASH,
                seed=request.seed,
                n_sims=request.n_sims,
                request_hash=request.content_hash(),
                model_artifact_hashes=_artifacts(request.distributions),
            ),
            samples=_sample_players(request.distributions, request.n_sims, request.seed),
        )


class IndependentMatchupSimulator:
    name = "independent_matchup"
    version = "0.1.1"

    def simulate(self, request: MatchupRequest) -> MatchupResult:
        home_ids = {d.player_id for d in request.home_lineup}
        if home_ids & {d.player_id for d in request.away_lineup}:
            raise ValueError("a player cannot appear in both lineups")
        home = _sample_players(request.home_lineup, request.n_sims, request.seed)
        away = _sample_players(request.away_lineup, request.n_sims, request.seed)
        home_total = np.sum(np.stack(list(home.values())), axis=0)
        away_total = np.sum(np.stack(list(away.values())), axis=0)
        margin = home_total - away_total
        return MatchupResult(
            run=ReproducibilityEnvelope(
                runtime=runtime_fingerprint(),
                simulator=self.name,
                simulator_version=self.version,
                config_hash=_NO_CONFIG_HASH,
                seed=request.seed,
                n_sims=request.n_sims,
                request_hash=request.content_hash(),
                model_artifact_hashes=_artifacts(request.home_lineup, request.away_lineup),
            ),
            p_home_win=float(np.mean(margin > 0)),
            p_away_win=float(np.mean(margin < 0)),
            p_tie=float(np.mean(margin == 0)),
            home_points_mean=float(np.mean(home_total)),
            away_points_mean=float(np.mean(away_total)),
            margin_quantiles={q: float(np.quantile(margin, q)) for q in _QUANTILES},
        )
