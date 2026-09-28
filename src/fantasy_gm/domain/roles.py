"""Latent role dimensions.

A player's role is multidimensional. Deployment (what the organisation chooses: snaps, routes,
handoffs) and target earning (what the player wins: targets per route, first reads, air yards)
are distinct latent quantities and must never be collapsed into one "usage" number. Future
projection models consume these dimensions -- and organizational-intent components as priors on
specific dimensions -- rather than a single usage metric or an aggregate intent score.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.ids import ObservationId, PlayerId
from fantasy_gm.domain.nfl import Position, UsageMetric
from fantasy_gm.domain.time import UtcDatetime

M = UsageMetric


class RoleFamily(StrEnum):
    DEPLOYMENT = "deployment"  # organisation's choice to put the player on the field / in routes
    TARGET_EARNING = "target_earning"  # player's ability to command the ball when deployed
    RUSHING = "rushing"
    SITUATIONAL = "situational"  # red zone, goal line, third down, two-minute
    PASS_PROTECTION = "pass_protection"
    ENVIRONMENT = "environment"  # team/offense context the role lives in
    ALIGNMENT = "alignment"  # slot/wide/inline, personnel groupings


class RoleDimension(StrEnum):
    # QB
    QB_DROPBACK_SHARE = "qb_dropback_share"
    QB_DESIGNED_RUSH_SHARE = "qb_designed_rush_share"
    QB_SCRAMBLE_RATE = "qb_scramble_rate"
    QB_TEAM_DROPBACK_RATE = "qb_team_dropback_rate"
    # RB
    RB_SNAP_SHARE = "rb_snap_share"
    RB_CARRY_SHARE = "rb_carry_share"
    RB_ROUTE_PARTICIPATION = "rb_route_participation"
    RB_TARGET_SHARE = "rb_target_share"
    RB_GOAL_LINE_SHARE = "rb_goal_line_share"
    RB_RED_ZONE_SHARE = "rb_red_zone_share"
    RB_THIRD_DOWN_SHARE = "rb_third_down_share"
    RB_TWO_MINUTE_SHARE = "rb_two_minute_share"
    RB_PASS_BLOCK_RATE = "rb_pass_block_rate"
    # WR / TE
    REC_SNAP_SHARE = "rec_snap_share"
    REC_ROUTE_PARTICIPATION = "rec_route_participation"
    REC_TARGETS_PER_ROUTE = "rec_targets_per_route"
    REC_TARGET_SHARE = "rec_target_share"
    REC_FIRST_READ_SHARE = "rec_first_read_share"
    REC_AIR_YARD_SHARE = "rec_air_yard_share"
    REC_RED_ZONE_TARGET_SHARE = "rec_red_zone_target_share"
    REC_SLOT_RATE = "rec_slot_rate"
    REC_PERSONNEL_ROLE = "rec_personnel_role"


class RoleDimensionSpec(DomainModel):
    positions: frozenset[Position]
    family: RoleFamily
    numerator: tuple[UsageMetric, ...] = ()
    denominator: tuple[UsageMetric, ...] = ()
    # False = reserved dimension whose raw inputs are not modelled yet (no fake values).
    supported: bool = True


_QB = frozenset({Position.QB})
_RB = frozenset({Position.RB})
_REC = frozenset({Position.WR, Position.TE})


def _s(
    positions: frozenset[Position],
    family: RoleFamily,
    num: tuple[UsageMetric, ...] = (),
    den: tuple[UsageMetric, ...] = (),
    supported: bool = True,
) -> RoleDimensionSpec:
    return RoleDimensionSpec(
        positions=positions, family=family, numerator=num, denominator=den, supported=supported
    )


D = RoleDimension
F = RoleFamily
ROLE_DIMENSIONS: dict[RoleDimension, RoleDimensionSpec] = {
    D.QB_DROPBACK_SHARE: _s(_QB, F.DEPLOYMENT, (M.DROPBACKS,), (M.TEAM_DROPBACKS,)),
    D.QB_DESIGNED_RUSH_SHARE: _s(_QB, F.RUSHING, (M.DESIGNED_RUSHES,), (M.TEAM_CARRIES,)),
    D.QB_SCRAMBLE_RATE: _s(_QB, F.RUSHING, (M.SCRAMBLES,), (M.DROPBACKS,)),
    D.QB_TEAM_DROPBACK_RATE: _s(_QB, F.ENVIRONMENT, (M.TEAM_DROPBACKS,), (M.TEAM_OFFENSE_SNAPS,)),
    D.RB_SNAP_SHARE: _s(_RB, F.DEPLOYMENT, (M.OFFENSE_SNAPS,), (M.TEAM_OFFENSE_SNAPS,)),
    D.RB_CARRY_SHARE: _s(_RB, F.RUSHING, (M.CARRIES,), (M.TEAM_CARRIES,)),
    D.RB_ROUTE_PARTICIPATION: _s(_RB, F.DEPLOYMENT, (M.ROUTES,), (M.TEAM_DROPBACKS,)),
    D.RB_TARGET_SHARE: _s(_RB, F.TARGET_EARNING, (M.TARGETS,), (M.TEAM_TARGETS,)),
    D.RB_GOAL_LINE_SHARE: _s(
        _RB, F.SITUATIONAL, (M.GOAL_LINE_CARRIES,), (M.TEAM_GOAL_LINE_CARRIES,)
    ),
    D.RB_RED_ZONE_SHARE: _s(
        _RB,
        F.SITUATIONAL,
        (M.RED_ZONE_CARRIES, M.RED_ZONE_TARGETS),
        (M.TEAM_RED_ZONE_CARRIES, M.TEAM_RED_ZONE_TARGETS),
    ),
    D.RB_THIRD_DOWN_SHARE: _s(
        _RB, F.SITUATIONAL, (M.THIRD_DOWN_SNAPS,), (M.TEAM_THIRD_DOWN_SNAPS,)
    ),
    D.RB_TWO_MINUTE_SHARE: _s(
        _RB, F.SITUATIONAL, (M.TWO_MINUTE_SNAPS,), (M.TEAM_TWO_MINUTE_SNAPS,)
    ),
    D.RB_PASS_BLOCK_RATE: _s(
        _RB, F.PASS_PROTECTION, (M.PASS_BLOCK_SNAPS,), (M.PASS_BLOCK_SNAPS, M.ROUTES)
    ),
    D.REC_SNAP_SHARE: _s(_REC, F.DEPLOYMENT, (M.OFFENSE_SNAPS,), (M.TEAM_OFFENSE_SNAPS,)),
    D.REC_ROUTE_PARTICIPATION: _s(_REC, F.DEPLOYMENT, (M.ROUTES,), (M.TEAM_DROPBACKS,)),
    D.REC_TARGETS_PER_ROUTE: _s(_REC, F.TARGET_EARNING, (M.TARGETS,), (M.ROUTES,)),
    D.REC_TARGET_SHARE: _s(_REC, F.TARGET_EARNING, (M.TARGETS,), (M.TEAM_TARGETS,)),
    D.REC_FIRST_READ_SHARE: _s(
        _REC, F.TARGET_EARNING, (M.FIRST_READ_TARGETS,), (M.TEAM_FIRST_READ_TARGETS,)
    ),
    D.REC_AIR_YARD_SHARE: _s(_REC, F.TARGET_EARNING, (M.AIR_YARDS,), (M.TEAM_AIR_YARDS,)),
    D.REC_RED_ZONE_TARGET_SHARE: _s(
        _REC, F.SITUATIONAL, (M.RED_ZONE_TARGETS,), (M.TEAM_RED_ZONE_TARGETS,)
    ),
    D.REC_SLOT_RATE: _s(_REC, F.ALIGNMENT, supported=False),
    D.REC_PERSONNEL_ROLE: _s(_REC, F.ALIGNMENT, supported=False),
}


def dimensions_for(position: Position) -> tuple[RoleDimension, ...]:
    return tuple(d for d, spec in ROLE_DIMENSIONS.items() if position in spec.positions)


class RoleEstimateStatus(StrEnum):
    OBSERVED = "observed"
    UNOBSERVED = "unobserved"  # supported, but no games carried the needed metrics
    NOT_YET_SUPPORTED = "not_yet_supported"


class RoleEstimate(DomainModel):
    """An *observed* (empirical) estimate of one latent role dimension. Not a projection."""

    dimension: RoleDimension
    family: RoleFamily
    status: RoleEstimateStatus
    value: float | None = Field(default=None, ge=0.0)
    games: int = 0
    effective_games: float = 0.0
    pooled_numerator: float = 0.0
    pooled_denominator: float = 0.0
    method: str = "recency_weighted_pooled_ratio_v0"
    source_observation_ids: tuple[ObservationId, ...] = ()


class LatentRoleVector(DomainModel):
    player_id: PlayerId
    position: Position
    as_of: UtcDatetime
    estimates: dict[RoleDimension, RoleEstimate]

    def value(self, dimension: RoleDimension) -> float | None:
        est = self.estimates.get(dimension)
        return None if est is None else est.value

    def family(self, family: RoleFamily) -> dict[RoleDimension, RoleEstimate]:
        return {d: e for d, e in self.estimates.items() if e.family is family}
