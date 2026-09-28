"""Append-only decision ledger.

* The ledger owns the physical clock: every stored record gets a ledger-stamped ``recorded_at``
  that no caller can supply (ADR 0008).
* Every record is stored as versioned canonical JSON + SHA-256 of those exact bytes; reads verify
  the bytes before parsing and dispatch on ``schema_version`` (ADR 0009).
* The decision itself is written once. Status events, execution-attempt events, observed
  outcomes, grades and model-based counterfactuals are separate append-only records.
* Appends to one decision are serialised (row lock on PostgreSQL, a write lock elsewhere) and
  support optimistic ``expected_seq``; races surface as ``ConcurrentModificationError`` or a
  domain transition error -- never as a raw integrity failure (ADR 0012).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from pydantic import BaseModel

from fantasy_gm.domain.autonomy import AutonomyMode
from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.clock import Clock
from fantasy_gm.domain.decision import (
    ActorKind,
    CounterfactualEstimate,
    CounterfactualEvaluation,
    Decision,
    DecisionStatus,
    DecisionStatusEvent,
    ObservedGrade,
    ObservedOutcome,
)
from fantasy_gm.domain.execution import (
    ExecutionEvent,
    ExecutionEventKind,
    ExecutionPhase,
    InvalidExecutionEventError,
    derive_execution_state,
    validate_execution_event,
)
from fantasy_gm.domain.ids import DecisionId, LeagueId
from fantasy_gm.domain.run_context import RunMode
from fantasy_gm.domain.time import UtcDatetime
from fantasy_gm.records.codec import EncodedRecord, TamperDetectedError, VersionedCodec
from fantasy_gm.records.registry import DEFAULT_CODECS, CodecSet


class LedgerError(RuntimeError):
    pass


class DecisionNotFoundError(LedgerError, LookupError):
    pass


class ImmutableRecordError(LedgerError):
    pass


class InvalidTransitionError(LedgerError):
    pass


class ConcurrentModificationError(LedgerError):
    """Another writer appended to this decision first; re-read and decide again."""


class RecordTimeError(LedgerError):
    """A record's logical time is inconsistent with when the ledger physically recorded it."""


class RecordMeta(DomainModel):
    record_type: str
    schema_version: int
    payload_hash: str
    recorded_at: UtcDatetime
    seq: int | None = None


class Stamped[T: BaseModel](DomainModel):
    value: T
    meta: RecordMeta


class LedgerEntry(DomainModel):
    decision: Stamped[Decision]
    status_history: tuple[Stamped[DecisionStatusEvent], ...]
    execution_events: tuple[Stamped[ExecutionEvent], ...] = ()
    outcomes: tuple[Stamped[ObservedOutcome], ...] = ()
    grades: tuple[Stamped[ObservedGrade], ...] = ()
    counterfactual_estimates: tuple[Stamped[CounterfactualEstimate], ...] = ()
    counterfactual_evaluations: tuple[Stamped[CounterfactualEvaluation], ...] = ()

    @property
    def current_status(self) -> DecisionStatus:
        return self.status_history[-1].value.status

    @property
    def decision_hash(self) -> str:
        return self.decision.meta.payload_hash

    @property
    def recorded_at(self) -> datetime:
        return self.decision.meta.recorded_at

    @property
    def statuses(self) -> tuple[DecisionStatusEvent, ...]:
        return tuple(s.value for s in self.status_history)

    @property
    def executions(self) -> tuple[ExecutionEvent, ...]:
        return tuple(e.value for e in self.execution_events)


class LedgerTimePolicy(DomainModel):
    # LIVE/PAPER: a record's logical time may trail the physical write by at most this much...
    max_record_lag: timedelta = timedelta(minutes=5)
    # ...and may lead it (clock skew between services) by at most this much.
    max_future_skew: timedelta = timedelta(seconds=30)


