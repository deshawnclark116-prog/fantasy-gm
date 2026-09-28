from __future__ import annotations

from datetime import timedelta

import pytest

from fantasy_gm.decisions.ledger import (
    DecisionLedger,
    DecisionNotFoundError,
    InMemoryDecisionLedger,
    InvalidTransitionError,
    LedgerError,
)
from fantasy_gm.domain.autonomy import AutonomyMode
from fantasy_gm.domain.clock import ManualClock
from fantasy_gm.domain.decision import (
    ActorKind,
    DecisionStatus,
    ExecutionResult,
    ObservedOutcome,
)
from fantasy_gm.domain.ids import DecisionId
from fantasy_gm.grading.grader import grade_observed
from fantasy_gm.player_state.store import ObservationStore
from fantasy_gm.records.codec import TamperDetectedError
from tests.factories import lineup_decision, live_run, memory_store, open_session, status_event, ts


def _live(store: ObservationStore, clock: ManualClock, mode: AutonomyMode):  # type: ignore[no-untyped-def]
    return lineup_decision(open_session(store, clock, clock.now(), run=live_run()), mode=mode)


def test_initial_status_must_match_mode(
    ledger: DecisionLedger, store: ObservationStore, clock: ManualClock
) -> None:
    d = _live(store, clock, AutonomyMode.OBSERVE)
    with pytest.raises(InvalidTransitionError):
        ledger.record(d, status_event(d, DecisionStatus.RECOMMENDED))
    auto = _live(store, clock, AutonomyMode.AUTONOMOUS)
    with pytest.raises(InvalidTransitionError, match="policy"):
        ledger.record(auto, status_event(auto, DecisionStatus.APPROVED, actor=ActorKind.USER))


def test_approval_flow(ledger: DecisionLedger, store: ObservationStore, clock: ManualClock) -> None:
    d = _live(store, clock, AutonomyMode.APPROVAL_REQUIRED)
    ledger.record(d, status_event(d, DecisionStatus.AWAITING_APPROVAL))
    later = clock.advance(timedelta(minutes=1))
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
        ledger.append_status(status_event(d, DecisionStatus.EXPIRED, at=d.decision_time))
    ledger.append_status(status_event(d, DecisionStatus.EXPIRED, at=later))
    entry = ledger.get(d.decision_id)
    assert [s.status for s in entry.statuses] == [
        DecisionStatus.AWAITING_APPROVAL,
        DecisionStatus.APPROVED,
        DecisionStatus.EXPIRED,
    ]
    assert [s.meta.seq for s in entry.status_history] == [0, 1, 2]
    with pytest.raises(InvalidTransitionError):
        ledger.append_status(
            status_event(d, DecisionStatus.APPROVED, at=later, actor=ActorKind.USER)
        )


def test_executed_status_requires_result(store: ObservationStore, clock: ManualClock) -> None:
    d = lineup_decision(open_session(store, clock, ts(days=6)))
    with pytest.raises(ValueError, match="execution_result"):
        status_event(d, DecisionStatus.EXECUTED)


def test_unknown_decision(ledger: DecisionLedger) -> None:
    with pytest.raises(DecisionNotFoundError):
        ledger.get(DecisionId("dec_missing"))


def test_replay_decisions_are_record_only(
    ledger: DecisionLedger, store: ObservationStore, clock: ManualClock
) -> None:
    d = lineup_decision(open_session(store, clock, ts(days=6)), mode=AutonomyMode.AUTONOMOUS)
    with pytest.raises(InvalidTransitionError):
        ledger.record(d, status_event(d, DecisionStatus.APPROVED, actor=ActorKind.POLICY))


def test_outcome_must_reference_known_candidates(
    ledger: DecisionLedger, store: ObservationStore, clock: ManualClock
) -> None:
    d = lineup_decision(open_session(store, clock, ts(days=6)))
    ledger.record(d, status_event(d, DecisionStatus.RECORDED))
    with pytest.raises(LedgerError, match="unknown candidates"):
        ledger.attach_outcome(
            ObservedOutcome(
                decision_id=d.decision_id,
                known_at=ts(days=8),
                realized={"lineup_points": 1.0},
                observed_alternatives={"cand_other": 3.0},
            )
        )  # type: ignore[dict-item]


def test_grade_must_match_recorded_hash(
    ledger: DecisionLedger, store: ObservationStore, clock: ManualClock
) -> None:
    d = lineup_decision(open_session(store, clock, ts(days=6)))
    ledger.record(d, status_event(d, DecisionStatus.RECORDED))
    outcome = ObservedOutcome(
        decision_id=d.decision_id, known_at=ts(days=8), realized={"lineup_points": 100.0}
    )
    ledger.attach_outcome(outcome)
    grade = grade_observed(ledger.get(d.decision_id), outcome, clock.now())
    with pytest.raises(TamperDetectedError):
        ledger.attach_grade(grade.model_copy(update={"decision_hash": "0" * 64}))


def test_in_memory_tamper_detection(clock: ManualClock) -> None:
    ledger = InMemoryDecisionLedger(clock)
    d = lineup_decision(open_session(memory_store(), clock, ts(days=6)))
    ledger.record(d, status_event(d, DecisionStatus.RECORDED))
    rows = ledger._rows[d.decision_id]
    enc, at = rows.decision
    rows.decision = (
        enc.model_copy(update={"payload": enc.payload.replace("start A", "start Z")}),
        at,
    )
    with pytest.raises(TamperDetectedError):
        ledger.get(d.decision_id)


def test_returned_objects_cannot_rewrite_history(clock: ManualClock) -> None:
    """``engine_versions`` is a deeply frozen ``FrozenMapping`` (ADR 0019): a returned decision's
    nested container cannot be mutated in place at all, so it can never drift from what the
    ledger has stored -- there is no window in which a mutation "doesn't stick"."""
    ledger = InMemoryDecisionLedger(clock)
    d = lineup_decision(open_session(memory_store(), clock, ts(days=6)))
    ledger.record(d, status_event(d, DecisionStatus.RECORDED))
    returned = ledger.get(d.decision_id).decision.value
    with pytest.raises(TypeError):
        returned.engine_versions["decision_engine"] = "rewritten"  # type: ignore[index]
    assert ledger.get(d.decision_id).decision.value.engine_versions["decision_engine"] == (
        "test_fixture_v0"
    )
