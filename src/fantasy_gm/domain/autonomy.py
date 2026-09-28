"""Autonomy policy: what the system is *allowed* to do on the user's behalf."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum

from pydantic import Field

from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.ids import LeagueId
from fantasy_gm.domain.risk import RiskPolicy


class AutonomyMode(StrEnum):
    OBSERVE = "observe"  # record decisions only; never notify or act
    RECOMMEND = "recommend"  # notify the manager with an actionable recommendation
    APPROVAL_REQUIRED = "approval_required"  # execute only after explicit human approval
    AUTONOMOUS = "autonomous"  # execute without approval when every gate passes


class DecisionType(StrEnum):
    DRAFT_PICK = "draft_pick"
    LINEUP = "lineup"
    WAIVER_CLAIM = "waiver_claim"
    FREE_AGENT_ADD = "free_agent_add"
    DROP = "drop"
    TRADE_PROPOSAL = "trade_proposal"
    TRADE_RESPONSE = "trade_response"


def _default_max_age() -> dict[DecisionType, timedelta]:
    return {
        DecisionType.DRAFT_PICK: timedelta(seconds=90),
        DecisionType.LINEUP: timedelta(minutes=30),
        DecisionType.FREE_AGENT_ADD: timedelta(minutes=30),
        DecisionType.DROP: timedelta(minutes=30),
        DecisionType.WAIVER_CLAIM: timedelta(hours=6),
        DecisionType.TRADE_RESPONSE: timedelta(hours=12),
        DecisionType.TRADE_PROPOSAL: timedelta(hours=48),
    }


class FreshnessContext(DomainModel):
    """Situational facts that tighten freshness requirements at execution time."""

    live_draft_on_clock: bool = False
    minutes_to_lineup_lock: float | None = None


class FreshnessPolicy(DomainModel):
    """Maximum age of a decision's information (now - information_cutoff) at execution."""

    max_age: dict[DecisionType, timedelta] = Field(default_factory=_default_max_age)
    live_draft_max_age: timedelta = timedelta(seconds=30)
    near_lock_window_minutes: float = Field(default=90.0, ge=0)
    near_lock_max_age: timedelta = timedelta(minutes=5)

    def limit(self, decision_type: DecisionType, context: FreshnessContext) -> timedelta:
        limit = self.max_age.get(decision_type, timedelta(0))  # unknown type: nothing is fresh
        if decision_type is DecisionType.DRAFT_PICK and context.live_draft_on_clock:
            limit = min(limit, self.live_draft_max_age)
        near_lock = (
            context.minutes_to_lineup_lock is not None
            and context.minutes_to_lineup_lock <= self.near_lock_window_minutes
        )
        if near_lock and decision_type in (
            DecisionType.LINEUP,
            DecisionType.FREE_AGENT_ADD,
            DecisionType.DROP,
        ):
            limit = min(limit, self.near_lock_max_age)
        return limit


class AutonomyPolicy(DomainModel):
    league_id: LeagueId
    default_mode: AutonomyMode = AutonomyMode.RECOMMEND
    mode_overrides: dict[DecisionType, AutonomyMode] = Field(default_factory=dict)
    freshness: FreshnessPolicy = Field(default_factory=FreshnessPolicy)
    risk: RiskPolicy = Field(default_factory=RiskPolicy)

    def mode_for(self, decision_type: DecisionType) -> AutonomyMode:
        return self.mode_overrides.get(decision_type, self.default_mode)
