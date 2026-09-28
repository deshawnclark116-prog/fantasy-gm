"""Canonical NFL entities and time-varying NFL observations."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from enum import StrEnum
from typing import ClassVar, Self

from pydantic import Field, field_validator, model_validator

from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.frozen import FrozenMapping, FrozenSet
from fantasy_gm.domain.ids import CoachId, GameId, NFLTeamId, PlayerId
from fantasy_gm.domain.observation import Observation


class Position(StrEnum):
    QB = "QB"
    RB = "RB"
    WR = "WR"
    TE = "TE"
    K = "K"
    DST = "DST"
    DL = "DL"
    LB = "LB"
    DB = "DB"


class SeasonPhase(StrEnum):
    PRESEASON = "preseason"
    REGULAR = "regular"
    POSTSEASON = "postseason"


# --------------------------------------------------------------------------- stable entities


class Player(DomainModel):
    """Stable identity for a real NFL player.

    ``positions`` is the canonical NFL position(s). Fantasy-platform eligibility can differ and
    is supplied per league (see ``fantasy_gm.leagues.roster_validation``).
    """

    player_id: PlayerId
    full_name: str = Field(min_length=1)
    positions: FrozenSet[Position] = Field(min_length=1)
    birth_date: date | None = None

    @property
    def primary_position(self) -> Position:
        order = list(Position)
        return min(self.positions, key=order.index)


class NFLTeam(DomainModel):
    team_id: NFLTeamId
    abbreviation: str = Field(min_length=2, max_length=4)
    full_name: str


# --------------------------------------------------------------------------- observations


class NFLGame(Observation):
    """A scheduled game; ``effective_at`` is kickoff.

    Modelled as an observation because kickoff times move (flex scheduling, relocations).
    """

    kind: ClassVar[str] = "nfl_game"
    game_id: GameId
    season: int = Field(ge=1920)
    week: int = Field(ge=0, le=25)
    phase: SeasonPhase
    home_team_id: NFLTeamId
    away_team_id: NFLTeamId

    def fact_key(self) -> str:
        return f"game:{self.game_id}"


class RosterStatus(StrEnum):
    ACTIVE = "active"
    PRACTICE_SQUAD = "practice_squad"
    RESERVE_INJURED = "reserve_injured"
    RESERVE_PUP = "reserve_pup"
    RESERVE_NFI = "reserve_nfi"
    SUSPENDED = "suspended"
    FREE_AGENT = "free_agent"
    RETIRED = "retired"


class PlayerTeamAssignment(Observation):
    """Player's team + roster status from ``effective_at`` until superseded."""

    kind: ClassVar[str] = "player_team_assignment"
    player_id: PlayerId
    team_id: NFLTeamId | None
    roster_status: RosterStatus

    @model_validator(mode="after")
    def _team_required_unless_unattached(self) -> Self:
        unattached = {RosterStatus.FREE_AGENT, RosterStatus.RETIRED}
        if (self.team_id is None) != (self.roster_status in unattached):
            raise ValueError("team_id must be None exactly when the player is unattached")
        return self

    def fact_key(self) -> str:
        return f"assignment:{self.player_id}:{self.effective_at.isoformat()}"

    def subject_player_id(self) -> PlayerId:
        return self.player_id

    def subject_team_id(self) -> NFLTeamId | None:
        return self.team_id


class DraftCapital(Observation):
    """NFL draft investment. ``overall_pick is None`` means undrafted (UDFA)."""

    kind: ClassVar[str] = "draft_capital"
    player_id: PlayerId
    draft_year: int = Field(ge=1936)
    round: int | None = Field(default=None, ge=1)
    overall_pick: int | None = Field(default=None, ge=1)
    drafting_team_id: NFLTeamId | None = None

    @model_validator(mode="after")
    def _drafted_consistency(self) -> Self:
        drafted = [self.round is not None, self.overall_pick is not None]
        if any(drafted) and not all(drafted):
            raise ValueError("round and overall_pick must both be set or both be None")
        if self.overall_pick is not None and self.drafting_team_id is None:
            raise ValueError("drafted players require drafting_team_id")
        return self

    @property
    def undrafted(self) -> bool:
        return self.overall_pick is None

    def fact_key(self) -> str:
        return f"draft:{self.player_id}"

    def subject_player_id(self) -> PlayerId:
        return self.player_id

    def subject_team_id(self) -> NFLTeamId | None:
        return self.drafting_team_id


class ContractKind(StrEnum):
    ROOKIE_SCALE = "rookie_scale"
    UDFA = "udfa"
    VETERAN = "veteran"
    EXTENSION = "extension"
    RESTRUCTURE = "restructure"
    FRANCHISE_TAG = "franchise_tag"
    TRANSITION_TAG = "transition_tag"
    MINIMUM = "minimum"


