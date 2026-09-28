"""Provider protocols.

All protocols are synchronous in v0.1 (see REVIEW_NOTES: async is an open question). Read
methods return provider-shaped records with provider publication timestamps; they never return
domain objects keyed by internal IDs.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from fantasy_gm.domain.actions import Action
from fantasy_gm.domain.capabilities import ProviderCapability
from fantasy_gm.domain.decision import ExecutionResult
from fantasy_gm.domain.identity import EntityType, ProviderRef
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
    def list_teams(self) -> Sequence[ProviderTeamRecord]: ...

    def list_players(
        self, updated_since: UtcDatetime | None = None
    ) -> Sequence[ProviderPlayerRecord]: ...


@runtime_checkable
class UsageProvider(Provider, Protocol):
    def usage(
        self, season: int, week: int, phase: SeasonPhase
    ) -> Sequence[ProviderUsageRecord]: ...

    def stat_lines(
        self, season: int, week: int, phase: SeasonPhase
    ) -> Sequence[ProviderStatLine]: ...


@runtime_checkable
class InjuryProvider(Provider, Protocol):
    def injury_reports(self, season: int, week: int) -> Sequence[ProviderInjuryRecord]: ...


@runtime_checkable
class ScheduleProvider(Provider, Protocol):
    def schedule(self, season: int) -> Sequence[ProviderGameRecord]: ...


@runtime_checkable
class MarketADPProvider(Provider, Protocol):
    def adp(self, market: str, as_of: UtcDatetime) -> Sequence[ProviderADPRecord]: ...


@runtime_checkable
class FantasyLeagueProvider(Provider, Protocol):
    """Read access to league settings, rosters and draft state."""

    def league_snapshot(self, league_ref: ProviderRef) -> ProviderLeagueSnapshot: ...


class IdentityLookup(Protocol):
    """Internal-ID -> provider-ID translation handed to providers at execution time."""

    def external_id(self, internal_id: str, provider: str, entity_type: EntityType) -> str: ...


@runtime_checkable
class FantasyTransactionProvider(Provider, Protocol):
    """League transactions: read history, and (capability permitting) write.

    ``capabilities`` is the authoritative statement of what may be executed. The autonomy layer
    refuses any action whose required capability is missing *before* calling ``execute``.
    """

    @property
    def capabilities(self) -> frozenset[ProviderCapability]: ...

    def transactions(
        self, league_ref: ProviderRef, since: UtcDatetime | None = None
    ) -> Sequence[ProviderFantasyTransaction]: ...

    def execute(
        self, league_ref: ProviderRef, action: Action, identity: IdentityLookup
    ) -> ExecutionResult: ...
