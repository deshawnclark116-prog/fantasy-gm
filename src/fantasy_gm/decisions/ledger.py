"""Append-only decision ledger.

* ``record`` writes a decision exactly once, together with its initial status event.
* Status changes, outcomes and grades are separate append-only records.
* The stored decision is serialised + hashed; every read re-verifies the hash, so no later
  write path (outcome attachment, grading, a buggy caller mutating a returned object) can alter
  what the decision saw. See ADR 0004.
"""

from __future__ import annotations

from typing import Protocol

from fantasy_gm.domain.autonomy import AutonomyMode
from fantasy_gm.domain.base import DomainModel, sha256_hex
from fantasy_gm.domain.decision import (
    ActorKind,
    Decision,
    DecisionGrade,
    DecisionOutcome,
    DecisionStatus,
    DecisionStatusEvent,
)
from fantasy_gm.domain.ids import DecisionId, LeagueId


class LedgerError(RuntimeError):
    pass


class DecisionNotFoundError(LedgerError, LookupError):
    pass


class ImmutableRecordError(LedgerError):
    pass


class InvalidTransitionError(LedgerError):
    pass


class TamperDetectedError(LedgerError):
    pass


class LedgerEntry(DomainModel):
    decision: Decision
    decision_hash: str
    status_history: tuple[DecisionStatusEvent, ...]
    outcomes: tuple[DecisionOutcome, ...] = ()
    grades: tuple[DecisionGrade, ...] = ()

    @property
    def current_status(self) -> DecisionStatus:
        return self.status_history[-1].status


S = DecisionStatus
TRANSITIONS: dict[DecisionStatus, frozenset[DecisionStatus]] = {
    S.RECORDED: frozenset(),
    S.RECOMMENDED: frozenset({S.EXECUTED, S.REJECTED, S.EXPIRED, S.SUPERSEDED}),
    S.AWAITING_APPROVAL: frozenset({S.APPROVED, S.REJECTED, S.EXPIRED, S.SUPERSEDED}),
    S.APPROVED: frozenset(
        {S.EXECUTED, S.EXECUTION_FAILED, S.EXECUTION_BLOCKED, S.EXPIRED, S.SUPERSEDED}
    ),
    S.EXECUTION_FAILED: frozenset({S.EXECUTED, S.EXECUTION_FAILED, S.EXECUTION_BLOCKED, S.EXPIRED}),
    S.EXECUTION_BLOCKED: frozenset({S.EXECUTED, S.EXPIRED, S.SUPERSEDED}),
    S.REJECTED: frozenset(),
    S.EXECUTED: frozenset(),
    S.EXPIRED: frozenset(),
    S.SUPERSEDED: frozenset(),
}

# A gate may downgrade a decision's routing (e.g. AUTONOMOUS -> approval) but never upgrade it.
INITIAL_STATUSES: dict[AutonomyMode, frozenset[DecisionStatus]] = {
    AutonomyMode.OBSERVE: frozenset({S.RECORDED}),
    AutonomyMode.RECOMMEND: frozenset({S.RECOMMENDED, S.RECORDED}),
    AutonomyMode.APPROVAL_REQUIRED: frozenset({S.AWAITING_APPROVAL, S.RECOMMENDED, S.RECORDED}),
    AutonomyMode.AUTONOMOUS: frozenset(
        {S.APPROVED, S.AWAITING_APPROVAL, S.RECOMMENDED, S.RECORDED}
    ),
}


def validate_initial(decision: Decision, event: DecisionStatusEvent) -> None:
    if event.decision_id != decision.decision_id:
        raise LedgerError("status event references a different decision")
    if event.status not in INITIAL_STATUSES[decision.autonomy_mode]:
        raise InvalidTransitionError(
            f"{event.status} is not a valid initial status in {decision.autonomy_mode} mode"
        )
    if event.status is S.APPROVED and event.actor.kind is not ActorKind.POLICY:
        raise InvalidTransitionError("initial APPROVED status can only come from policy")
    if event.occurred_at < decision.created_at:
        raise LedgerError("status event cannot precede the decision")


def validate_transition(entry: LedgerEntry, event: DecisionStatusEvent) -> None:
    if event.decision_id != entry.decision.decision_id:
        raise LedgerError("status event references a different decision")
    current = entry.current_status
    if event.status not in TRANSITIONS[current]:
        raise InvalidTransitionError(f"{current} -> {event.status} is not allowed")
    if event.occurred_at < entry.status_history[-1].occurred_at:
        raise LedgerError("status events must be appended in time order")
    if (
        event.status is S.APPROVED
        and entry.decision.autonomy_mode is not AutonomyMode.AUTONOMOUS
        and event.actor.kind is not ActorKind.USER
    ):
        raise InvalidTransitionError("only a user can approve outside AUTONOMOUS mode")


