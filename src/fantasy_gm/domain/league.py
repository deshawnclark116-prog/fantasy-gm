"""Fantasy league configuration and state.

Nothing here assumes a particular platform, scoring format, roster shape or a salary cap.
Scoring and roster rules are data, validated for internal consistency only.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import ClassVar, Self

from pydantic import Field, model_validator

from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.frozen import FrozenMapping, FrozenSet
from fantasy_gm.domain.ids import FantasyTeamId, LeagueId, PlayerId
from fantasy_gm.domain.nfl import Position
from fantasy_gm.domain.observation import Observation
from fantasy_gm.domain.time import UtcDatetime

# --------------------------------------------------------------------------- scoring


class StatKey(StrEnum):
    """Scoreable statistics. Provider adapters must map their stat codes onto these explicitly;
    an unmapped provider stat is an ingestion error, never silently dropped."""

    PASS_ATT = "pass_att"
    PASS_CMP = "pass_cmp"
    PASS_YD = "pass_yd"
    PASS_TD = "pass_td"
    PASS_INT = "pass_int"
    PASS_2PT = "pass_2pt"
    PASS_FIRST_DOWN = "pass_first_down"
    PASS_SACKED = "pass_sacked"
    RUSH_ATT = "rush_att"
    RUSH_YD = "rush_yd"
    RUSH_TD = "rush_td"
    RUSH_2PT = "rush_2pt"
    RUSH_FIRST_DOWN = "rush_first_down"
    REC = "rec"
    REC_YD = "rec_yd"
    REC_TD = "rec_td"
    REC_2PT = "rec_2pt"
    REC_FIRST_DOWN = "rec_first_down"
    FUMBLE_LOST = "fumble_lost"
    RETURN_TD = "return_td"
    FG_MADE_0_39 = "fg_made_0_39"
    FG_MADE_40_49 = "fg_made_40_49"
    FG_MADE_50_PLUS = "fg_made_50_plus"
    FG_MISSED = "fg_missed"
    XP_MADE = "xp_made"
    XP_MISSED = "xp_missed"
    DST_SACK = "dst_sack"
    DST_INT = "dst_int"
    DST_FUMBLE_RECOVERY = "dst_fumble_recovery"
    DST_TD = "dst_td"
    DST_SAFETY = "dst_safety"
    DST_BLOCKED_KICK = "dst_blocked_kick"
    DST_POINTS_ALLOWED = "dst_points_allowed"
    DST_YARDS_ALLOWED = "dst_yards_allowed"


class ThresholdBonus(DomainModel):
    """Award ``points`` once when ``stat >= threshold`` (optionally only for some positions)."""

    stat: StatKey
    threshold: Decimal
    points: Decimal
    positions: FrozenSet[Position] | None = None


class AllowedTier(DomainModel):
    """Points for an allowed-amount band, inclusive: ``min_allowed <= x <= max_allowed``."""

    min_allowed: int = Field(ge=0)
    max_allowed: int | None = None  # None = unbounded
    points: Decimal

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.max_allowed is not None and self.max_allowed < self.min_allowed:
            raise ValueError("max_allowed must be >= min_allowed")
        return self


def _validate_tiers(tiers: tuple[AllowedTier, ...], label: str) -> None:
    if not tiers:
        return
    ordered = sorted(tiers, key=lambda t: t.min_allowed)
    if ordered[0].min_allowed != 0:
        raise ValueError(f"{label} tiers must start at 0")
    for prev, nxt in pairwise(ordered):
        if prev.max_allowed is None or nxt.min_allowed != prev.max_allowed + 1:
            raise ValueError(f"{label} tiers must be contiguous and non-overlapping")
    if ordered[-1].max_allowed is not None:
        raise ValueError(f"{label} tiers must end with an unbounded tier")


class ScoringRules(DomainModel):
    name: str
    # FrozenMapping (ADR 0019): scoring tables are league-state evidence handed to decision
    # engines, and must not be mutable in place after being read and hashed.
    per_stat: FrozenMapping[StatKey, Decimal]
    position_overrides: FrozenMapping[Position, FrozenMapping[StatKey, Decimal]] = Field(
        default_factory=dict
    )
    bonuses: tuple[ThresholdBonus, ...] = ()
    dst_points_allowed_tiers: tuple[AllowedTier, ...] = ()
    dst_yards_allowed_tiers: tuple[AllowedTier, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _validate_tiers(self.dst_points_allowed_tiers, "points-allowed")
        _validate_tiers(self.dst_yards_allowed_tiers, "yards-allowed")
        tiered = {
            StatKey.DST_POINTS_ALLOWED: self.dst_points_allowed_tiers,
            StatKey.DST_YARDS_ALLOWED: self.dst_yards_allowed_tiers,
        }
        linear_tables = [self.per_stat, *self.position_overrides.values()]
        for stat, tiers in tiered.items():
            if tiers and any(stat in table for table in linear_tables):
                raise ValueError(f"{stat} cannot be both tiered and linearly scored")
        return self

    def rate(self, stat: StatKey, position: Position) -> Decimal:
        override = self.position_overrides.get(position)
        if override is not None and stat in override:
            return override[stat]
        return self.per_stat.get(stat, Decimal(0))


# --------------------------------------------------------------------------- rosters


class SlotKind(StrEnum):
    STARTER = "starter"
    BENCH = "bench"
    RESERVE = "reserve"  # IR / injured reserve
    TAXI = "taxi"


class RosterSlot(DomainModel):
    """A slot definition, e.g. FLEX = {RB, WR, TE} x1, SUPERFLEX = {QB, RB, WR, TE} x1.

    ``eligible_positions`` is empty for BENCH (any player may occupy it).
    """

    label: str = Field(min_length=1, max_length=32)
    kind: SlotKind
    count: int = Field(ge=1)
    eligible_positions: FrozenSet[Position] = frozenset()

    @model_validator(mode="after")
    def _starter_needs_positions(self) -> Self:
        if self.kind is SlotKind.STARTER and not self.eligible_positions:
            raise ValueError(f"starter slot {self.label} needs eligible_positions")
        return self


class RosterRules(DomainModel):
    slots: tuple[RosterSlot, ...] = Field(min_length=1)
    max_per_position: FrozenMapping[Position, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique_labels(self) -> Self:
        labels = [s.label for s in self.slots]
        if len(labels) != len(set(labels)):
            raise ValueError("slot labels must be unique")
        if not any(s.kind is SlotKind.STARTER for s in self.slots):
            raise ValueError("roster must have at least one starter slot")
        return self

    def slot(self, label: str) -> RosterSlot:
        for s in self.slots:
            if s.label == label:
                return s
        raise KeyError(label)

    @property
    def starter_count(self) -> int:
        return sum(s.count for s in self.slots if s.kind is SlotKind.STARTER)

    @property
    def active_roster_size(self) -> int:
        """Starters + bench (reserve/taxi excluded)."""
        return sum(s.count for s in self.slots if s.kind in (SlotKind.STARTER, SlotKind.BENCH))


# --------------------------------------------------------------------------- league settings


class DraftType(StrEnum):
    SNAKE = "snake"
    LINEAR = "linear"
    AUCTION = "auction"


class DraftSettings(DomainModel):
    draft_type: DraftType
    rounds: int = Field(ge=1)
    third_round_reversal: bool = False
    auction_budget: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _auction_budget(self) -> Self:
        if (self.draft_type is DraftType.AUCTION) != (self.auction_budget is not None):
            raise ValueError("auction_budget is required for, and only for, auction drafts")
        if self.third_round_reversal and self.draft_type is not DraftType.SNAKE:
            raise ValueError("third_round_reversal only applies to snake drafts")
        return self


class WaiverType(StrEnum):
    FAAB = "faab"
    ROLLING = "rolling"
    REVERSE_STANDINGS = "reverse_standings"
    NONE = "none"  # pure free agency


class WaiverSettings(DomainModel):
    waiver_type: WaiverType
    faab_budget: int | None = Field(default=None, ge=0)
    allow_zero_dollar_bids: bool = True

    @model_validator(mode="after")
    def _budget(self) -> Self:
        if (self.waiver_type is WaiverType.FAAB) != (self.faab_budget is not None):
            raise ValueError("faab_budget is required for, and only for, FAAB leagues")
        return self


class LineupLock(StrEnum):
    PER_GAME = "per_game"
    WEEKLY_FIRST_GAME = "weekly_first_game"


class PlayoffSettings(DomainModel):
    playoff_teams: int = Field(ge=0)
    first_week: int = Field(ge=1, le=25)
    last_week: int = Field(ge=1, le=25)
    reseed: bool = False

    @model_validator(mode="after")
    def _weeks(self) -> Self:
        if self.last_week < self.first_week:
            raise ValueError("last_week must be >= first_week")
        return self


class League(DomainModel):
    """Stable league identity only. Everything a commissioner or the platform can change during
    a season (scoring, rosters, waivers, schedule/playoffs, draft, eligibility) is a temporal
    observation, reconstructed as of a cutoff by ``fantasy_gm.leagues.state``."""

    league_id: LeagueId
    name: str
    season: int
    num_teams: int = Field(ge=2)
    managed_team_id: FantasyTeamId | None = None


class SeasonSettings(DomainModel):
    playoffs: PlayoffSettings
    lineup_lock: LineupLock
    trade_deadline_week: int | None = Field(default=None, ge=1, le=25)


class LeagueRules(DomainModel):
    """The complete rule set in force at one point in time (derived, never stored as truth)."""

    scoring: ScoringRules
    roster_rules: RosterRules
    draft: DraftSettings
    waivers: WaiverSettings
    season_settings: SeasonSettings


# --------------------------------------------------------------------------- temporal league facts


class _LeagueObservation(Observation):
    league_id: LeagueId

    def subject_league_id(self) -> LeagueId:
        return self.league_id


class LeagueScoringSettings(_LeagueObservation):
    kind: ClassVar[str] = "league_scoring_settings"
    scoring: ScoringRules

    def fact_key(self) -> str:
        return f"league_scoring:{self.league_id}:{self.effective_at.isoformat()}"


class LeagueRosterSettings(_LeagueObservation):
    kind: ClassVar[str] = "league_roster_settings"
    roster_rules: RosterRules

    def fact_key(self) -> str:
        return f"league_roster:{self.league_id}:{self.effective_at.isoformat()}"


class LeagueWaiverSettings(_LeagueObservation):
    kind: ClassVar[str] = "league_waiver_settings"
    waivers: WaiverSettings

    def fact_key(self) -> str:
        return f"league_waivers:{self.league_id}:{self.effective_at.isoformat()}"


class LeagueSeasonSettings(_LeagueObservation):
    kind: ClassVar[str] = "league_season_settings"
    settings: SeasonSettings

    def fact_key(self) -> str:
        return f"league_season:{self.league_id}:{self.effective_at.isoformat()}"


class LeagueDraftSettings(_LeagueObservation):
    kind: ClassVar[str] = "league_draft_settings"
    draft: DraftSettings

    def fact_key(self) -> str:
        return f"league_draft:{self.league_id}:{self.effective_at.isoformat()}"


class WaiverBudgetState(_LeagueObservation):
    kind: ClassVar[str] = "waiver_budget_state"
    team_id: FantasyTeamId
    faab_remaining: int | None = Field(default=None, ge=0)
    waiver_priority: int | None = Field(default=None, ge=1)

    def fact_key(self) -> str:
        return f"waiver_state:{self.league_id}:{self.team_id}:{self.effective_at.isoformat()}"


class PlatformEligibility(_LeagueObservation):
    """Positions the league's platform lets this player fill (may differ from NFL position)."""

    kind: ClassVar[str] = "platform_eligibility"
    player_id: PlayerId
    positions: FrozenSet[Position] = Field(min_length=1)

    def fact_key(self) -> str:
        return f"eligibility:{self.league_id}:{self.player_id}:{self.effective_at.isoformat()}"

    def subject_player_id(self) -> PlayerId:
        return self.player_id


