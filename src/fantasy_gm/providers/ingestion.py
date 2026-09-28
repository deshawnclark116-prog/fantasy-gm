"""Translate provider records into domain observations (pure; no IO).

Only VERIFIED identity mappings are accepted; the mapping events used are recorded on the
observation's ``source.identity_basis`` so identity corrections can later find what they affect.
"""

from __future__ import annotations

from fantasy_gm.domain.identity import ProviderRef
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


class _Resolver:
    def __init__(self, registry: IdentityRegistry, as_of: UtcDatetime) -> None:
        self._registry = registry
        self._as_of = as_of
        self.basis: list[str] = []

    def __call__(self, ref: ProviderRef) -> str:
        internal, event_id = require(self._registry, ref, self._as_of)
        self.basis.append(event_id)
        return internal


def _source(
    provider: str, record: ProviderRecord, ingested_at: UtcDatetime, basis: list[str]
) -> SourceRef:
    return SourceRef(
        provider=provider,
        provider_record_id=record.provider_record_id,
        ingested_at=ingested_at,
        timestamp_quality=record.timestamp_quality,
        raw_timestamp=record.raw_timestamp,
        raw_timestamp_field=record.raw_timestamp_field,
        identity_basis=tuple(dict.fromkeys(basis)),
    )


def map_usage(
    record: ProviderUsageRecord, registry: IdentityRegistry, ingested_at: UtcDatetime
) -> UsageSnapshot:
    r = _Resolver(registry, ingested_at)
    player, team, game = r(record.player_ref), r(record.team_ref), r(record.game_ref)
    return UsageSnapshot(
        observed_at=record.observed_at,
        effective_at=record.effective_at,
        source=_source(record.player_ref.provider, record, ingested_at, r.basis),
        player_id=PlayerId(player),
        team_id=NFLTeamId(team),
        game_id=GameId(game),
        season=record.season,
        week=record.week,
        phase=record.phase,
        metrics=dict(record.metrics),
    )


def map_injury(
    record: ProviderInjuryRecord, registry: IdentityRegistry, ingested_at: UtcDatetime
) -> InjuryStatus:
    r = _Resolver(registry, ingested_at)
    player = r(record.player_ref)
    game = None if record.game_ref is None else GameId(r(record.game_ref))
    return InjuryStatus(
        observed_at=record.observed_at,
        effective_at=record.effective_at,
        source=_source(record.player_ref.provider, record, ingested_at, r.basis),
        player_id=PlayerId(player),
        game_id=game,
        designation=record.designation,
        practice=record.practice,
        body_part=record.body_part,
        report_type=record.report_type,
    )


def map_adp(
    record: ProviderADPRecord, registry: IdentityRegistry, ingested_at: UtcDatetime
) -> MarketADPObservation:
    r = _Resolver(registry, ingested_at)
    player = r(record.player_ref)
    return MarketADPObservation(
        observed_at=record.observed_at,
        effective_at=record.effective_at,
        source=_source(record.player_ref.provider, record, ingested_at, r.basis),
        player_id=PlayerId(player),
        market=record.market,
        adp=record.adp,
        adp_stdev=record.adp_stdev,
        sample_size=record.sample_size,
    )
