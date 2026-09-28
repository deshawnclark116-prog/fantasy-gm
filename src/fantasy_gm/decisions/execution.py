"""Execution service: the only code path that calls a provider's write methods (ADR 0012).

Safety properties:
* **Claim before call.** An ``ATTEMPT_STARTED`` event is appended with an optimistic sequence
  number under the ledger's per-decision lock. Two workers racing on one approved decision:
  exactly one claim succeeds; the other sees ``IN_PROGRESS_ELSEWHERE`` and never calls the
  provider. No database transaction is held open across the network call; the claim carries a
  lease instead.
* **Deterministic idempotency key** per decision+action, identical across retries, forwarded to
  adapters (platforms with native idempotency dedupe on it).
* **Unknown outcomes are never retried blindly.** A timeout / lost response / expired lease
  moves the decision to ``EXECUTION_UNCERTAIN``. Before any retry the service reconciles against
  provider history (read-only). FOUND -> executed; NOT_FOUND -> safe to retry;
  UNDETERMINABLE -> stop. Only platforms with native idempotency may re-submit without
  reconciling, and only with the same key.
* **Re-authorised on every attempt**, so a capability revoked (or policy changed, or staleness
  crossed) between the decision and a retry blocks the retry.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from enum import StrEnum

from fantasy_gm.decisions.autonomy import authorize_execution
from fantasy_gm.decisions.ledger import (
    TRANSITIONS,
    ConcurrentModificationError,
    DecisionLedger,
    InvalidTransitionError,
    LedgerEntry,
    LedgerError,
)
from fantasy_gm.domain.autonomy import AutonomyPolicy, FreshnessContext
from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.capabilities import ProviderCapability
from fantasy_gm.domain.clock import Clock
from fantasy_gm.domain.decision import (
    Actor,
    ActorKind,
    DecisionStatus,
    DecisionStatusEvent,
    ExecutionResult,
)
from fantasy_gm.domain.execution import (
    ExecutionEvent,
    ExecutionEventKind,
    ExecutionPhase,
    action_fingerprint,
    derive_execution_state,
    idempotency_key,
)
from fantasy_gm.domain.identity import ProviderRef
from fantasy_gm.domain.ids import AttemptId, DecisionId, new_attempt_id
from fantasy_gm.models.registry import ModelRegistry
from fantasy_gm.providers.interfaces import (
    ExecutionRequest,
    FantasyTransactionProvider,
    IdentityLookup,
    ProviderRejectedError,
    ReconciliationStatus,
)


class AttemptOutcome(StrEnum):
    EXECUTED = "executed"
    ALREADY_EXECUTED = "already_executed"
    RECONCILED_EXECUTED = "reconciled_executed"
    IN_PROGRESS_ELSEWHERE = "in_progress_elsewhere"
    REFUSED = "refused"
    FAILED = "failed"  # provider definitively rejected
    UNCERTAIN = "uncertain"  # outcome unknown; requires reconciliation before any retry


class ExecutionAttempt(DomainModel):
    decision_id: DecisionId
    outcome: AttemptOutcome
    reasons: tuple[str, ...] = ()
    provider_transaction_ref: str | None = None


class ExecutionService:
    def __init__(
        self,
        ledger: DecisionLedger,
        provider: FantasyTransactionProvider,
        identity: IdentityLookup,
        models: ModelRegistry,
        clock: Clock,
        *,
        execution_enabled: bool,
        worker_id: str,
        lease_ttl: timedelta = timedelta(minutes=2),
        provider_timeout: timedelta = timedelta(seconds=30),
        reconciliation_settle: timedelta = timedelta(minutes=2),
    ) -> None:
        if provider_timeout >= lease_ttl:
            raise ValueError("provider_timeout must be shorter than the claim lease")
        self._ledger = ledger
        self._provider = provider
        self._identity = identity
        self._models = models
        self._clock = clock
        self._enabled = execution_enabled
        self._worker = worker_id
        self._lease_ttl = lease_ttl
        self._timeout = provider_timeout
        # A request that timed out on our side may still be committed late by the platform.
        # NOT_FOUND is only trusted once this much time has passed since the attempt started.
        self._settle = reconciliation_settle

    async def execute(
        self,
        decision_id: DecisionId,
        policy: AutonomyPolicy,
        league_ref: ProviderRef,
        *,
        faab_budget: int | None = None,
        freshness: FreshnessContext | None = None,
    ) -> ExecutionAttempt:
        entry = await self._get(decision_id)
        decision = entry.decision.value
        action = decision.selected.action
        request = ExecutionRequest(
            league_ref=league_ref,
            decision_id=decision_id,
            action=action,
            idempotency_key=idempotency_key(decision_id, action),
            action_fingerprint=action_fingerprint(action),
        )
        state = derive_execution_state(entry.executions, self._clock.now())

        if state.phase is ExecutionPhase.SUCCEEDED:
            return self._result(
                decision_id, AttemptOutcome.ALREADY_EXECUTED, txn=state.provider_transaction_ref
            )
        if state.phase is ExecutionPhase.IN_FLIGHT:
            return self._result(
                decision_id,
                AttemptOutcome.IN_PROGRESS_ELSEWHERE,
                ("another worker holds a live claim",),
            )

        native_retry = False
        if state.phase is ExecutionPhase.UNCERTAIN:
            if self._provider.supports_native_idempotency:
                native_retry = True  # same key; the platform guarantees at-most-once
            else:
                resolved = await self._reconcile(entry, request, state.current_attempt_id)
                if resolved is not None:
                    return resolved
                entry = await self._get(decision_id)

        auth = authorize_execution(
            entry,
            policy,
            self._provider.capabilities,
            self._models,
            self._clock.now(),
            execution_enabled=self._enabled,
            faab_budget=faab_budget,
            freshness=freshness,
        )
        if not auth.allowed:
            await self._status(entry, DecisionStatus.EXECUTION_BLOCKED, "; ".join(auth.reasons))
            return self._result(decision_id, AttemptOutcome.REFUSED, auth.reasons)

        # ---- claim -------------------------------------------------------------------
        attempt_id = new_attempt_id()
        started = self._clock.now()
        claim = self._event(
            request,
            attempt_id,
            ExecutionEventKind.ATTEMPT_STARTED,
            lease_expires_at=started + self._lease_ttl,
            native_idempotency=native_retry or self._provider.supports_native_idempotency,
        )
        try:
            await asyncio.to_thread(
                self._ledger.append_execution_event, claim, len(entry.execution_events)
            )
        except (ConcurrentModificationError, InvalidTransitionError) as exc:
            return self._result(decision_id, AttemptOutcome.IN_PROGRESS_ELSEWHERE, (str(exc),))
        seq = len(entry.execution_events) + 1

        # ---- provider call (no DB transaction held) -----------------------------------
        try:
            receipt = await asyncio.wait_for(
                self._provider.execute(request, self._identity),
                timeout=self._timeout.total_seconds(),
            )
        except ProviderRejectedError as exc:
            await self._append(
                request, attempt_id, ExecutionEventKind.PROVIDER_REJECTED, seq, message=str(exc)
            )
            await self._status_after(
                decision_id,
                DecisionStatus.EXECUTION_FAILED,
                str(exc),
                ExecutionResult(
                    provider=self._provider.provider_name,
                    success=False,
                    executed_at=self._clock.now(),
                    message=str(exc),
                ),
            )
            return self._result(decision_id, AttemptOutcome.FAILED, (str(exc),))
        except Exception as exc:  # timeout, lost response, unexpected adapter error
            message = f"{type(exc).__name__}: {exc}"
            await self._append(
                request, attempt_id, ExecutionEventKind.OUTCOME_UNKNOWN, seq, message=message
            )
            await self._status_after(decision_id, DecisionStatus.EXECUTION_UNCERTAIN, message)
            return self._result(decision_id, AttemptOutcome.UNCERTAIN, (message,))

        await self._append(
            request,
            attempt_id,
            ExecutionEventKind.PROVIDER_CONFIRMED,
            seq,
            txn=receipt.provider_transaction_ref,
            message=receipt.message,
        )
        await self._status_after(
            decision_id,
            DecisionStatus.EXECUTED,
            "provider confirmed",
            ExecutionResult(
                provider=self._provider.provider_name,
                success=True,
                executed_at=receipt.executed_at,
                provider_transaction_ref=receipt.provider_transaction_ref,
                message=receipt.message,
            ),
        )
        return self._result(
            decision_id, AttemptOutcome.EXECUTED, txn=receipt.provider_transaction_ref
        )

    # ------------------------------------------------------------------ reconciliation

    async def _reconcile(
        self, entry: LedgerEntry, request: ExecutionRequest, attempt_id: AttemptId | None
    ) -> ExecutionAttempt | None:
        """Returns a final attempt result, or None when it is safe to start a new attempt."""
        decision_id = request.decision_id
        assert attempt_id is not None
        if ProviderCapability.READ not in self._provider.capabilities:
            return self._result(
                decision_id,
                AttemptOutcome.UNCERTAIN,
                ("cannot reconcile: provider has no READ capability",),
            )
        started_at = min(e.occurred_at for e in entry.executions if e.attempt_id == attempt_id)
        try:
            result = await asyncio.wait_for(
                self._provider.reconcile(
                    request, started_at - timedelta(minutes=1), self._identity
                ),
                timeout=self._timeout.total_seconds(),
            )
        except Exception as exc:
            return self._result(
                decision_id,
                AttemptOutcome.UNCERTAIN,
                (f"reconciliation failed: {type(exc).__name__}: {exc}",),
            )
        seq = len(entry.execution_events)
        if result.status is ReconciliationStatus.FOUND:
            await self._append(
                request,
                attempt_id,
                ExecutionEventKind.RECONCILED_EXECUTED,
                seq,
                txn=result.provider_transaction_ref,
                message=result.message,
            )
            await self._status_after(
                decision_id,
                DecisionStatus.EXECUTED,
                "reconciled against provider history",
                ExecutionResult(
                    provider=self._provider.provider_name,
                    success=True,
                    executed_at=result.checked_through,
                    provider_transaction_ref=result.provider_transaction_ref,
                    message="reconciled",
                ),
            )
            return self._result(
                decision_id, AttemptOutcome.RECONCILED_EXECUTED, txn=result.provider_transaction_ref
            )
        if result.status is ReconciliationStatus.NOT_FOUND:
            if self._clock.now() - started_at < self._settle:
                return self._result(
                    decision_id,
                    AttemptOutcome.UNCERTAIN,
                    (
                        "not found in provider history yet, but the settle window has not "
                        f"elapsed ({self._settle}); retry later",
                    ),
                )
            await self._append(
                request,
                attempt_id,
                ExecutionEventKind.RECONCILED_NOT_EXECUTED,
                seq,
                message=result.message,
            )
            await self._status_after(
                decision_id,
                DecisionStatus.EXECUTION_FAILED,
                "reconciled: previous attempt did not execute",
                ExecutionResult(
                    provider=self._provider.provider_name,
                    success=False,
                    executed_at=result.checked_through,
                    message="reconciled not executed",
                ),
            )
            return None
        return self._result(
            decision_id,
            AttemptOutcome.UNCERTAIN,
            (f"reconciliation undeterminable: {result.message}",),
        )

    # ------------------------------------------------------------------ helpers

    async def _get(self, decision_id: DecisionId) -> LedgerEntry:
        return await asyncio.to_thread(self._ledger.get, decision_id)

    def _event(
        self,
        request: ExecutionRequest,
        attempt_id: AttemptId,
        kind: ExecutionEventKind,
        *,
        lease_expires_at: datetime | None = None,
        native_idempotency: bool = False,
        txn: str | None = None,
        message: str = "",
    ) -> ExecutionEvent:
        return ExecutionEvent(
            decision_id=request.decision_id,
            attempt_id=attempt_id,
            kind=kind,
            occurred_at=self._clock.now(),
            provider=self._provider.provider_name,
            idempotency_key=request.idempotency_key,
            action_fingerprint=request.action_fingerprint,
            worker_id=self._worker,
            lease_expires_at=lease_expires_at,
            native_idempotency=native_idempotency,
            provider_transaction_ref=txn,
            message=message,
        )

    async def _append(
        self,
        request: ExecutionRequest,
        attempt_id: AttemptId,
        kind: ExecutionEventKind,
        seq: int,
        *,
        txn: str | None = None,
        message: str = "",
    ) -> None:
        event = self._event(request, attempt_id, kind, txn=txn, message=message)
        await asyncio.to_thread(self._ledger.append_execution_event, event, seq)

    async def _status(
        self,
        entry: LedgerEntry,
        status: DecisionStatus,
        reason: str,
        result: ExecutionResult | None = None,
    ) -> None:
        """Append a lifecycle status reflecting execution facts already in the attempt log.

        Retries on concurrent appends. Only EXECUTION_BLOCKED may be skipped when the
        transition is no longer valid; failing to record EXECUTED / UNCERTAIN / FAILED would
        hide what happened, so that raises.
        """
        decision_id = entry.decision.value.decision_id
        for _ in range(5):
            if status not in TRANSITIONS[entry.current_status]:
                if status is DecisionStatus.EXECUTION_BLOCKED:
                    return
                raise LedgerError(
                    f"cannot record {status} after {entry.current_status}; the execution log "
                    "is authoritative and needs operator attention"
                )
            event = DecisionStatusEvent(
                decision_id=decision_id,
                status=status,
                occurred_at=self._clock.now(),
                actor=Actor(kind=ActorKind.SYSTEM, actor_id=f"execution:{self._worker}"),
                reason=reason,
                execution_result=result,
            )
            try:
                await asyncio.to_thread(
                    self._ledger.append_status, event, len(entry.status_history)
                )
                return
            except ConcurrentModificationError:
                entry = await self._get(decision_id)
        raise LedgerError(f"could not record {status}: persistent concurrent modification")

    async def _status_after(
        self,
        decision_id: DecisionId,
        status: DecisionStatus,
        reason: str,
        result: ExecutionResult | None = None,
    ) -> None:
        await self._status(await self._get(decision_id), status, reason, result)

    @staticmethod
    def _result(
        decision_id: DecisionId,
        outcome: AttemptOutcome,
        reasons: tuple[str, ...] = (),
        txn: str | None = None,
    ) -> ExecutionAttempt:
        return ExecutionAttempt(
            decision_id=decision_id, outcome=outcome, reasons=reasons, provider_transaction_ref=txn
        )
