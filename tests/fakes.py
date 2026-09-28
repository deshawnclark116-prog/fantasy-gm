"""Async fake fantasy platform for execution tests."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from fantasy_gm.domain.capabilities import ProviderCapability
from fantasy_gm.domain.clock import Clock
from fantasy_gm.domain.identity import EntityType, ProviderRef
from fantasy_gm.providers.interfaces import (
    ExecutionRequest,
    IdentityLookup,
    ProviderExecutionReceipt,
    ProviderOutcomeUnknownError,
    ProviderRejectedError,
    ReconciliationResult,
    ReconciliationStatus,
)
from fantasy_gm.providers.records import ProviderFantasyTransaction

WRITE_CAPS = frozenset({ProviderCapability.READ, ProviderCapability.LINEUP_WRITE})
READ_ONLY = frozenset({ProviderCapability.READ})
LEAGUE_REF = ProviderRef(provider="fake_platform", entity_type=EntityType.LEAGUE, external_id="L1")


@dataclass
class PlatformTxn:
    ref: str
    fingerprint: str
    idempotency_key: str


@dataclass
class FakePlatform:
    """Scriptable platform. Behaviours per execute call (consumed in order, default "ok"):

    ok            -- executes and responds
    reject        -- definitively refuses (not executed)
    lose_response -- EXECUTES, then the response is lost (timeout-like error)
    fail_before   -- times out WITHOUT executing
    hang          -- never responds (exercises the service's own timeout); does not execute
    """

    clock: Clock
    capabilities: frozenset[ProviderCapability] = WRITE_CAPS
    native_idempotency: bool = False
    delay: float = 0.0
    script: list[str] = field(default_factory=list)
    reconcile_status: ReconciliationStatus | None = None  # force a reconciliation answer
    history: list[PlatformTxn] = field(default_factory=list)
    execute_calls: int = 0
    reconcile_calls: int = 0

    @property
    def provider_name(self) -> str:
        return "fake_platform"

    @property
    def supports_native_idempotency(self) -> bool:
        return self.native_idempotency

    async def transactions(
        self, league_ref: ProviderRef, since: datetime | None = None
    ) -> Sequence[ProviderFantasyTransaction]:
        return ()

    async def execute(
        self, request: ExecutionRequest, identity: IdentityLookup
    ) -> ProviderExecutionReceipt:
        self.execute_calls += 1
        behaviour = self.script.pop(0) if self.script else "ok"
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.native_idempotency:
            for txn in self.history:
                if txn.idempotency_key == request.idempotency_key:
                    return ProviderExecutionReceipt(
                        executed_at=self.clock.now(),
                        provider_transaction_ref=txn.ref,
                        message="deduplicated by idempotency key",
                    )
        if behaviour == "reject":
            raise ProviderRejectedError("roster lock")
        if behaviour == "fail_before":
            raise ProviderOutcomeUnknownError("timeout before commit")
        if behaviour == "hang":
            await asyncio.sleep(3600)
        txn = PlatformTxn(
            ref=f"txn-{len(self.history) + 1}",
            fingerprint=request.action_fingerprint,
            idempotency_key=request.idempotency_key,
        )
        self.history.append(txn)
        if behaviour == "lose_response":
            raise ProviderOutcomeUnknownError("connection reset after commit")
        return ProviderExecutionReceipt(
            executed_at=self.clock.now(), provider_transaction_ref=txn.ref
        )

    async def reconcile(
        self, request: ExecutionRequest, since: datetime, identity: IdentityLookup
    ) -> ReconciliationResult:
        self.reconcile_calls += 1
        if self.reconcile_status is ReconciliationStatus.UNDETERMINABLE:
            return ReconciliationResult(
                status=ReconciliationStatus.UNDETERMINABLE,
                checked_through=self.clock.now(),
                message="history endpoint unavailable",
            )
        for txn in self.history:
            if txn.fingerprint == request.action_fingerprint:
                return ReconciliationResult(
                    status=ReconciliationStatus.FOUND,
                    provider_transaction_ref=txn.ref,
                    checked_through=self.clock.now(),
                )
        return ReconciliationResult(
            status=ReconciliationStatus.NOT_FOUND, checked_through=self.clock.now()
        )

    @property
    def executed(self) -> int:
        return len(self.history)


class NullIdentity:
    def external_id(self, internal_id: str, provider: str, entity_type: EntityType) -> str:
        return internal_id
