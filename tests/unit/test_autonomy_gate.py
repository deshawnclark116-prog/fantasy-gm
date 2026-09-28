from __future__ import annotations

from datetime import timedelta

from fantasy_gm.decisions.autonomy import Routing, authorize_execution, route_decision
from fantasy_gm.decisions.ledger import InMemoryDecisionLedger
from fantasy_gm.domain.autonomy import AutonomyMode, AutonomyPolicy, DecisionType
from fantasy_gm.domain.capabilities import ProviderCapability, is_read_only
from fantasy_gm.domain.clock import ManualClock
from fantasy_gm.domain.decision import ActorKind, DecisionStatus
from fantasy_gm.domain.validation import ModelValidationRecord, ValidationState
from fantasy_gm.models.registry import InMemoryModelRegistry
from tests.factories import (
    heuristic_artifact,
    lineup_decision,
    live_run,
    make_league,
    memory_store,
    open_session,
    status_event,
    ts,
)
from tests.fakes import WRITE_CAPS


def _setup(clock: ManualClock, validated: bool = True):  # type: ignore[no-untyped-def]
    league = make_league()
    models = InMemoryModelRegistry(clock)
    art = heuristic_artifact("engine")
    models.register_artifact(art)
    if validated:
        models.record_validation(
            ModelValidationRecord(
                artifact_hash=art.artifact_hash,
                decision_type=DecisionType.LINEUP,
                regime="regular_season",
                state=ValidationState.VALIDATED,
                effective_at=clock.now(),
                approved_by="t",
            )
        )
    d = lineup_decision(
        open_session(memory_store(), clock, clock.now(), run=live_run()),
        league_id=league.league_id,
        mode=AutonomyMode.AUTONOMOUS,
        artifact=art,
    )
    policy = AutonomyPolicy(league_id=league.league_id, default_mode=AutonomyMode.AUTONOMOUS)
    return league, models, d, policy


def test_read_only_helper() -> None:
    assert is_read_only(frozenset({ProviderCapability.READ}))
    assert not is_read_only(WRITE_CAPS)


def test_policy_mode_is_most_restrictive(clock: ManualClock) -> None:
    _, models, d, policy = _setup(clock)
    observe = policy.model_copy(
        update={"mode_overrides": {DecisionType.LINEUP: AutonomyMode.OBSERVE}}
    )
    r = route_decision(d, observe, WRITE_CAPS, models, clock.now())
    assert r.routing is Routing.RECORD_ONLY and r.initial_status is DecisionStatus.RECORDED


def test_replay_decisions_are_record_only(clock: ManualClock) -> None:
    league, models, _, policy = _setup(clock)
    replay = lineup_decision(
        open_session(memory_store(), clock, ts(days=6)),
        league_id=league.league_id,
        mode=AutonomyMode.AUTONOMOUS,
    )
    assert route_decision(replay, policy, WRITE_CAPS, models, clock.now()).routing is (
        Routing.RECORD_ONLY
    )


def test_insufficient_evidence_blocks_autonomy(clock: ManualClock) -> None:
    _, models, d, policy = _setup(clock)
    flagged = d.model_copy(
        update={"confidence": d.confidence.model_copy(update={"insufficient_evidence": True})}
    )
    assert route_decision(flagged, policy, WRITE_CAPS, models, clock.now()).routing is (
        Routing.REQUEST_APPROVAL
    )


def test_kill_switch_staleness_and_policy_downgrade(clock: ManualClock) -> None:
    _, models, d, policy = _setup(clock)
    ledger = InMemoryDecisionLedger(clock)
    ledger.record(d, status_event(d, DecisionStatus.APPROVED, actor=ActorKind.POLICY))
    entry = ledger.get(d.decision_id)
    now = clock.now()
    off = authorize_execution(entry, policy, WRITE_CAPS, models, now, execution_enabled=False)
    assert not off.allowed and any("kill switch" in r for r in off.reasons)
    stale = authorize_execution(
        entry, policy, WRITE_CAPS, models, now + timedelta(hours=1), execution_enabled=True
    )
    assert not stale.allowed and any("stale" in r for r in stale.reasons)
    assert authorize_execution(
        entry, policy, WRITE_CAPS, models, now, execution_enabled=True
    ).allowed
    downgraded = policy.model_copy(update={"default_mode": AutonomyMode.RECOMMEND})
    assert not authorize_execution(
        entry, downgraded, WRITE_CAPS, models, now, execution_enabled=True
    ).allowed


def test_no_action_is_never_executed(clock: ManualClock) -> None:
    _, models, d, policy = _setup(clock)
    hold = next(c for c in d.candidates if c.action.action_type == "no_action")
    d2 = d.model_copy(update={"selected_candidate_id": hold.candidate_id})
    r = route_decision(d2, policy, WRITE_CAPS, models, clock.now())
    assert r.routing is Routing.NOTIFY and not r.manual_action_required