S = DecisionStatus
TRANSITIONS: dict[DecisionStatus, frozenset[DecisionStatus]] = {
    S.RECORDED: frozenset(),
    S.RECOMMENDED: frozenset({S.EXECUTED, S.REJECTED, S.EXPIRED, S.SUPERSEDED}),
    S.AWAITING_APPROVAL: frozenset({S.APPROVED, S.REJECTED, S.EXPIRED, S.SUPERSEDED}),
    S.APPROVED: frozenset(
        {
            S.EXECUTED,
            S.EXECUTION_FAILED,
            S.EXECUTION_BLOCKED,
            S.EXECUTION_UNCERTAIN,
            S.EXPIRED,
            S.SUPERSEDED,
        }
    ),
    S.EXECUTION_FAILED: frozenset(
        {
            S.EXECUTED,
            S.EXECUTION_FAILED,
            S.EXECUTION_BLOCKED,
            S.EXECUTION_UNCERTAIN,
            S.EXPIRED,
            S.SUPERSEDED,
        }
    ),
    # An uncertain execution can only be resolved by reconciliation: it can never simply expire
    # or be superseded, because the action may already have happened.
    S.EXECUTION_UNCERTAIN: frozenset({S.EXECUTED, S.EXECUTION_FAILED, S.EXECUTION_UNCERTAIN}),
    S.EXECUTION_BLOCKED: frozenset({S.EXECUTED, S.EXPIRED, S.SUPERSEDED}),
    S.REJECTED: frozenset(),
    S.EXECUTED: frozenset(),
    S.EXPIRED: frozenset(),
    S.SUPERSEDED: frozenset(),
}

INITIAL_STATUSES: dict[AutonomyMode, frozenset[DecisionStatus]] = {
    AutonomyMode.OBSERVE: frozenset({S.RECORDED}),
    AutonomyMode.RECOMMEND: frozenset({S.RECOMMENDED, S.RECORDED}),
    AutonomyMode.APPROVAL_REQUIRED: frozenset({S.AWAITING_APPROVAL, S.RECOMMENDED, S.RECORDED}),
    AutonomyMode.AUTONOMOUS: frozenset(
        {S.APPROVED, S.AWAITING_APPROVAL, S.RECOMMENDED, S.RECORDED}
    ),
}

EXECUTABLE_STATUSES = frozenset({S.APPROVED, S.EXECUTION_FAILED, S.EXECUTION_UNCERTAIN})


# --------------------------------------------------------------------------- validation (shared)


def _check_logical_time(
    label: str,
    logical: datetime,
    recorded_at: datetime,
    mode: RunMode,
    policy: LedgerTimePolicy,
) -> None:
    if logical > recorded_at + policy.max_future_skew:
        raise RecordTimeError(
            f"{label} {logical.isoformat()} is in the future of the ledger clock "
            f"{recorded_at.isoformat()}"
        )
    if mode in (RunMode.LIVE, RunMode.PAPER) and recorded_at - logical > policy.max_record_lag:
        raise RecordTimeError(
            f"{mode} {label} {logical.isoformat()} is {recorded_at - logical} older than the "
            f"physical write at {recorded_at.isoformat()} (max {policy.max_record_lag}); a live "
            "record cannot be backdated"
        )


def validate_new_decision(
    decision: Decision,
    initial: DecisionStatusEvent,
    recorded_at: datetime,
    policy: LedgerTimePolicy,
) -> None:
    mode = decision.run.mode
    _check_logical_time("decision_time", decision.decision_time, recorded_at, mode, policy)
    _check_logical_time(
        "evidence sealed_at", decision.evidence.sealed_at, recorded_at, mode, policy
    )
    if initial.decision_id != decision.decision_id:
        raise LedgerError("status event references a different decision")
    allowed = INITIAL_STATUSES[decision.autonomy_mode]
    if mode is not RunMode.LIVE:
        allowed = frozenset({S.RECORDED})  # PAPER / REPLAY never notify or execute
    if initial.status not in allowed:
        raise InvalidTransitionError(
            f"{initial.status} is not a valid initial status for a {mode} decision in "
            f"{decision.autonomy_mode} mode"
        )
    if initial.status is S.APPROVED and initial.actor.kind is not ActorKind.POLICY:
        raise InvalidTransitionError("initial APPROVED status can only come from policy")
    if initial.occurred_at < decision.decision_time:
        raise LedgerError("status event cannot precede the decision")
    _check_logical_time("status occurred_at", initial.occurred_at, recorded_at, mode, policy)


