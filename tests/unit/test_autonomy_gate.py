from __future__ import annotations

from datetime import timedelta

from fantasy_gm.decisions.autonomy import Routing, authorize_execution, route_decision
from fantasy_gm.decisions.ledger import InMemoryDecisionLedger
from fantasy_gm.domain.actions import WaiverClaim
from fantasy_gm.domain.autonomy import AutonomyMode, AutonomyPolicy, DecisionType
from fantasy_gm.domain.capabilities import ProviderCapability, is_read_only
from fantasy_gm.domain.decision import (
    ActorKind,
    ConfidenceAssessment,
    Decision,
    DecisionCandidate,
    DecisionStatus,
)
from fantasy_gm.domain.ids import new_player_id
from tests.factories import lineup_decision, make_league, status_event

WRITE = frozenset(
    {ProviderCapability.READ, ProviderCapability.LINEUP_WRITE, ProviderCapability.WAIVER_WRITE}
)


def _policy(league_id: str, mode: AutonomyMode = AutonomyMode.AUTONOMOUS) -> AutonomyPolicy:
    return AutonomyPolicy(league_id=league_id, default_mode=mode)  # type: ignore[arg-type]


def test_read_only_helper() -> None:
    assert is_read_only(frozenset({ProviderCapability.READ}))
    assert not is_read_only(WRITE)


def test_policy_mode_is_most_restrictive() -> None:
    league = make_league()
    d = lineup_decision(league_id=league.league_id, mode=AutonomyMode.AUTONOMOUS)
    policy = AutonomyPolicy(
        league_id=league.league_id,
        default_mode=AutonomyMode.AUTONOMOUS,
        mode_overrides={DecisionType.LINEUP: AutonomyMode.OBSERVE},
    )
    r = route_decision(d, policy, WRITE, league)
    assert r.routing is Routing.RECORD_ONLY and r.initial_status is DecisionStatus.RECORDED


def test_low_confidence_downgrades_to_approval() -> None:
    league = make_league()
    d = lineup_decision(league_id=league.league_id, mode=AutonomyMode.AUTONOMOUS, confidence=0.5)
    r = route_decision(d, _policy(league.league_id), WRITE, league)
    assert r.routing is Routing.REQUEST_APPROVAL
    assert any("confidence" in reason for reason in r.reasons)


def test_insufficient_evidence_blocks_autonomy() -> None:
    league = make_league()
    d = lineup_decision(league_id=league.league_id, mode=AutonomyMode.AUTONOMOUS, insufficient=True)
    r = route_decision(d, _policy(league.league_id), WRITE, league)
    assert r.routing is Routing.REQUEST_APPROVAL


def _waiver_decision(league_id: str, bid: int) -> Decision:
    base = lineup_decision(league_id=league_id, mode=AutonomyMode.AUTONOMOUS)  # type: ignore[arg-type]
    cand = DecisionCandidate(action=WaiverClaim(add_player_id=new_player_id(), faab_bid=bid))
    return Decision(
        league_id=base.league_id,
        fantasy_team_id=base.fantasy_team_id,
        decision_type=DecisionType.WAIVER_CLAIM,
        created_at=base.created_at,
        information_cutoff=base.information_cutoff,
        candidates=(cand,),
        selected_candidate_id=cand.candidate_id,
        model_versions={"decision_engine": "t"},
        confidence=ConfidenceAssessment(score=0.95, insufficient_evidence=False),
        autonomy_mode=AutonomyMode.AUTONOMOUS,
    )


def test_faab_ceiling() -> None:
    league = make_league(faab_budget=100)
    ok = route_decision(
        _waiver_decision(league.league_id, 20), _policy(league.league_id), WRITE, league
    )
    big = route_decision(
        _waiver_decision(league.league_id, 60), _policy(league.league_id), WRITE, league
    )
    assert ok.routing is Routing.EXECUTE
    assert big.routing is Routing.REQUEST_APPROVAL


def _approved_entry(mode: AutonomyMode = AutonomyMode.AUTONOMOUS):  # type: ignore[no-untyped-def]
    league = make_league()
    d = lineup_decision(league_id=league.league_id, mode=mode)
    ledger = InMemoryDecisionLedger()
    ledger.record(d, status_event(d, DecisionStatus.APPROVED, actor=ActorKind.POLICY))
    return league, d, ledger.get(d.decision_id)


def test_kill_switch_and_staleness() -> None:
    league, d, entry = _approved_entry()
    policy = _policy(league.league_id)
    off = authorize_execution(
        entry, policy, WRITE, d.created_at, execution_enabled=False, league=league
    )
    assert not off.allowed and any("kill switch" in r for r in off.reasons)
    stale = authorize_execution(
        entry,
        policy,
        WRITE,
        d.created_at + timedelta(days=1),
        execution_enabled=True,
        league=league,
    )
    assert not stale.allowed and any("stale" in r for r in stale.reasons)
    fresh = authorize_execution(
        entry, policy, WRITE, d.created_at, execution_enabled=True, league=league
    )
    assert fresh.allowed


def test_policy_downgrade_after_approval_blocks_execution() -> None:
    league, d, entry = _approved_entry()
    downgraded = _policy(league.league_id, AutonomyMode.RECOMMEND)
    auth = authorize_execution(
        entry, downgraded, WRITE, d.created_at, execution_enabled=True, league=league
    )
    assert not auth.allowed


def test_no_action_is_never_executed() -> None:
    league = make_league()
    d = lineup_decision(league_id=league.league_id, mode=AutonomyMode.AUTONOMOUS)
    hold = next(c for c in d.candidates if c.action.action_type == "no_action")
    d2 = d.model_copy(update={"selected_candidate_id": hold.candidate_id})
    r = route_decision(d2, _policy(league.league_id), WRITE, league)
    assert r.routing is Routing.NOTIFY and not r.manual_action_required
