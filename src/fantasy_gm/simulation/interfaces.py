"""Simulator protocols and their request/result contracts.

Every request carries an explicit ``SeedSpec`` and every result a ``SimulationRunRecord`` so any
run referenced by a decision can be reproduced exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np
from numpy.typing import NDArray
from pydantic import Field

from fantasy_gm.domain.actions import Action
from fantasy_gm.domain.artifacts import RuntimeFingerprint
from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.ids import CandidateId, FantasyTeamId, LeagueId, PlayerId
from fantasy_gm.domain.league import DraftPick, League, LeagueRules
from fantasy_gm.domain.seeds import SeedSpec
from fantasy_gm.simulation.distributions import PlayerWeekDistribution


class ReproducibilityEnvelope(DomainModel):
    """Everything needed to identify (and, in a pinned environment, re-run) a simulation.

    Bit-for-bit reproduction is only claimed when ``runtime`` (commit, Python, numpy, lockfile
    hash) matches; across arbitrary future library versions it is not promised.
    """

    runtime: RuntimeFingerprint
    simulator: str
    simulator_version: str
    config_hash: str
    seed: SeedSpec
    n_sims: int
    request_hash: str
    model_artifact_hashes: tuple[str, ...] = ()


# --------------------------------------------------------------------------- weekly outcomes


class WeeklyOutcomeRequest(DomainModel):
    distributions: tuple[PlayerWeekDistribution, ...] = Field(min_length=1)
    n_sims: int = Field(ge=1, le=10_000_000)
    seed: SeedSpec


@dataclass(frozen=True)
class WeeklyOutcomeResult:
    run: ReproducibilityEnvelope
    samples: dict[PlayerId, NDArray[np.float64]]  # player -> (n_sims,) read-only array


class WeeklyPlayerOutcomeSimulator(Protocol):
    def simulate(self, request: WeeklyOutcomeRequest) -> WeeklyOutcomeResult: ...


# --------------------------------------------------------------------------- matchup


class MatchupRequest(DomainModel):
    home_team_id: FantasyTeamId
    away_team_id: FantasyTeamId
    home_lineup: tuple[PlayerWeekDistribution, ...] = Field(min_length=1)
    away_lineup: tuple[PlayerWeekDistribution, ...] = Field(min_length=1)
    n_sims: int = Field(ge=1, le=10_000_000)
    seed: SeedSpec


class MatchupResult(DomainModel):
    run: ReproducibilityEnvelope
    p_home_win: float
    p_away_win: float
    p_tie: float
    home_points_mean: float
    away_points_mean: float
    margin_quantiles: dict[float, float]  # home - away


class MatchupSimulator(Protocol):
    def simulate(self, request: MatchupRequest) -> MatchupResult: ...


# --------------------------------------------------------------------------- remaining season


class ScheduledMatchup(DomainModel):
    week: int
    home_team_id: FantasyTeamId
    away_team_id: FantasyTeamId


class TeamRecord(DomainModel):
    team_id: FantasyTeamId
    wins: int = 0
    losses: int = 0
    ties: int = 0
    points_for: float = 0.0


class RemainingSeasonRequest(DomainModel):
    league: League
    rules: LeagueRules  # the rules in force at the simulation's cutoff
    current_week: int
    standings: tuple[TeamRecord, ...]
    remaining_schedule: tuple[ScheduledMatchup, ...]
    # team -> week -> lineup distributions (lineup choice is an upstream decision).
    lineups: dict[FantasyTeamId, dict[int, tuple[PlayerWeekDistribution, ...]]]
    n_sims: int = Field(ge=1)
    seed: SeedSpec


class RemainingSeasonResult(DomainModel):
    run: ReproducibilityEnvelope
    p_playoffs: dict[FantasyTeamId, float]
    p_championship: dict[FantasyTeamId, float]
    expected_wins: dict[FantasyTeamId, float]


class RemainingSeasonSimulator(Protocol):
    def simulate(self, request: RemainingSeasonRequest) -> RemainingSeasonResult: ...


# --------------------------------------------------------------------------- draft continuation


class DraftCandidateAction(DomainModel):
    candidate_id: CandidateId
    action: Action


class DraftContinuationRequest(DomainModel):
    league_id: LeagueId
    our_team_id: FantasyTeamId
    picks: tuple[DraftPick, ...]  # full order; made picks have selected_player_id
    current_overall: int
    available_player_ids: tuple[PlayerId, ...]
    candidate_actions: tuple[DraftCandidateAction, ...] = Field(min_length=1)
    opponent_model: str  # identifier of the opponent drafting-behaviour model used
    value_model: str  # identifier of the finished-roster value model used
    n_sims: int = Field(ge=1)
    seed: SeedSpec


class DraftContinuationResult(DomainModel):
    run: ReproducibilityEnvelope
    # candidate -> quantiles of the finished-roster objective (e.g. championship equity)
    candidate_objective_quantiles: dict[CandidateId, dict[float, float]]
    candidate_objective_mean: dict[CandidateId, float]


class DraftContinuationSimulator(Protocol):
    def simulate(self, request: DraftContinuationRequest) -> DraftContinuationResult: ...
