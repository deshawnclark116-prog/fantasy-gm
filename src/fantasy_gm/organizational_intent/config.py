"""Every tunable in Organizational Intent v0, in one versioned, hashable object.

ALL VALUES BELOW ARE UNVALIDATED HEURISTIC PLACEHOLDERS. They exist to make behaviour explicit
and reproducible, not because they are known to be right. Each must earn its place (or be
replaced) through forward validation. The config hash is stored on every snapshot.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.nfl import DepthChartSource, Position


def _primary_roles() -> dict[Position, int]:
    return {Position.QB: 1, Position.RB: 1, Position.WR: 3, Position.TE: 1}


def _depth_reliability() -> dict[DepthChartSource, float]:
    # Official team depth charts are widely known to be stale/unreliable, hence lowest.
    return {
        DepthChartSource.OFFICIAL_TEAM: 0.4,
        DepthChartSource.BEAT_REPORT: 0.5,
        DepthChartSource.PROVIDER_CURATED: 0.6,
    }


def _full_role_reference() -> dict[Position, float]:
    # Usage share at which a player is treated as holding a full organizational role.
    # QB: dropback share; RB: (carries+targets) share; WR/TE: route participation.
    return {Position.QB: 0.9, Position.RB: 0.55, Position.WR: 0.85, Position.TE: 0.75}


class IntentConfigV0(DomainModel):
    version: Literal["org_intent_v0"] = "org_intent_v0"

    # Draft investment
    draft_max_overall_pick: int = Field(default=262, ge=2)
    draft_confidence_half_life_years: float = Field(default=2.0, gt=0)
    draft_other_team_factor: float = Field(default=0.5, ge=0, le=1)

    # Contract investment
    contract_confidence: float = Field(default=0.7, ge=0, le=1)

    # Roster competition
    competition_primary_roles: dict[Position, int] = Field(default_factory=_primary_roles)
    competition_base_confidence: float = Field(default=0.6, ge=0, le=1)

    # Recent roster transactions
    transaction_lookback_days: int = Field(default=180, ge=1)
    transaction_confidence: float = Field(default=0.5, ge=0, le=1)

    # Depth chart
    depth_source_reliability: dict[DepthChartSource, float] = Field(
        default_factory=_depth_reliability
    )
    depth_staleness_half_life_days: float = Field(default=21.0, gt=0)

    # Coaching / scheme continuity
    coaching_lookback_days: int = Field(default=365, ge=1)
    regime_change_discount: float = Field(default=0.5, ge=0, le=1)

    # Preseason deployment
    preseason_first_team_confidence: float = Field(default=0.6, ge=0, le=1)
    preseason_full_confidence_games: int = Field(default=3, ge=1)

    # Actual NFL usage
    usage_max_games: int = Field(default=17, ge=1)
    usage_recency_half_life_games: float = Field(default=4.0, gt=0)
    usage_prior_season_discount: float = Field(default=0.5, ge=0, le=1)
    usage_fallback_quality: float = Field(default=0.6, ge=0, le=1)
    usage_full_role_reference: dict[Position, float] = Field(default_factory=_full_role_reference)

    # Prior vs evidence balance: usage_weight = n_eff / (n_eff + prior_pseudo_games)
    prior_pseudo_games: float = Field(default=4.0, gt=0)

    # Conflict detection
    conflict_threshold: float = Field(default=0.35, gt=0, le=1)
    conflict_min_effective_games: float = Field(default=2.0, ge=0)
