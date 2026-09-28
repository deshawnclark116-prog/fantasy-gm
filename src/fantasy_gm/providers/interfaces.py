"""Provider protocols (async).

External IO is asynchronous so live draft events, late inactive news and transaction handling
can be serviced concurrently. Pure domain calculations stay synchronous; CPU-heavy simulation is
offloaded to a worker boundary (``fantasy_gm.simulation.offload``), never run on the event loop.

Read methods return provider-shaped records carrying provider identifiers and timestamp-trust
metadata; they never return domain objects keyed by internal IDs.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol, runtime_checkable

from fantasy_gm.domain.actions import Action
from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.capabilities import ProviderCapability
from fantasy_gm.domain.identity import EntityType, ProviderRef
from fantasy_gm.domain.ids import DecisionId
from fantasy_gm.domain.nfl import SeasonPhase
from fantasy_gm.domain.time import UtcDatetime
from fantasy_gm.providers.records import (
    ProviderADPRecord,
    ProviderFantasyTransaction,
    ProviderGameRecord,
    ProviderInjuryRecord,
    ProviderLeagueSnapshot,
    ProviderPlayerRecord,
    ProviderStatLine,
    ProviderTeamRecord,
    ProviderUsageRecord,
)


class Provider(Protocol):
    @property
    def provider_name(self) -> str: ...


@runtime_checkable
class NFLPlayerProvider(Provider, Protocol):
    async def list_teams(self) -> Sequence[ProviderTeamRecord]: ...

    async def list_players(
        self, updated_since: UtcDatetime | None = None
    ) -> Sequence[ProviderPlayerRecord]: ...


@runtime_checkable
class UsageProvider(Provider, Protocol):
    async def usage(
        self, season: int, week: int, phase: SeasonPhase
    ) -> Sequence[ProviderUsageRecord]: ...

    async def stat_lines(
        self, season: int, week: int, phase: SeasonPhase
    ) -> Sequence[ProviderStatLine]: ...


@runtime_checkable
class InjuryProvider(Provider, Protocol):
    async def injury_reports(self, season: int, week: int) -> Sequence[ProviderInjuryRecord]: ...


@runtime_checkable
class ScheduleProvider(Provider, Protocol):
    async def schedule(self, season: int) -> Sequence[ProviderGameRecord]: ...


@runtime_checkable
class MarketADPProvider(Provider, Protocol):
    async def adp(self, market: str, as_of: UtcDatetime) -> Sequence[ProviderADPRecord]: ...


@runtime_checkable
class FantasyLeagueProvider(Provider, Protocol):
    """Read access to league settings, rosters, eligibility and draft state."""

    async def league_snapshot(self, league_ref: ProviderRef) -> ProviderLeagueSnapshot: ...


class IdentityLookup(Protocol):
    """Internal-ID -> provider-ID translation (current VERIFIED mapping only)."""

    def external_id(self, internal_id: str, provider: str, entity_type: EntityType) -> str: ...


# --------------------------------------------------------------------------- execution contract


class ExecutionRequest(DomainModel):
    league_ref: ProviderRef
    decision_id: DecisionId
    action: Action
    # Stable across retries of the same decision. Adapters for platforms with native
    # idempotency MUST forward it; the platform must then execute at most once per key.
    idempotency_key: str
    action_fingerprint: str


class ProviderExecutionReceipt(DomainModel):
    executed_at: UtcDatetime
    provider_transaction_ref: str | None = None
    message: str = ""


class ProviderRejectedError(Exception):
    """The platform definitively refused the action: it was NOT executed."""


class ProviderOutcomeUnknownError(Exception):
    """The action may or may not have executed (timeout, dropped connection, 5xx after send)."""


class ReconciliationStatus(StrEnum):
    FOUND = "found"  # the action appears in platform history: it executed
    NOT_FOUND = "not_found"  # history is authoritative and complete for the window: it did not
    UNDETERMINABLE = "undeterminable"  # cannot tell; never retry on this


class ReconciliationResult(DomainModel):
    status: ReconciliationStatus
    provider_transaction_ref: str | None = None
    checked_through: UtcDatetime
    message: str = ""


@runtime_checkable
class FantasyTransactionProvider(Provider, Protocol):
    """League transactions: read history, reconcile, and (capability permitting) write.

    ``capabilities`` is the authoritative statement of what may be executed. The autonomy layer
    refuses any action whose required capability is missing *before* calling ``execute``.
    """

    @property
    def capabilities(self) -> frozenset[ProviderCapability]: ...

    @property
    def supports_native_idempotency(self) -> bool: ...

    async def transactions(
        self, league_ref: ProviderRef, since: UtcDatetime | None = None
    ) -> Sequence[ProviderFantasyTransaction]: ...

    async def execute(
        self, request: ExecutionRequest, identity: IdentityLookup
    ) -> ProviderExecutionReceipt: ...

    async def reconcile(
        self, request: ExecutionRequest, since: UtcDatetime, identity: IdentityLookup
    ) -> ReconciliationResult: ...
