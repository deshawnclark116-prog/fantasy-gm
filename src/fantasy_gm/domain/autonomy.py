"""Autonomy policy (what the system is *allowed* to do on the user's behalf)."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum

from pydantic import Field

from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.ids import LeagueId


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


class AutonomyPolicy(DomainModel):
    league_id: LeagueId
    default_mode: AutonomyMode = AutonomyMode.RECOMMEND
    mode_overrides: dict[DecisionType, AutonomyMode] = Field(default_factory=dict)
    # Autonomous execution additionally requires this much decision confidence ...
    min_autonomous_confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    # ... and the decision's information cutoff must be at most this old at execution time.
    max_decision_staleness: timedelta = timedelta(hours=6)
    # Hard ceiling on a single autonomous FAAB bid as a fraction of the league budget.
    max_autonomous_faab_fraction: float = Field(default=0.25, ge=0.0, le=1.0)

    def mode_for(self, decision_type: DecisionType) -> AutonomyMode:
        return self.mode_overrides.get(decision_type, self.default_mode)
