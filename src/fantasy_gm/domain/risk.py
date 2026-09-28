"""Action risk classes: configurable policy, not hard-coded product behaviour."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from pydantic import Field

from fantasy_gm.domain.actions import (
    Action,
    DraftSelection,
    FreeAgentAddDrop,
    NoAction,
    ProposeTrade,
    RespondToTrade,
    SetLineup,
    WaiverClaim,
)
from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.frozen import FrozenSet
from fantasy_gm.domain.ids import PlayerId


class ActionRiskClass(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"  # irreversible or very high impact

    @property
    def rank(self) -> int:
        return _RANK[self]


_RANK: Mapping[ActionRiskClass, int] = MappingProxyType(
    {
        ActionRiskClass.LOW: 0,
        ActionRiskClass.MEDIUM: 1,
        ActionRiskClass.HIGH: 2,
        ActionRiskClass.CRITICAL: 3,
    }
)


def riskiest(*classes: ActionRiskClass) -> ActionRiskClass:
    return max(classes, key=lambda c: c.rank)


class RiskPolicy(DomainModel):
    """League/user-configurable risk classification. Defaults are deliberately conservative."""

    lineup_class: ActionRiskClass = ActionRiskClass.LOW
    add_only_class: ActionRiskClass = ActionRiskClass.LOW
    add_drop_class: ActionRiskClass = ActionRiskClass.MEDIUM
    waiver_base_class: ActionRiskClass = ActionRiskClass.MEDIUM
    faab_high_fraction: float = Field(default=0.20, ge=0, le=1)  # bid share of budget -> HIGH
    faab_critical_fraction: float = Field(default=0.40, ge=0, le=1)  # -> CRITICAL
    protected_player_ids: FrozenSet[PlayerId] = frozenset()
    protected_drop_class: ActionRiskClass = ActionRiskClass.HIGH
    trade_proposal_class: ActionRiskClass = ActionRiskClass.HIGH
    trade_accept_class: ActionRiskClass = ActionRiskClass.CRITICAL
    trade_decline_class: ActionRiskClass = ActionRiskClass.MEDIUM
    draft_selection_class: ActionRiskClass = ActionRiskClass.CRITICAL
    # Highest risk class a VALIDATED model may execute without human approval.
    max_autonomous_risk: ActionRiskClass = ActionRiskClass.MEDIUM


class RiskAssessment(DomainModel):
    risk_class: ActionRiskClass
    reasons: tuple[str, ...] = ()


def assess_action_risk(
    action: Action, policy: RiskPolicy, faab_budget: int | None
) -> RiskAssessment:
    reasons: list[str] = []
    drop: PlayerId | None = None
    if isinstance(action, NoAction):
        return RiskAssessment(risk_class=ActionRiskClass.LOW, reasons=("no action",))
    if isinstance(action, SetLineup):
        risk = policy.lineup_class
    elif isinstance(action, FreeAgentAddDrop):
        drop = action.drop_player_id
        risk = policy.add_only_class if drop is None else policy.add_drop_class
    elif isinstance(action, WaiverClaim):
        drop = action.drop_player_id
        risk = policy.waiver_base_class
        if action.faab_bid:
            if faab_budget is None or faab_budget <= 0:
                risk = ActionRiskClass.CRITICAL
                reasons.append("FAAB bid with unknown league budget")
            else:
                share = action.faab_bid / faab_budget
                if share > policy.faab_critical_fraction:
                    risk = riskiest(risk, ActionRiskClass.CRITICAL)
                elif share > policy.faab_high_fraction:
                    risk = riskiest(risk, ActionRiskClass.HIGH)
                reasons.append(f"FAAB bid is {share:.0%} of budget")
    elif isinstance(action, ProposeTrade):
        risk = policy.trade_proposal_class
    elif isinstance(action, RespondToTrade):
        risk = policy.trade_accept_class if action.accept else policy.trade_decline_class
    elif isinstance(action, DraftSelection):
        risk = policy.draft_selection_class
        reasons.append("draft selections are irreversible")
    else:  # pragma: no cover - exhaustive over Action
        risk = ActionRiskClass.CRITICAL
    if drop is not None and drop in policy.protected_player_ids:
        risk = riskiest(risk, policy.protected_drop_class)
        reasons.append(f"drops protected player {drop}")
    return RiskAssessment(risk_class=risk, reasons=tuple(reasons))