class ContractSignal(Observation):
    kind: ClassVar[str] = "contract_signal"
    player_id: PlayerId
    team_id: NFLTeamId
    contract_kind: ContractKind
    years: int | None = Field(default=None, ge=0)
    total_value_usd: int | None = Field(default=None, ge=0)
    guaranteed_usd: int | None = Field(default=None, ge=0)
    apy_usd: int | None = Field(default=None, ge=0)
    # Provider-supplied percentile of this contract's APY among active contracts at the same
    # position. We do not compute it ourselves in v0 (no market dataset yet).
    position_market_percentile: float | None = Field(default=None, ge=0.0, le=1.0)

    def fact_key(self) -> str:
        return f"contract:{self.player_id}:{self.team_id}:{self.effective_at.isoformat()}"

    def subject_player_id(self) -> PlayerId:
        return self.player_id

    def subject_team_id(self) -> NFLTeamId:
        return self.team_id


class DepthChartSource(StrEnum):
    OFFICIAL_TEAM = "official_team"
    PROVIDER_CURATED = "provider_curated"
    BEAT_REPORT = "beat_report"
    # Derived from usage. Excluded from the depth-chart intent component to avoid counting the
    # same usage evidence twice.
    INFERRED_FROM_USAGE = "inferred_from_usage"


class DepthChartSignal(Observation):
    kind: ClassVar[str] = "depth_chart_signal"
    player_id: PlayerId
    team_id: NFLTeamId
    position: Position
    depth_rank: int = Field(ge=1)
    source_type: DepthChartSource

    def fact_key(self) -> str:
        return (
            f"depth:{self.team_id}:{self.player_id}:{self.position}:"
            f"{self.source_type}:{self.effective_at.isoformat()}"
        )

    def subject_player_id(self) -> PlayerId:
        return self.player_id

    def subject_team_id(self) -> NFLTeamId:
        return self.team_id


class UsageMetric(StrEnum):
    """Extensible vocabulary of raw usage metrics.

    Adding a metric is additive (metrics are stored as a mapping). A metric *absent* from a
    snapshot means "not observed", never zero.
    """

    OFFENSE_SNAPS = "offense_snaps"
    TEAM_OFFENSE_SNAPS = "team_offense_snaps"
    FIRST_TEAM_SNAPS = "first_team_snaps"
    TEAM_FIRST_TEAM_SNAPS = "team_first_team_snaps"
    ROUTES = "routes"
    TEAM_DROPBACKS = "team_dropbacks"
    DROPBACKS = "dropbacks"
    TARGETS = "targets"
    TEAM_TARGETS = "team_targets"
    FIRST_READ_TARGETS = "first_read_targets"
    AIR_YARDS = "air_yards"
    TEAM_AIR_YARDS = "team_air_yards"
    CARRIES = "carries"
    TEAM_CARRIES = "team_carries"
    RED_ZONE_TARGETS = "red_zone_targets"
    RED_ZONE_CARRIES = "red_zone_carries"
    GOAL_LINE_CARRIES = "goal_line_carries"
    TEAM_GOAL_LINE_CARRIES = "team_goal_line_carries"
    THIRD_DOWN_SNAPS = "third_down_snaps"
    TWO_MINUTE_SNAPS = "two_minute_snaps"
    PASS_BLOCK_SNAPS = "pass_block_snaps"
    DESIGNED_RUSHES = "designed_rushes"
    SCRAMBLES = "scrambles"
    TEAM_FIRST_READ_TARGETS = "team_first_read_targets"
    TEAM_RED_ZONE_TARGETS = "team_red_zone_targets"
    TEAM_RED_ZONE_CARRIES = "team_red_zone_carries"
    TEAM_THIRD_DOWN_SNAPS = "team_third_down_snaps"
    TEAM_TWO_MINUTE_SNAPS = "team_two_minute_snaps"