class FantasyTeam(DomainModel):
    team_id: FantasyTeamId
    league_id: LeagueId
    name: str


class RosterEntry(DomainModel):
    player_id: PlayerId
    slot_label: str


class FantasyRoster(_LeagueObservation):
    """A fantasy team's roster + lineup as observed from the league platform."""

    kind: ClassVar[str] = "fantasy_roster"
    team_id: FantasyTeamId
    week: int | None = Field(default=None, ge=0, le=25)
    entries: tuple[RosterEntry, ...]

    def fact_key(self) -> str:
        return f"fantasy_roster:{self.league_id}:{self.team_id}:{self.effective_at.isoformat()}"

    def player_ids(self) -> frozenset[PlayerId]:
        return frozenset(e.player_id for e in self.entries)


class DraftPick(DomainModel):
    league_id: LeagueId
    season: int
    round: int = Field(ge=1)
    pick_in_round: int = Field(ge=1)
    overall: int = Field(ge=1)
    original_team_id: FantasyTeamId
    owner_team_id: FantasyTeamId  # differs from original when the pick was traded
    selected_player_id: PlayerId | None = None
    selected_at: UtcDatetime | None = None
    is_keeper: bool = False

    @model_validator(mode="after")
    def _selection_consistency(self) -> Self:
        if (self.selected_player_id is None) != (self.selected_at is None):
            raise ValueError("selected_player_id and selected_at must be set together")
        return self


class DraftBoardState(_LeagueObservation):
    """Full draft order/board (made and pending picks) as of ``effective_at``."""

    kind: ClassVar[str] = "draft_board_state"
    picks: tuple[DraftPick, ...]

    def fact_key(self) -> str:
        return f"draft_board:{self.league_id}:{self.effective_at.isoformat()}"
