"""Reconstruct the league rules and state that actually existed at a cutoff."""

from __future__ import annotations

from pydantic import Field

from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.frozen import FrozenMapping
from fantasy_gm.domain.ids import FantasyTeamId, ObservationId, PlayerId
from fantasy_gm.domain.knowledge import of_type
from fantasy_gm.domain.league import (
    DraftBoardState,
    DraftSettings,
    FantasyRoster,
    League,
    LeagueDraftSettings,
    LeagueRosterSettings,
    LeagueRules,
    LeagueScoringSettings,
    LeagueSeasonSettings,
    LeagueWaiverSettings,
    PlatformEligibility,
    RosterRules,
    ScoringRules,
    SeasonSettings,
    WaiverBudgetState,
    WaiverSettings,
)
from fantasy_gm.domain.nfl import Position
from fantasy_gm.domain.observation import Observation
from fantasy_gm.domain.time import UtcDatetime
from fantasy_gm.player_state.store import ObservationQuery, ObservationReader


class LeagueStateIncompleteError(LookupError):
    pass


class LeagueState(DomainModel):
    league: League
    as_of: UtcDatetime
    scoring: ScoringRules | None = None
    roster_rules: RosterRules | None = None
    waivers: WaiverSettings | None = None
    season_settings: SeasonSettings | None = None
    draft: DraftSettings | None = None
    rosters: FrozenMapping[FantasyTeamId, FantasyRoster] = Field(default_factory=dict)
    waiver_budgets: FrozenMapping[FantasyTeamId, WaiverBudgetState] = Field(default_factory=dict)
    eligibility: FrozenMapping[PlayerId, frozenset[Position]] = Field(default_factory=dict)
    draft_board: DraftBoardState | None = None
    source_observation_ids: tuple[ObservationId, ...] = ()
    gaps: tuple[str, ...] = ()

    def rules(self) -> LeagueRules:
        if (
            self.scoring is None
            or self.roster_rules is None
            or self.waivers is None
            or self.season_settings is None
            or self.draft is None
        ):
            raise LeagueStateIncompleteError("; ".join(self.gaps) or "league rules incomplete")
        return LeagueRules(
            scoring=self.scoring,
            roster_rules=self.roster_rules,
            draft=self.draft,
            waivers=self.waivers,
            season_settings=self.season_settings,
        )

    @property
    def faab_budget(self) -> int | None:
        return None if self.waivers is None else self.waivers.faab_budget


def _latest[O: Observation](items: list[Observation], cls: type[O]) -> O | None:
    found = of_type(items, cls)
    return found[-1] if found else None


def reconstruct_league_state(reader: ObservationReader, league: League) -> LeagueState:
    known = reader.read(ObservationQuery(league_id=league.league_id))
    gaps: list[str] = []
    scoring = _latest(known, LeagueScoringSettings)
    roster = _latest(known, LeagueRosterSettings)
    waivers = _latest(known, LeagueWaiverSettings)
    season = _latest(known, LeagueSeasonSettings)
    draft = _latest(known, LeagueDraftSettings)
    for label, value in (
        ("scoring", scoring),
        ("roster", roster),
        ("waiver", waivers),
        ("season", season),
        ("draft", draft),
    ):
        if value is None:
            gaps.append(f"no {label} settings known at cutoff")

    rosters: dict[FantasyTeamId, FantasyRoster] = {}
    for r in of_type(known, FantasyRoster):
        rosters[r.team_id] = r
    budgets: dict[FantasyTeamId, WaiverBudgetState] = {}
    for b in of_type(known, WaiverBudgetState):
        budgets[b.team_id] = b
    eligibility: dict[PlayerId, frozenset[Position]] = {}
    for e in of_type(known, PlatformEligibility):
        eligibility[e.player_id] = e.positions
    board = _latest(known, DraftBoardState)

    used: list[Observation] = [
        *(x for x in (scoring, roster, waivers, season, draft, board) if x is not None),
        *rosters.values(),
        *budgets.values(),
    ]
    used += [e for e in of_type(known, PlatformEligibility) if eligibility.get(e.player_id)]
    return LeagueState(
        league=league,
        as_of=reader.cutoff.as_of,
        scoring=scoring.scoring if scoring else None,
        roster_rules=roster.roster_rules if roster else None,
        waivers=waivers.waivers if waivers else None,
        season_settings=season.settings if season else None,
        draft=draft.draft if draft else None,
        rosters=rosters,
        waiver_budgets=budgets,
        eligibility=eligibility,
        draft_board=board,
        source_observation_ids=tuple(dict.fromkeys(o.observation_id for o in used)),
        gaps=tuple(gaps),
    )
