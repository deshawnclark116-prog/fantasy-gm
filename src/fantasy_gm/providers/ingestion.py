"""Translate provider records into domain observations (pure; no IO)."""

from __future__ import annotations

from fantasy_gm.domain.ids import GameId, NFLTeamId, PlayerId
from fantasy_gm.domain.market import MarketADPObservation
from fantasy_gm.domain.nfl import InjuryStatus, UsageSnapshot
from fantasy_gm.domain.observation import SourceRef
from fantasy_gm.domain.time import UtcDatetime
from fantasy_gm.providers.identity import IdentityRegistry, require
from fantasy_gm.providers.records import (
    ProviderADPRecord,
    ProviderInjuryRecord,
    ProviderRecord,
    ProviderUsageRecord,
)


def _source(provider: str, record: ProviderRecord, ingested_at: UtcDatetime) -> SourceRef:
    return SourceRef(
        provider=provider, provider_record_id=record.provider_record_id, ingested_at=ingested_at
    )


def map_usage(
    record: ProviderUsageRecord, registry: IdentityRegistry, ingested_at: UtcDatetime
) -> UsageSnapshot:
    return UsageSnapshot(
        observed_at=record.observed_at,
        effective_at=record.effective_at,
        source=_source(record.player_ref.provider, record, ingested_at),
        player_id=PlayerId(require(registry, record.player_ref)),
        team_id=NFLTeamId(require(registry, record.team_ref)),
        game_id=GameId(require(registry, record.game_ref)),
        season=record.season,
        week=record.week,
        phase=record.phase,
        metrics=dict(record.metrics),
    )


def map_injury(
    record: ProviderInjuryRecord, registry: IdentityRegistry, ingested_at: UtcDatetime
) -> InjuryStatus:
    return InjuryStatus(
        observed_at=record.observed_at,
        effective_at=record.effective_at,
        source=_source(record.player_ref.provider, record, ingested_at),
        player_id=PlayerId(require(registry, record.player_ref)),
        game_id=None if record.game_ref is None else GameId(require(registry, record.game_ref)),
        designation=record.designation,
        practice=record.practice,
        body_part=record.body_part,
        report_type=record.report_type,
    )


def map_adp(
    record: ProviderADPRecord, registry: IdentityRegistry, ingested_at: UtcDatetime
) -> MarketADPObservation:
    return MarketADPObservation(
        observed_at=record.observed_at,
        effective_at=record.effective_at,
        source=_source(record.player_ref.provider, record, ingested_at),
        player_id=PlayerId(require(registry, record.player_ref)),
        market=record.market,
        adp=record.adp,
        adp_stdev=record.adp_stdev,
        sample_size=record.sample_size,
    )