class UsageSnapshot(Observation):
    """One player's raw usage in one game. Revisions (stat corrections) share the fact_key."""

    kind: ClassVar[str] = "usage_snapshot"
    player_id: PlayerId
    team_id: NFLTeamId
    game_id: GameId
    season: int
    week: int = Field(ge=0, le=25)
    phase: SeasonPhase
    # Deeply immutable: the outer mapping is frozen by construction (ADR 0019), so an engine
    # that reads this observation through a KnowledgeSession cannot mutate it after its
    # evidence hash has been sealed into the manifest.
    metrics: FrozenMapping[UsageMetric, float]

    @field_validator("metrics")
    @classmethod
    def _non_negative(cls, value: Mapping[UsageMetric, float]) -> Mapping[UsageMetric, float]:
        for metric, amount in value.items():
            if amount < 0:
                raise ValueError(f"usage metric {metric} cannot be negative")
        return value  # already frozen by the FrozenMapping annotation; returned unchanged

    def share(self, numerator: UsageMetric, denominator: UsageMetric) -> float | None:
        """Ratio of two observed metrics, or None if either is unobserved / denominator is 0."""
        num = self.metrics.get(numerator)
        den = self.metrics.get(denominator)
        if num is None or den is None or den <= 0:
            return None
        return min(num / den, 1.0)

    def fact_key(self) -> str:
        return f"usage:{self.player_id}:{self.game_id}"

    def subject_player_id(self) -> PlayerId:
        return self.player_id

    def subject_team_id(self) -> NFLTeamId:
        return self.team_id


class InjuryDesignation(StrEnum):
    NONE = "none"
    QUESTIONABLE = "questionable"
    DOUBTFUL = "doubtful"
    OUT = "out"


class PracticeParticipation(StrEnum):
    FULL = "full"
    LIMITED = "limited"
    DID_NOT_PARTICIPATE = "did_not_participate"


class InjuryReportType(StrEnum):
    OFFICIAL_REPORT = "official_report"
    NEWS = "news"


class InjuryStatus(Observation):
    kind: ClassVar[str] = "injury_status"
    player_id: PlayerId
    game_id: GameId | None = None
    designation: InjuryDesignation
    practice: PracticeParticipation | None = None
    body_part: str | None = None
    report_type: InjuryReportType

    def fact_key(self) -> str:
        return f"injury:{self.player_id}:{self.report_type}:{self.effective_at.isoformat()}"

    def subject_player_id(self) -> PlayerId:
        return self.player_id


class TeamTransactionKind(StrEnum):
    DRAFTED = "drafted"
    SIGNED = "signed"
    RE_SIGNED = "re_signed"
    EXTENDED = "extended"
    TRADED_FOR = "traded_for"
    CLAIMED_OFF_WAIVERS = "claimed_off_waivers"
    TRADED_AWAY = "traded_away"
    RELEASED = "released"
    PLACED_ON_RESERVE = "placed_on_reserve"
    ACTIVATED_FROM_RESERVE = "activated_from_reserve"


class RosterTransactionSignal(Observation):
    """An NFL team's roster move involving one player at one position."""

    kind: ClassVar[str] = "roster_transaction"
    team_id: NFLTeamId
    player_id: PlayerId
    position: Position
    transaction_kind: TeamTransactionKind

    def fact_key(self) -> str:
        return (
            f"txn:{self.team_id}:{self.player_id}:{self.transaction_kind}:"
            f"{self.effective_at.isoformat()}"
        )

    def subject_player_id(self) -> PlayerId:
        return self.player_id

    def subject_team_id(self) -> NFLTeamId:
        return self.team_id


class CoachingRole(StrEnum):
    HEAD_COACH = "head_coach"
    OFFENSIVE_COORDINATOR = "offensive_coordinator"
    OFFENSIVE_PLAY_CALLER = "offensive_play_caller"


class CoachingAssignment(Observation):
    """Who holds a coaching role from ``effective_at`` until superseded."""

    kind: ClassVar[str] = "coaching_assignment"
    team_id: NFLTeamId
    role: CoachingRole
    coach_id: CoachId
    coach_name: str

    def fact_key(self) -> str:
        return f"coach:{self.team_id}:{self.role}:{self.effective_at.isoformat()}"

    def subject_team_id(self) -> NFLTeamId:
        return self.team_id


class ContextSourceKind(StrEnum):
    OFFICIAL_PARTICIPATION = "official_participation"
    CHARTING_PROVIDER = "charting_provider"
    BEAT_REPORT = "beat_report"
    INFERRED = "inferred"


class PreseasonGameContext(Observation):
    """Team-level context for one preseason game, when genuinely observable.

    ``None`` fields mean "not known" -- never "false". Missing context lowers confidence in
    preseason evidence rather than being filled in.
    """

    kind: ClassVar[str] = "preseason_game_context"
    team_id: NFLTeamId
    game_id: GameId
    season: int
    starters_rested: bool | None = None
    first_team_offense_drives: int | None = Field(default=None, ge=0)
    source_kind: ContextSourceKind

    def fact_key(self) -> str:
        return f"preseason_ctx:{self.team_id}:{self.game_id}"

    def subject_team_id(self) -> NFLTeamId:
        return self.team_id
