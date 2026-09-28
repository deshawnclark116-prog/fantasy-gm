"""Reference simulators: sample *given* distributions, independently across players.

Independence is a known simplification (real outcomes correlate: QB/WR stacks, game script,
shared weather). It is recorded as ``simulator_version`` so later correlated simulators are
distinguishable in the ledger.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from fantasy_gm.domain.ids import PlayerId
from fantasy_gm.domain.seeds import SeedSpec
from fantasy_gm.simulation.distributions import PlayerWeekDistribution
from fantasy_gm.simulation.interfaces import (
    MatchupRequest,
    MatchupResult,
    SimulationRunRecord,
    WeeklyOutcomeRequest,
    WeeklyOutcomeResult,
)
from fantasy_gm.simulation.rng import NUMPY_VERSION, generator_for

_QUANTILES = (0.05, 0.25, 0.5, 0.75, 0.95)


def _sample_players(
    lineup: tuple[PlayerWeekDistribution, ...], n: int, seed: SeedSpec
) -> dict[PlayerId, NDArray[np.float64]]:
    out: dict[PlayerId, NDArray[np.float64]] = {}
    for dist in lineup:
        # One stream per player-week: adding/removing another player never changes this draw
        # (common random numbers across candidate lineups).
        rng = generator_for(seed.child("player", dist.player_id, str(dist.season), str(dist.week)))
        arr = dist.sample(rng, n)
        arr.setflags(write=False)
        out[dist.player_id] = arr
    return out


class IndependentWeeklyOutcomeSimulator:
    name = "independent_weekly_outcome"
    version = "0.1.0"

    def simulate(self, request: WeeklyOutcomeRequest) -> WeeklyOutcomeResult:
        ids = [d.player_id for d in request.distributions]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate player in request")
        samples = _sample_players(request.distributions, request.n_sims, request.seed)
        return WeeklyOutcomeResult(
            run=SimulationRunRecord(
                simulator=self.name,
                simulator_version=self.version,
                seed=request.seed,
                n_sims=request.n_sims,
                request_hash=request.content_hash(),
                numpy_version=NUMPY_VERSION,
            ),
            samples=samples,
        )


class IndependentMatchupSimulator:
    name = "independent_matchup"
    version = "0.1.0"

    def simulate(self, request: MatchupRequest) -> MatchupResult:
        home_ids = {d.player_id for d in request.home_lineup}
        if home_ids & {d.player_id for d in request.away_lineup}:
            raise ValueError("a player cannot appear in both lineups")
        # Players share the same per-player stream regardless of side (true to reality: a
        # player's outcome does not depend on which fantasy team rosters him).
        home = _sample_players(request.home_lineup, request.n_sims, request.seed)
        away = _sample_players(request.away_lineup, request.n_sims, request.seed)
        home_total = np.sum(np.stack(list(home.values())), axis=0)
        away_total = np.sum(np.stack(list(away.values())), axis=0)
        margin = home_total - away_total
        return MatchupResult(
            run=SimulationRunRecord(
                simulator=self.name,
                simulator_version=self.version,
                seed=request.seed,
                n_sims=request.n_sims,
                request_hash=request.content_hash(),
                numpy_version=NUMPY_VERSION,
            ),
            p_home_win=float(np.mean(margin > 0)),
            p_away_win=float(np.mean(margin < 0)),
            p_tie=float(np.mean(margin == 0)),
            home_points_mean=float(np.mean(home_total)),
            away_points_mean=float(np.mean(away_total)),
            margin_quantiles={q: float(np.quantile(margin, q)) for q in _QUANTILES},
        )
