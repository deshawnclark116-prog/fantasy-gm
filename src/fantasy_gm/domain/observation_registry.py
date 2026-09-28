"""Registry of concrete observation types, keyed by ``kind``."""

from __future__ import annotations

from fantasy_gm.domain.league import (
    DraftBoardState,
    FantasyRoster,
    LeagueDraftSettings,
    LeagueRosterSettings,
    LeagueScoringSettings,
    LeagueSeasonSettings,
    LeagueWaiverSettings,
    PlatformEligibility,
    WaiverBudgetState,
)
from fantasy_gm.domain.market import MarketADPObservation
from fantasy_gm.domain.nfl import (
    CoachingAssignment,
    ContractSignal,
    DepthChartSignal,
    DraftCapital,
    InjuryStatus,
    NFLGame,
    PlayerTeamAssignment,
    PreseasonGameContext,
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
        PreseasonGameContext,
        MarketADPObservation,
        FantasyRoster,
        LeagueScoringSettings,
        LeagueRosterSettings,
        LeagueWaiverSettings,
        LeagueSeasonSettings,
        LeagueDraftSettings,
        WaiverBudgetState,
        PlatformEligibility,
        DraftBoardState,
    )
}
