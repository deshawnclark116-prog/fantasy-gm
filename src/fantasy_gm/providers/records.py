"""Provider-shaped records.

Providers speak in *their* identifiers (``ProviderRef``). These records are translated into
domain observations by ``fantasy_gm.providers.ingestion`` using the identity registry; a provider
never sees or mints internal IDs.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import Field

from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.identity import ProviderRef
from fantasy_gm.domain.league import StatKey
from fantasy_gm.domain.nfl import (
    InjuryDesignation,
    InjuryReportType,
    Position,
    PracticeParticipation,
    RosterStatus,
    SeasonPhase,
    UsageMetric,
)
from fantasy_gm.domain.time import UtcDatetime


class ProviderRecord(DomainModel):
    observed_at: UtcDatetime
    effective_at: UtcDatetime
    provider_record_id: str | None = None


class ProviderPlayerRecord(ProviderRecord):
    player_ref: ProviderRef
    full_name: str
    positions: frozenset[Position]
    birth_date: date | None = None
    team_ref: ProviderRef | None = None
    roster_status: RosterStatus | None = None


class ProviderTeamRecord(ProviderRecord):
    team_ref: ProviderRef
    abbreviation: str
    full_name: str


class ProviderGameRecord(ProviderRecord):
    game_ref: ProviderRef
    season: int
    week: int
    phase: SeasonPhase
    home_team_ref: ProviderRef
    away_team_ref: ProviderRef


class ProviderUsageRecord(ProviderRecord):
    player_ref: ProviderRef
    team_ref: ProviderRef
    game_ref: ProviderRef
    season: int
    week: int
    phase: SeasonPhase
    metrics: dict[UsageMetric, float]


class ProviderInjuryRecord(ProviderRecord):
    player_ref: ProviderRef
    game_ref: ProviderRef | None = None
    designation: InjuryDesignation
    practice: PracticeParticipation | None = None
    body_part: str | None = None
    report_type: InjuryReportType


class ProviderStatLine(ProviderRecord):
    player_ref: ProviderRef
    game_ref: ProviderRef
    stats: dict[StatKey, Decimal]


class ProviderADPRecord(ProviderRecord):
    player_ref: ProviderRef
    market: str
    adp: float = Field(gt=0)
    adp_stdev: float | None = None
    sample_size: int | None = None


class ProviderLeagueSnapshot(ProviderRecord):
    """Raw league settings/rosters as a provider reports them; the league adapter is responsible
    for translating into ``fantasy_gm.domain.league`` types."""

    league_ref: ProviderRef
    raw: dict[str, object]


class ProviderFantasyTransaction(ProviderRecord):
    league_ref: ProviderRef
    transaction_ref: str
    transaction_type: str
    team_refs: tuple[ProviderRef, ...]
    added_player_refs: tuple[ProviderRef, ...] = ()
    dropped_player_refs: tuple[ProviderRef, ...] = ()
    faab_bid: int | None = None