def validate_transition(
    entry: LedgerEntry,
    event: DecisionStatusEvent,
    recorded_at: datetime,
    policy: LedgerTimePolicy,
) -> None:
    decision = entry.decision.value
    if event.decision_id != decision.decision_id:
        raise LedgerError("status event references a different decision")
    current = entry.current_status
    if event.status not in TRANSITIONS[current]:
        raise InvalidTransitionError(f"{current} -> {event.status} is not allowed")
    if event.occurred_at < entry.status_history[-1].value.occurred_at:
        raise LedgerError("status events must be appended in time order")
    if event.status in (S.EXPIRED, S.SUPERSEDED, S.REJECTED):
        phase = derive_execution_state(entry.executions, recorded_at).phase
        if phase in (ExecutionPhase.IN_FLIGHT, ExecutionPhase.UNCERTAIN):
            raise InvalidTransitionError(
                f"cannot mark {event.status} while execution is {phase}: the action may "
                "already have happened"
            )
    if (
        event.status is S.APPROVED
        and decision.autonomy_mode is not AutonomyMode.AUTONOMOUS
        and event.actor.kind is not ActorKind.USER
    ):
        raise InvalidTransitionError("only a user can approve outside AUTONOMOUS mode")
    _check_logical_time(
        "status occurred_at", event.occurred_at, recorded_at, decision.run.mode, policy
    )


def validate_execution_append(
    entry: LedgerEntry,
    event: ExecutionEvent,
    recorded_at: datetime,
    policy: LedgerTimePolicy,
) -> None:
    decision = entry.decision.value
    if event.decision_id != decision.decision_id:
        raise LedgerError("execution event references a different decision")
    if decision.run.mode is not RunMode.LIVE:
        raise InvalidTransitionError(f"{decision.run.mode} decisions can never be executed")
    if (
        event.kind is ExecutionEventKind.ATTEMPT_STARTED
        and entry.current_status not in EXECUTABLE_STATUSES
    ):
        raise InvalidTransitionError(f"cannot start execution from status {entry.current_status}")
    _check_logical_time(
        "execution occurred_at", event.occurred_at, recorded_at, RunMode.LIVE, policy
    )
    try:
        validate_execution_event(entry.executions, event, recorded_at)
    except InvalidExecutionEventError as exc:
        raise InvalidTransitionError(str(exc)) from exc


def validate_outcome(
    entry: LedgerEntry, outcome: ObservedOutcome, recorded_at: datetime, policy: LedgerTimePolicy
) -> None:
    decision = entry.decision.value
    if outcome.decision_id != decision.decision_id:
        raise LedgerError("outcome references a different decision")
    if outcome.known_at <= decision.information_cutoff:
        raise LedgerError("an outcome cannot be known at/before the decision's information cutoff")
    if outcome.known_at > recorded_at + policy.max_future_skew:
        raise RecordTimeError("an outcome cannot be known after it is recorded")
    unknown = set(outcome.observed_alternatives) - {c.candidate_id for c in decision.candidates}
    if unknown:
        raise LedgerError(f"outcome references unknown candidates {unknown}")


def validate_grade(entry: LedgerEntry, grade: ObservedGrade) -> None:
    if grade.decision_id != entry.decision.value.decision_id:
        raise LedgerError("grade references a different decision")
    if grade.decision_hash != entry.decision_hash:
        raise TamperDetectedError("grade was computed against a different decision version")
    if grade.outcome_id not in {o.value.outcome_id for o in entry.outcomes}:
        raise LedgerError("grade references an outcome not attached to this decision")


def validate_counterfactual_estimate(
    entry: LedgerEntry,
    estimate: CounterfactualEstimate,
    recorded_at: datetime,
    policy: LedgerTimePolicy,
) -> None:
    decision = entry.decision.value
    if estimate.decision_id != decision.decision_id:
        raise LedgerError("estimate references a different decision")
    if estimate.candidate_id not in {c.candidate_id for c in decision.candidates}:
        raise LedgerError("estimate references an unknown candidate")
    if estimate.computed_at > recorded_at + policy.max_future_skew:
        raise RecordTimeError("an estimate cannot be computed after it is recorded")