def validate_outcome(entry: LedgerEntry, outcome: DecisionOutcome) -> None:
    decision = entry.decision
    if outcome.decision_id != decision.decision_id:
        raise LedgerError("outcome references a different decision")
    if outcome.recorded_at <= decision.information_cutoff:
        raise LedgerError(
            "an outcome cannot be recorded at/before the decision's information cutoff"
        )
    unknown = set(outcome.candidate_realized) - {c.candidate_id for c in decision.candidates}
    if unknown:
        raise LedgerError(f"outcome references unknown candidates {unknown}")


def validate_grade(entry: LedgerEntry, grade: DecisionGrade) -> None:
    if grade.decision_id != entry.decision.decision_id:
        raise LedgerError("grade references a different decision")
    if grade.decision_hash != entry.decision_hash:
        raise TamperDetectedError("grade was computed against a different decision version")
    if grade.outcome_id not in {o.outcome_id for o in entry.outcomes}:
        raise LedgerError("grade references an outcome not attached to this decision")


def load_decision(payload: str, expected_hash: str) -> Decision:
    """Verify the *stored bytes* before parsing.

    Hashing the stored canonical JSON (rather than re-serialising the parsed model) keeps
    verification independent of future pydantic/serialisation changes.
    """
    actual = sha256_hex(payload)
    if actual != expected_hash:
        raise TamperDetectedError(f"decision payload hash mismatch ({actual} != {expected_hash})")
    return Decision.model_validate_json(payload)


class DecisionLedger(Protocol):
    def record(self, decision: Decision, initial_status: DecisionStatusEvent) -> str:
        """Persist a new decision; returns its content hash."""
        ...

    def append_status(self, event: DecisionStatusEvent) -> None: ...

    def attach_outcome(self, outcome: DecisionOutcome) -> None: ...

    def attach_grade(self, grade: DecisionGrade) -> None: ...

    def get(self, decision_id: DecisionId) -> LedgerEntry: ...

    def list_ids(self, league_id: LeagueId) -> list[DecisionId]: ...


class InMemoryDecisionLedger:
    """Reference implementation. Stores serialised JSON only (never live objects)."""

    def __init__(self) -> None:
        self._decisions: dict[DecisionId, tuple[str, str]] = {}  # id -> (json, hash)
        self._league: dict[DecisionId, LeagueId] = {}
        self._events: dict[DecisionId, list[str]] = {}
        self._outcomes: dict[DecisionId, list[str]] = {}
        self._grades: dict[DecisionId, list[str]] = {}

    def record(self, decision: Decision, initial_status: DecisionStatusEvent) -> str:
        if decision.decision_id in self._decisions:
            raise ImmutableRecordError(f"decision {decision.decision_id} already recorded")
        validate_initial(decision, initial_status)
        digest = decision.content_hash()
        self._decisions[decision.decision_id] = (decision.canonical_json(), digest)
        self._league[decision.decision_id] = decision.league_id
        self._events[decision.decision_id] = [initial_status.canonical_json()]
        return digest

    def append_status(self, event: DecisionStatusEvent) -> None:
        entry = self.get(event.decision_id)
        validate_transition(entry, event)
        self._events[event.decision_id].append(event.canonical_json())

    def attach_outcome(self, outcome: DecisionOutcome) -> None:
        entry = self.get(outcome.decision_id)
        validate_outcome(entry, outcome)
        self._outcomes.setdefault(outcome.decision_id, []).append(outcome.canonical_json())

    def attach_grade(self, grade: DecisionGrade) -> None:
        entry = self.get(grade.decision_id)
        validate_grade(entry, grade)
        self._grades.setdefault(grade.decision_id, []).append(grade.canonical_json())

    def get(self, decision_id: DecisionId) -> LedgerEntry:
        stored = self._decisions.get(decision_id)
        if stored is None:
            raise DecisionNotFoundError(decision_id)
        payload, digest = stored
        return LedgerEntry(
            decision=load_decision(payload, digest),
            decision_hash=digest,
            status_history=tuple(
                DecisionStatusEvent.model_validate_json(e) for e in self._events[decision_id]
            ),
            outcomes=tuple(
                DecisionOutcome.model_validate_json(o) for o in self._outcomes.get(decision_id, [])
            ),
            grades=tuple(
                DecisionGrade.model_validate_json(g) for g in self._grades.get(decision_id, [])
            ),
        )

    def list_ids(self, league_id: LeagueId) -> list[DecisionId]:
        return [d for d, lg in self._league.items() if lg == league_id]
