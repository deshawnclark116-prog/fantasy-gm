"""Registry of concrete observation types, used for polymorphic (de)serialization."""

from __future__ import annotations

from typing import Any

from fantasy_gm.domain.league import FantasyRoster
from fantasy_gm.domain.market import MarketADPObservation
from fantasy_gm.domain.nfl import (
    CoachingAssignment,
    ContractSignal,
    DepthChartSignal,
    DraftCapital,
    InjuryStatus,
    NFLGame,
    PlayerTeamAssignment,
    RosterTransactionSignal,
    UsageSnapshot,
)
from fantasy_gm.domain.observation import Observation

OBSERVATION_TYPES: dict[str, type[Observation]] = {
    cls.kind: cls
    for cls in (
        NFLGame,
        PlayerTeamAssignment,
        DraftCapital,
        ContractSignal,
        DepthChartSignal,
        UsageSnapshot,
        InjuryStatus,
        RosterTransactionSignal,
        CoachingAssignment,
        MarketADPObservation,
        FantasyRoster,
    )
}


def deserialize_observation(kind: str, payload: dict[str, Any]) -> Observation:
    try:
        cls = OBSERVATION_TYPES[kind]
    except KeyError as exc:
        raise ValueError(f"unknown observation kind {kind!r}") from exc
    return cls.model_validate(payload)