def validate_counterfactual_evaluation(
    entry: LedgerEntry, evaluation: CounterfactualEvaluation
) -> None:
    if evaluation.decision_id != entry.decision.value.decision_id:
        raise LedgerError("evaluation references a different decision")
    if evaluation.decision_hash != entry.decision_hash:
        raise TamperDetectedError("evaluation was computed against a different decision version")
    if evaluation.outcome_id not in {o.value.outcome_id for o in entry.outcomes}:
        raise LedgerError("evaluation references an outcome not attached to this decision")
    known = {e.value.estimate_id for e in entry.counterfactual_estimates}
    if not set(evaluation.estimate_ids) <= known:
        raise LedgerError("evaluation references estimates not attached to this decision")


# --------------------------------------------------------------------------- protocol


class DecisionLedger(Protocol):
    def record(self, decision: Decision, initial_status: DecisionStatusEvent) -> RecordMeta: ...

    def append_status(
        self, event: DecisionStatusEvent, expected_seq: int | None = None
    ) -> RecordMeta: ...

    def append_execution_event(self, event: ExecutionEvent, expected_seq: int) -> RecordMeta: ...

    def attach_outcome(self, outcome: ObservedOutcome) -> RecordMeta: ...

    def attach_grade(self, grade: ObservedGrade) -> RecordMeta: ...

    def attach_counterfactual_estimate(self, estimate: CounterfactualEstimate) -> RecordMeta: ...

    def attach_counterfactual_evaluation(
        self, evaluation: CounterfactualEvaluation
    ) -> RecordMeta: ...

    def get(self, decision_id: DecisionId) -> LedgerEntry: ...

    def list_ids(self, league_id: LeagueId) -> list[DecisionId]: ...


def stamp[T: BaseModel](
    codec: VersionedCodec[T], record: EncodedRecord, recorded_at: datetime, seq: int | None = None
) -> Stamped[T]:
    return Stamped[T](
        value=codec.decode_record(record),
        meta=RecordMeta(
            record_type=record.record_type,
            schema_version=record.schema_version,
            payload_hash=record.payload_hash,
            recorded_at=recorded_at,
            seq=seq,
        ),
    )


def meta_for(record: EncodedRecord, recorded_at: datetime, seq: int | None = None) -> RecordMeta:
    return RecordMeta(
        record_type=record.record_type,
        schema_version=record.schema_version,
        payload_hash=record.payload_hash,
        recorded_at=recorded_at,
        seq=seq,
    )


# --------------------------------------------------------------------------- in-memory


Row = tuple[EncodedRecord, datetime]


@dataclass
class _Rows:
    league_id: LeagueId
    decision: Row
    statuses: list[Row] = field(default_factory=list)
    executions: list[Row] = field(default_factory=list)
    outcomes: list[Row] = field(default_factory=list)
    grades: list[Row] = field(default_factory=list)
    cf_estimates: list[Row] = field(default_factory=list)
    cf_evaluations: list[Row] = field(default_factory=list)


