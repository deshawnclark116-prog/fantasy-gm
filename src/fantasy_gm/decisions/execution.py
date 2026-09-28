"""Execution service: the only code path that calls a provider's write methods.

Separated from intelligence: it never decides *what* to do, only whether an already-recorded,
approved decision may be carried out, and it records every attempt in the ledger.
"""

from __future__ import annotations

from datetime import datetime

from fantasy_gm.decisions.autonomy import authorize_execution
from fantasy_gm.decisions.ledger import TRANSITIONS, DecisionLedger
from fantasy_gm.domain.autonomy import AutonomyPolicy
from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.decision import (
    Actor,
    ActorKind,
    DecisionStatus,
    DecisionStatusEvent,
    ExecutionResult,
)
from fantasy_gm.domain.identity import ProviderRef
from fantasy_gm.domain.ids import DecisionId
from fantasy_gm.domain.league import League
from fantasy_gm.providers.interfaces import FantasyTransactionProvider, IdentityLookup

_SYSTEM = Actor(kind=ActorKind.SYSTEM, actor_id="execution_service")


class ExecutionAttempt(DomainModel):
    decision_id: DecisionId
    executed: bool
    refused: bool
    reasons: tuple[str, ...] = ()
    result: ExecutionResult | None = None


class ExecutionService:
    def __init__(
        self,
        ledger: DecisionLedger,
        provider: FantasyTransactionProvider,
        identity: IdentityLookup,
        *,
        execution_enabled: bool,
    ) -> None:
        self._ledger = ledger
        self._provider = provider
        self._identity = identity
        self._execution_enabled = execution_enabled

    def execute(
        self,
        decision_id: DecisionId,
        policy: AutonomyPolicy,
        league: League,
        league_ref: ProviderRef,
        now: datetime,
    ) -> ExecutionAttempt:
        entry = self._ledger.get(decision_id)
        auth = authorize_execution(
            entry,
            policy,
            self._provider.capabilities,
            now,
            execution_enabled=self._execution_enabled,
            league=league,
        )
        if not auth.allowed:
            if DecisionStatus.EXECUTION_BLOCKED in TRANSITIONS[entry.current_status]:
                self._ledger.append_status(
                    DecisionStatusEvent(
                        decision_id=decision_id,
                        status=DecisionStatus.EXECUTION_BLOCKED,
                        occurred_at=now,
                        actor=_SYSTEM,
                        reason="; ".join(auth.reasons),
                    )
                )
            return ExecutionAttempt(
                decision_id=decision_id, executed=False, refused=True, reasons=auth.reasons
            )

        try:
            result = self._provider.execute(
                league_ref, entry.decision.selected.action, self._identity
            )
        except Exception as exc:  # provider failures are recorded, never swallowed silently
            result = ExecutionResult(
                provider=self._provider.provider_name,
                success=False,
                executed_at=now,
                message=f"{type(exc).__name__}: {exc}",
            )
        status = DecisionStatus.EXECUTED if result.success else DecisionStatus.EXECUTION_FAILED
        self._ledger.append_status(
            DecisionStatusEvent(
                decision_id=decision_id,
                status=status,
                occurred_at=max(now, result.executed_at),
                actor=_SYSTEM,
                execution_result=result,
            )
        )
        return ExecutionAttempt(
            decision_id=decision_id, executed=result.success, refused=False, result=result
        )
