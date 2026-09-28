"""Execution attempts: append-only, idempotent, crash/retry safe (ADR 0012).

The derived ``ExecutionState`` is a pure function of the attempt log and the current time.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum

from pydantic import Field

from fantasy_gm.domain.actions import Action
from fantasy_gm.domain.base import DomainModel, canonical_json
from fantasy_gm.domain.ids import AttemptId, DecisionId, ExecutionEventId, new_execution_event_id
from fantasy_gm.domain.time import UtcDatetime


def action_fingerprint(action: Action) -> str:
    """Stable hash of *what* is done (independent of which decision asked for it)."""
    return hashlib.sha256(canonical_json(action.model_dump(mode="json")).encode()).hexdigest()


def idempotency_key(decision_id: DecisionId, action: Action) -> str:
    """Deterministic per decision+action: identical across retries of the same decision."""
    raw = f"{decision_id}:{action_fingerprint(action)}"
    return hashlib.sha256(raw.encode()).hexdigest()


class ExecutionEventKind(StrEnum):
    ATTEMPT_STARTED = "attempt_started"  # the claim
    PROVIDER_CONFIRMED = "provider_confirmed"
    PROVIDER_REJECTED = "provider_rejected"  # definitive: not executed
    OUTCOME_UNKNOWN = "outcome_unknown"  # timeout / lost response / crash
    RECONCILED_EXECUTED = "reconciled_executed"
    RECONCILED_NOT_EXECUTED = "reconciled_not_executed"


class ExecutionEvent(DomainModel):
    event_id: ExecutionEventId = Field(default_factory=new_execution_event_id)
    decision_id: DecisionId
    attempt_id: AttemptId
    kind: ExecutionEventKind
    occurred_at: UtcDatetime
    provider: str
    idempotency_key: str
    action_fingerprint: str
    worker_id: str
    lease_expires_at: UtcDatetime | None = None
    native_idempotency: bool = False
    provider_transaction_ref: str | None = None
    message: str = ""


class ExecutionPhase(StrEnum):
    READY = "ready"  # nothing in flight, nothing executed
    IN_FLIGHT = "in_flight"  # a live claim holds the decision
    UNCERTAIN = "uncertain"  # outcome unknown (or claim lease expired): reconcile before retry
    SUCCEEDED = "succeeded"


class ExecutionState(DomainModel):
    phase: ExecutionPhase
    attempts: int = 0
    current_attempt_id: AttemptId | None = None
    current_attempt_started_at: UtcDatetime | None = None
    lease_expires_at: UtcDatetime | None = None
    provider_transaction_ref: str | None = None


def derive_execution_state(events: tuple[ExecutionEvent, ...], now: datetime) -> ExecutionState:
    phase = ExecutionPhase.READY
    attempts = 0
    current: ExecutionEvent | None = None
    txn: str | None = None
    for ev in events:
        k = ev.kind
        if k is ExecutionEventKind.ATTEMPT_STARTED:
            attempts += 1
            current = ev
            phase = ExecutionPhase.IN_FLIGHT
        elif k in (ExecutionEventKind.PROVIDER_CONFIRMED, ExecutionEventKind.RECONCILED_EXECUTED):
            phase = ExecutionPhase.SUCCEEDED
            txn = ev.provider_transaction_ref
        elif k in (
            ExecutionEventKind.PROVIDER_REJECTED,
            ExecutionEventKind.RECONCILED_NOT_EXECUTED,
        ):
            phase = ExecutionPhase.READY
        elif k is ExecutionEventKind.OUTCOME_UNKNOWN:
            phase = ExecutionPhase.UNCERTAIN
    lease = current.lease_expires_at if current else None
    if phase is ExecutionPhase.IN_FLIGHT and lease is not None and now > lease:
        phase = ExecutionPhase.UNCERTAIN  # the claiming worker died or hung mid-call
    return ExecutionState(
        phase=phase,
        attempts=attempts,
        current_attempt_id=current.attempt_id if current else None,
        current_attempt_started_at=current.occurred_at if current else None,
        lease_expires_at=lease,
        provider_transaction_ref=txn,
    )


class InvalidExecutionEventError(RuntimeError):
    pass


def validate_execution_event(
    events: tuple[ExecutionEvent, ...], new: ExecutionEvent, now: datetime
) -> None:
    """State machine for the attempt log (enforced under the decision lock)."""
    state = derive_execution_state(events, now)
    k = new.kind
    if state.phase is ExecutionPhase.SUCCEEDED:
        raise InvalidExecutionEventError("decision already executed")
    if k is ExecutionEventKind.ATTEMPT_STARTED:
        native_retry = state.phase is ExecutionPhase.UNCERTAIN and new.native_idempotency
        if state.phase is not ExecutionPhase.READY and not native_retry:
            raise InvalidExecutionEventError(f"cannot start an attempt while {state.phase}")
        if new.lease_expires_at is None:
            raise InvalidExecutionEventError("attempts require a lease")
        prior_keys = {e.idempotency_key for e in events}
        if prior_keys and new.idempotency_key not in prior_keys:
            raise InvalidExecutionEventError("idempotency key changed between attempts")
        return
    if new.attempt_id != state.current_attempt_id:
        raise InvalidExecutionEventError("event does not belong to the current attempt")
    if k in (
        ExecutionEventKind.PROVIDER_CONFIRMED,
        ExecutionEventKind.PROVIDER_REJECTED,
        ExecutionEventKind.OUTCOME_UNKNOWN,
    ):
        if state.phase not in (ExecutionPhase.IN_FLIGHT, ExecutionPhase.UNCERTAIN):
            raise InvalidExecutionEventError(f"no attempt in flight ({state.phase})")
        return
    if state.phase is not ExecutionPhase.UNCERTAIN:
        raise InvalidExecutionEventError("reconciliation is only valid while uncertain")