class InMemoryDecisionLedger:
    """Reference implementation: stores encoded records only; one lock serialises appends."""

    def __init__(
        self,
        clock: Clock,
        codecs: CodecSet = DEFAULT_CODECS,
        time_policy: LedgerTimePolicy | None = None,
    ) -> None:
        self._clock = clock
        self._codecs = codecs
        self._policy = time_policy or LedgerTimePolicy()
        self._rows: dict[DecisionId, _Rows] = {}
        self._lock = threading.RLock()

    def record(self, decision: Decision, initial_status: DecisionStatusEvent) -> RecordMeta:
        with self._lock:
            if decision.decision_id in self._rows:
                raise ImmutableRecordError(f"decision {decision.decision_id} already recorded")
            now = self._clock.now()
            validate_new_decision(decision, initial_status, now, self._policy)
            enc = self._codecs.decision.encode(decision)
            self._rows[decision.decision_id] = _Rows(
                league_id=decision.league_id,
                decision=(enc, now),
                statuses=[(self._codecs.status_event.encode(initial_status), now)],
            )
            return meta_for(enc, now)

    def append_status(
        self, event: DecisionStatusEvent, expected_seq: int | None = None
    ) -> RecordMeta:
        with self._lock:
            entry = self._get_locked(event.decision_id)
            seq = len(entry.status_history)
            if expected_seq is not None and expected_seq != seq:
                raise ConcurrentModificationError(f"expected status seq {expected_seq}, at {seq}")
            now = self._clock.now()
            validate_transition(entry, event, now, self._policy)
            enc = self._codecs.status_event.encode(event)
            self._rows[event.decision_id].statuses.append((enc, now))
            return meta_for(enc, now, seq)

    def append_execution_event(self, event: ExecutionEvent, expected_seq: int) -> RecordMeta:
        with self._lock:
            entry = self._get_locked(event.decision_id)
            seq = len(entry.execution_events)
            if expected_seq != seq:
                raise ConcurrentModificationError(
                    f"expected execution seq {expected_seq}, at {seq}"
                )
            now = self._clock.now()
            validate_execution_append(entry, event, now, self._policy)
            enc = self._codecs.execution_event.encode(event)
            self._rows[event.decision_id].executions.append((enc, now))
            return meta_for(enc, now, seq)

    def attach_outcome(self, outcome: ObservedOutcome) -> RecordMeta:
        with self._lock:
            entry = self._get_locked(outcome.decision_id)
            now = self._clock.now()
            validate_outcome(entry, outcome, now, self._policy)
            enc = self._codecs.observed_outcome.encode(outcome)
            self._rows[outcome.decision_id].outcomes.append((enc, now))
            return meta_for(enc, now)

    def attach_grade(self, grade: ObservedGrade) -> RecordMeta:
        with self._lock:
            entry = self._get_locked(grade.decision_id)
            validate_grade(entry, grade)
            now = self._clock.now()
            enc = self._codecs.observed_grade.encode(grade)
            self._rows[grade.decision_id].grades.append((enc, now))
            return meta_for(enc, now)

    def attach_counterfactual_estimate(self, estimate: CounterfactualEstimate) -> RecordMeta:
        with self._lock:
            entry = self._get_locked(estimate.decision_id)
            now = self._clock.now()
            validate_counterfactual_estimate(entry, estimate, now, self._policy)
            enc = self._codecs.counterfactual_estimate.encode(estimate)
            self._rows[estimate.decision_id].cf_estimates.append((enc, now))
            return meta_for(enc, now)

    def attach_counterfactual_evaluation(self, evaluation: CounterfactualEvaluation) -> RecordMeta:
        with self._lock:
            entry = self._get_locked(evaluation.decision_id)
            validate_counterfactual_evaluation(entry, evaluation)
            now = self._clock.now()
            enc = self._codecs.counterfactual_evaluation.encode(evaluation)
            self._rows[evaluation.decision_id].cf_evaluations.append((enc, now))
            return meta_for(enc, now)

    def get(self, decision_id: DecisionId) -> LedgerEntry:
        with self._lock:
            return self._get_locked(decision_id)

    def list_ids(self, league_id: LeagueId) -> list[DecisionId]:
        with self._lock:
            return [d for d, r in self._rows.items() if r.league_id == league_id]

    def _get_locked(self, decision_id: DecisionId) -> LedgerEntry:
        rows = self._rows.get(decision_id)
        if rows is None:
            raise DecisionNotFoundError(decision_id)
        c = self._codecs
        return LedgerEntry(
            decision=stamp(c.decision, *rows.decision),
            status_history=tuple(
                stamp(c.status_event, r, at, i) for i, (r, at) in enumerate(rows.statuses)
            ),
            execution_events=tuple(
                stamp(c.execution_event, r, at, i) for i, (r, at) in enumerate(rows.executions)
            ),
            outcomes=tuple(stamp(c.observed_outcome, r, at) for r, at in rows.outcomes),
            grades=tuple(stamp(c.observed_grade, r, at) for r, at in rows.grades),
            counterfactual_estimates=tuple(
                stamp(c.counterfactual_estimate, r, at) for r, at in rows.cf_estimates
            ),
            counterfactual_evaluations=tuple(
                stamp(c.counterfactual_evaluation, r, at) for r, at in rows.cf_evaluations
            ),
        )
