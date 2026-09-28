"""Autonomy gate: decides how a decision is routed and whether it may be executed.

Pure functions. Two checkpoints:

* ``route_decision`` -- at record time: record only / notify / request approval / execute.
* ``authorize_execution`` -- immediately before calling a provider. Re-checks everything,
  because policy, capabilities and time may all have changed since the decision was recorded.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from fantasy_gm.decisions.ledger import LedgerEntry
from fantasy_gm.domain.actions import WaiverClaim
from fantasy_gm.domain.autonomy import AutonomyMode, AutonomyPolicy
from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.capabilities import ProviderCapability
from fantasy_gm.domain.decision import ActorKind, Decision, DecisionStatus
from fantasy_gm.domain.league import League

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
    decision: Decision, policy: AutonomyPolicy, league: League | None
) -> list[str]:
    reasons: list[str] = []
    if decision.confidence.insufficient_evidence:
        reasons.append("decision flagged insufficient evidence")
    if decision.confidence.score < policy.min_autonomous_confidence:
        reasons.append(
            f"confidence {decision.confidence.score:.2f} below autonomous threshold "
            f"{policy.min_autonomous_confidence:.2f}"
        )
    action = decision.selected.action
    if isinstance(action, WaiverClaim) and action.faab_bid:
        budget = league.waivers.faab_budget if league else None
        if budget is None:
            reasons.append("FAAB bid without a known league FAAB budget")
        elif action.faab_bid > policy.max_autonomous_faab_fraction * budget:
            reasons.append(
                f"FAAB bid {action.faab_bid} exceeds autonomous ceiling "
                f"{policy.max_autonomous_faab_fraction:.0%} of {budget}"
            )
    return reasons


def route_decision(
    decision: Decision,
    policy: AutonomyPolicy,
    capabilities: frozenset[ProviderCapability],
    league: League | None = None,
) -> RoutingDecision:
    if policy.league_id != decision.league_id:
        raise ValueError("policy belongs to a different league")
    mode = most_restrictive(decision.autonomy_mode, policy.mode_for(decision.decision_type))
    can_execute, cap_reasons = _capability_gap(decision, capabilities)
    is_action = decision.selected.action.required_capability is not None

    if mode is AutonomyMode.OBSERVE:
        return RoutingDecision(
            routing=Routing.RECORD_ONLY,
            initial_status=DecisionStatus.RECORDED,
            effective_mode=mode,
            provider_can_execute=can_execute,
            manual_action_required=False,
            reasons=("observe mode",),
        )
    notify = RoutingDecision(
        routing=Routing.NOTIFY,
        initial_status=DecisionStatus.RECOMMENDED,
        effective_mode=mode,
        provider_can_execute=can_execute,
        manual_action_required=is_action and not can_execute,
        reasons=tuple(cap_reasons),
    )
    if mode is AutonomyMode.RECOMMEND or not can_execute:
        return notify
    if mode is AutonomyMode.APPROVAL_REQUIRED:
        return RoutingDecision(
            routing=Routing.REQUEST_APPROVAL,
            initial_status=DecisionStatus.AWAITING_APPROVAL,
            effective_mode=mode,
            provider_can_execute=True,
            manual_action_required=False,
        )
    blockers = _autonomy_blockers(decision, policy, league)
    if blockers:
        return RoutingDecision(
            routing=Routing.REQUEST_APPROVAL,
            initial_status=DecisionStatus.AWAITING_APPROVAL,
            effective_mode=mode,
            provider_can_execute=True,
            manual_action_required=False,
            reasons=("autonomous execution downgraded to approval", *blockers),
        )
    return RoutingDecision(
        routing=Routing.EXECUTE,
        initial_status=DecisionStatus.APPROVED,
        effective_mode=mode,
        provider_can_execute=True,
        manual_action_required=False,
    )


def authorize_execution(
    entry: LedgerEntry,
    policy: AutonomyPolicy,
    capabilities: frozenset[ProviderCapability],
    now: datetime,
    *,
    execution_enabled: bool,
    league: League | None = None,
) -> ExecutionAuthorization:
    decision = entry.decision
    reasons: list[str] = []
    if not execution_enabled:
        reasons.append("global execution kill switch is off")
    if policy.league_id != decision.league_id:
        reasons.append("policy belongs to a different league")
    if entry.current_status not in (DecisionStatus.APPROVED, DecisionStatus.EXECUTION_FAILED):
        reasons.append(f"decision status is {entry.current_status}, not approved")
    mode = most_restrictive(decision.autonomy_mode, policy.mode_for(decision.decision_type))
    if mode in (AutonomyMode.OBSERVE, AutonomyMode.RECOMMEND):
        reasons.append(f"effective autonomy mode {mode} never executes")
    _, cap_reasons = _capability_gap(decision, capabilities)
    reasons.extend(cap_reasons)
    if now - decision.information_cutoff > policy.max_decision_staleness:
        reasons.append(
            f"decision information is stale ({now - decision.information_cutoff} old, "
            f"max {policy.max_decision_staleness})"
        )
    approvals = [e for e in entry.status_history if e.status is DecisionStatus.APPROVED]
    if approvals and approvals[-1].actor.kind is ActorKind.POLICY:
        if mode is not AutonomyMode.AUTONOMOUS:
            reasons.append("policy approval is no longer valid: league is not autonomous")
        reasons.extend(_autonomy_blockers(decision, policy, league))
    return ExecutionAuthorization(allowed=not reasons, reasons=tuple(reasons))
