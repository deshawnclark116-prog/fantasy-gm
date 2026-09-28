"""In-memory fake providers for tests."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from fantasy_gm.domain.actions import Action
from fantasy_gm.domain.capabilities import ProviderCapability
from fantasy_gm.domain.decision import ExecutionResult
from fantasy_gm.domain.identity import ProviderRef
from fantasy_gm.providers.interfaces import IdentityLookup
from fantasy_gm.providers.records import ProviderFantasyTransaction


class FakeTransactionProvider:
    def __init__(
        self,
        capabilities: frozenset[ProviderCapability],
        executed_at: datetime,
        fail_with: Exception | None = None,
    ) -> None:
        self._capabilities = capabilities
        self._executed_at = executed_at
        self._fail_with = fail_with
        self.calls: list[Action] = []

    @property
    def provider_name(self) -> str:
        return "fake_platform"

    @property
    def capabilities(self) -> frozenset[ProviderCapability]:
        return self._capabilities

    def transactions(
        self, league_ref: ProviderRef, since: datetime | None = None
    ) -> Sequence[ProviderFantasyTransaction]:
        return ()

    def execute(
        self, league_ref: ProviderRef, action: Action, identity: IdentityLookup
    ) -> ExecutionResult:
        self.calls.append(action)
        if self._fail_with is not None:
            raise self._fail_with
        return ExecutionResult(
            provider=self.provider_name,
            success=True,
            executed_at=self._executed_at,
            provider_transaction_ref="txn-1",
        )


class NullIdentity:
    def external_id(self, internal_id: str, provider: str, entity_type: object) -> str:
        return internal_id
