"""Bitemporal observation base.

Every time-varying fact is an ``Observation`` with three timestamps:

* ``effective_at`` -- when the fact is/was true in the world.
* ``observed_at``  -- when the fact became publicly observable, with its trust level recorded in
  ``source.timestamp_quality`` and the provider's raw value preserved in ``source.raw_timestamp``.
* ``source.ingested_at`` -- when *this system* received it.

Revisions are new observations sharing the same ``fact_key``; nothing is updated in place.
See ADR 0002 and ADR 0010.
"""

from __future__ import annotations

from abc import abstractmethod
from datetime import timedelta
from typing import ClassVar, Self

from pydantic import Field, model_validator

from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.identity import ProviderName
from fantasy_gm.domain.ids import LeagueId, NFLTeamId, ObservationId, PlayerId, new_observation_id
from fantasy_gm.domain.time import INGESTION_BOUND_QUALITIES, TimestampQuality, UtcDatetime

# Tolerated clock skew between a provider's publication clock and our ingestion clock.
MAX_PUBLICATION_AFTER_INGESTION = timedelta(minutes=5)


class SourceRef(DomainModel):
    provider: ProviderName
    provider_record_id: str | None = None
    ingested_at: UtcDatetime
    timestamp_quality: TimestampQuality
    # The provider's original timestamp value and field name, verbatim (never reinterpreted).
    raw_timestamp: str | None = None
    raw_timestamp_field: str | None = None
    # Identity-mapping events used to attribute this record to internal IDs (for audits and
    # re-attribution after an identity correction).
    identity_basis: tuple[str, ...] = ()


class Observation(DomainModel):
    kind: ClassVar[str]
    schema_version: ClassVar[int] = 1

    observation_id: ObservationId = Field(default_factory=new_observation_id)
    observed_at: UtcDatetime
    effective_at: UtcDatetime
    source: SourceRef

    @model_validator(mode="after")
    def _timestamp_honesty(self) -> Self:
        quality = self.source.timestamp_quality
        if quality in INGESTION_BOUND_QUALITIES and self.observed_at != self.source.ingested_at:
            raise ValueError(
                f"timestamp quality {quality} cannot carry a publication time different from "
                "ingestion time (no fabricated publication timestamps)"
            )
        if self.observed_at > self.source.ingested_at + MAX_PUBLICATION_AFTER_INGESTION:
            raise ValueError("observed_at is after ingested_at: timestamp semantics are wrong")
        return self

    @abstractmethod
    def fact_key(self) -> str:
        """Identity of the *fact* this observation describes. Revisions share a fact_key."""

    def subject_player_id(self) -> PlayerId | None:
        return None

    def subject_team_id(self) -> NFLTeamId | None:
        return None

    def subject_league_id(self) -> LeagueId | None:
        return None
