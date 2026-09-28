from __future__ import annotations

from datetime import timedelta

import pytest

from fantasy_gm.decisions.ledger import (
    DecisionLedger,
    DecisionNotFoundError,
    InMemoryDecisionLedger,
    InvalidTransitionError,
    LedgerError,
    TamperDetectedError,
)
from fantasy_gm.domain.autonomy import AutonomyMode
from fantasy_gm.domain.decision import (
    ActorKind,
    DecisionOutcome,
    DecisionStatus,
    ExecutionResult,
)
from fantasy_gm.domain.ids import DecisionId
from fantasy_gm.grading.grader import grade_decision
from tests.factories import lineup_decision, status_event


def test_initial_status_must_match_mode(ledger: DecisionLedger) -> None:
    d = lineup_decision(mode=AutonomyMode.OBSERVE)
    with pytest.raises(InvalidTransitionError):
        ledger.record(d, status_event(d, DecisionStatus.RECOMMENDED))
    auto = lineup_decision(mode=AutonomyMode.AUTONOMOUS)
    with pytest.raises(InvalidTransitionError, match="policy"):
        ledger.record(auto, status_event(auto, DecisionStatus.APPROVED, actor=ActorKind.USER))


def test_approval_flow(ledger: DecisionLedger) -> None:
    d = lineup_decision(mode=AutonomyMode.APPROVAL_REQUIRED)
    ledger.record(d, status_event(d, DecisionStatus.AWAITING_APPROVAL))
    later = d.created_at + timedelta(minutes=1)
    with pytest.raises(InvalidTransitionError, match="only a user"):
        ledger.append_status(status_event(d, DecisionStatus.APPROVED, at=later))
    with pytest.raises(InvalidTransitionError):
        ledger.append_status(
            status_event(
                d,
                DecisionStatus.EXECUTED,
                at=later,
                execution_result=ExecutionResult(provider="p", success=True, executed_at=later),
            )
        )
    ledger.append_status(status_event(d, DecisionStatus.APPROVED, at=later, actor=ActorKind.USER))
    with pytest.raises(LedgerError, match="time order"):
        ledger.append_status(status_event(d, DecisionStatus.EXPIRED, at=d.created_at))
    ledger.append_status(status_event(d, DecisionStatus.EXPIRED, at=later))
    entry = ledger.get(d.decision_id)
    assert [e.status for e in entry.status_history] == [
        DecisionStatus.AWAITING_APPROVAL,
        DecisionStatus.APPROVED,
        DecisionStatus.EXPIRED,
    ]
    with pytest.raises(InvalidTransitionError):  # terminal
        ledger.append_status(
            status_event(d, DecisionStatus.APPROVED, at=later, actor=ActorKind.USER)
        )


def test_executed_status_requires_result() -> None:
    d = lineup_decision()
    with pytest.raises(ValueError, match="execution_result"):
        status_event(d, DecisionStatus.EXECUTED)


def test_unknown_decision(ledger: DecisionLedger) -> None:
    with pytest.raises(DecisionNotFoundError):
        ledger.get(DecisionId("dec_missing"))


def test_outcome_must_reference_known_candidates(ledger: DecisionLedger) -> None:
    d = lineup_decision()
    ledger.record(d, status_event(d, DecisionStatus.RECOMMENDED))
    with pytest.raises(LedgerError, match="unknown candidates"):
        ledger.attach_outcome(
            DecisionOutcome(
                decision_id=d.decision_id,
                recorded_at=d.created_at + timedelta(days=1),
                realized={"lineup_points": 1.0},
                candidate_realized={"cand_other": 3.0},  # type: ignore[dict-item]
            )
        )


def test_grade_must_match_recorded_hash(ledger: DecisionLedger) -> None:
    d = lineup_decision()
    ledger.record(d, status_event(d, DecisionStatus.RECOMMENDED))
    outcome = DecisionOutcome(
        decision_id=d.decision_id,
        recorded_at=d.created_at + timedelta(days=1),
        realized={"lineup_points": 100.0},
    )
    ledger.attach_outcome(outcome)
    grade = grade_decision(ledger.get(d.decision_id), outcome, d.created_at + timedelta(days=2))
    forged = grade.model_copy(update={"decision_hash": "0" * 64})
    with pytest.raises(TamperDetectedError):
        ledger.attach_grade(forged)


def test_in_memory_tamper_detection() -> None:
    ledger = InMemoryDecisionLedger()
    d = lineup_decision()
    ledger.record(d, status_event(d, DecisionStatus.RECOMMENDED))
    payload, digest = ledger._decisions[d.decision_id]
    ledger._decisions[d.decision_id] = (payload.replace("start A", "start B"), digest)
    with pytest.raises(TamperDetectedError):
        ledger.get(d.decision_id)


def test_returned_objects_cannot_rewrite_history() -> None:
    ledger = InMemoryDecisionLedger()
    d = lineup_decision()
    ledger.record(d, status_event(d, DecisionStatus.RECOMMENDED))
    entry = ledger.get(d.decision_id)
    entry.decision.model_versions["decision_engine"] = "rewritten"  # nested dicts are mutable
    assert ledger.get(d.decision_id).decision.model_versions["decision_engine"] == (
        "test_fixture_v0"
    )
