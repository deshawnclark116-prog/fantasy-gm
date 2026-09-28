"""Autonomy gate: routing at record time, authorisation immediately before execution.

Autonomous execution fails closed. It requires ALL of:
* a LIVE run (PAPER and REPLAY never execute);
* the decision artifact (and its calibrator) VALIDATED for this decision type and regime --
  descriptive confidence never authorises anything;
* an action risk class at or below the policy's autonomous ceiling;
* no insufficient-evidence flag; fresh enough information for this decision type and context;
* the provider capability for the action; the global kill switch on.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from fantasy_gm.decisions.ledger import EXECUTABLE_STATUSES, LedgerEntry
from fantasy_gm.domain.autonomy import AutonomyMode, AutonomyPolicy, FreshnessContext
from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.capabilities import ProviderCapability
from fantasy_gm.domain.decision import ActorKind, Decision, DecisionStatus
from fantasy_gm.domain.risk import RiskAssessment, assess_action_risk
from fantasy_gm.domain.run_context import RunMode
from fantasy_gm.domain.validation import ValidationState
from fantasy_gm.models.registry import ModelRegistry, effective_validation_state

_MODE_ORDER = [
    AutonomyMode.OBSERVE,
    AutonomyMode.RECOMMEND,
    AutonomyMode.APPROVAL_REQUIRED,
    AutonomyMode.AUTONOMOUS,
]


def most_restrictive(*modes: AutonomyMode) -> AutonomyMode:
    return min(modes, key=_MODE_ORDER.index)


class Routing(StrEnum):
    RECORD_ONLY = "record_only"
    NOTIFY = "notify"
    REQUEST_APPROVAL = "request_approval"
    EXECUTE = "execute"


class RoutingDecision(DomainModel):
    routing: Routing
    initial_status: DecisionStatus
    effective_mode: AutonomyMode
    provider_can_execute: bool
    manual_action_required: bool
    risk: RiskAssessment
    validation_state: ValidationState
    reasons: tuple[str, ...] = ()


class ExecutionAuthorization(DomainModel):
    allowed: bool
    reasons: tuple[str, ...] = ()


def _capability_gap(
    decision: Decision, capabilities: frozenset[ProviderCapability]
) -> tuple[bool, list[str]]:
    required = decision.selected.action.required_capability
    if required is None:
        return False, ["selected action is NoAction; nothing to execute"]
    if required not in capabilities:
        return False, [f"provider lacks required capability {required}"]
    return True, []


def _autonomy_blockers(
    decision: Decision,
    policy: AutonomyPolicy,
    faab_budget: int | None,
    models: ModelRegistry,
    now: datetime,
) -> tuple[list[str], RiskAssessment, ValidationState]:
    reasons: list[str] = []
    state, v_reasons = effective_validation_state(
        models, decision.decision_artifact_hash, decision.decision_type, decision.regime, now
    )
    if state is not ValidationState.VALIDATED:
        reasons.extend(v_reasons or [f"decision model is {state}"])
    risk = assess_action_risk(decision.selected.action, policy.risk, faab_budget)
    if risk.risk_class.rank > policy.risk.max_autonomous_risk.rank:
        reasons.append(
            f"action risk {risk.risk_class} exceeds autonomous ceiling "
            f"{policy.risk.max_autonomous_risk}"
        )
    if decision.confidence.insufficient_evidence:
        reasons.append("decision flagged insufficient evidence")
    return reasons, risk, state


def route_decision(
    decision: Decision,
    policy: AutonomyPolicy,
    capabilities: frozenset[ProviderCapability],
    models: ModelRegistry,
    now: datetime,
    faab_budget: int | None = None,
) -> RoutingDecision:
    if policy.league_id != decision.league_id:
        raise ValueError("policy belongs to a different league")
    mode = most_restrictive(decision.autonomy_mode, policy.mode_for(decision.decision_type))
    can_execute, cap_reasons = _capability_gap(decision, capabilities)
    is_action = decision.selected.action.required_capability is not None
    blockers, risk, state = _autonomy_blockers(decision, policy, faab_budget, models, now)

    def result(
        routing: Routing, status: DecisionStatus, manual: bool, reasons: list[str]
    ) -> RoutingDecision:
        return RoutingDecision(
            routing=routing,
            initial_status=status,
            effective_mode=mode,
            provider_can_execute=can_execute,
            manual_action_required=manual,
            risk=risk,
            validation_state=state,
            reasons=tuple(reasons),
        )

    if decision.run.mode is not RunMode.LIVE:
        return result(
            Routing.RECORD_ONLY,
            DecisionStatus.RECORDED,
            False,
            [f"{decision.run.mode} run: record only"],
        )
    if mode is AutonomyMode.OBSERVE:
        return result(Routing.RECORD_ONLY, DecisionStatus.RECORDED, False, ["observe mode"])
    if mode is AutonomyMode.RECOMMEND or not can_execute:
        return result(
            Routing.NOTIFY, DecisionStatus.RECOMMENDED, is_action and not can_execute, cap_reasons
        )
    if mode is AutonomyMode.APPROVAL_REQUIRED:
        return result(Routing.REQUEST_APPROVAL, DecisionStatus.AWAITING_APPROVAL, False, [])
    if blockers:
        return result(
            Routing.REQUEST_APPROVAL,
            DecisionStatus.AWAITING_APPROVAL,
            False,
            ["autonomous execution downgraded to approval", *blockers],
        )
    return result(Routing.EXECUTE, DecisionStatus.APPROVED, False, [])


def authorize_execution(
    entry: LedgerEntry,
    policy: AutonomyPolicy,
    capabilities: frozenset[ProviderCapability],
    models: ModelRegistry,
    now: datetime,
    *,
    execution_enabled: bool,
    faab_budget: int | None = None,
    freshness: FreshnessContext | None = None,
) -> ExecutionAuthorization:
    decision = entry.decision.value
    reasons: list[str] = []
    if not execution_enabled:
        reasons.append("global execution kill switch is off")
    if decision.run.mode is not RunMode.LIVE:
        reasons.append(f"{decision.run.mode} decisions are never executed")
    if policy.league_id != decision.league_id:
        reasons.append("policy belongs to a different league")
    if entry.current_status not in EXECUTABLE_STATUSES:
        reasons.append(f"decision status is {entry.current_status}, not executable")
    mode = most_restrictive(decision.autonomy_mode, policy.mode_for(decision.decision_type))
    if mode in (AutonomyMode.OBSERVE, AutonomyMode.RECOMMEND):
        reasons.append(f"effective autonomy mode {mode} never executes")
    _, cap_reasons = _capability_gap(decision, capabilities)
    reasons.extend(cap_reasons)
    limit = policy.freshness.limit(decision.decision_type, freshness or FreshnessContext())
    age = now - decision.information_cutoff
    if age > limit:
        reasons.append(
            f"decision information is stale for {decision.decision_type} ({age} old, max {limit})"
        )
    approvals = [s for s in entry.statuses if s.status is DecisionStatus.APPROVED]
    if approvals and approvals[-1].actor.kind is ActorKind.POLICY:
        if mode is not AutonomyMode.AUTONOMOUS:
            reasons.append("policy approval is no longer valid: league is not autonomous")
        blockers, _, _ = _autonomy_blockers(decision, policy, faab_budget, models, now)
        reasons.extend(blockers)
    return ExecutionAuthorization(allowed=not reasons, reasons=tuple(reasons))
