"""Bitemporal observation base.

Every time-varying fact is an ``Observation`` with three timestamps:

* ``effective_at`` -- when the fact is/was true in the world (e.g. kickoff of the game a usage
  line describes, the date a contract was signed).
* ``observed_at``  -- when the fact became publicly observable (provider publication time).
* ``source.ingested_at`` -- when *this system* received it.

Revisions (stat corrections, updated depth charts for the same date, ...) are new observations
sharing the same ``fact_key``; nothing is ever updated in place. See ADR 0002.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import ClassVar

from pydantic import Field

from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.identity import ProviderName
from fantasy_gm.domain.ids import NFLTeamId, ObservationId, PlayerId, new_observation_id
from fantasy_gm.domain.time import UtcDatetime


class SourceRef(DomainModel):
    provider: ProviderName
    provider_record_id: str | None = None
    ingested_at: UtcDatetime


class Observation(DomainModel):
    kind: ClassVar[str]

    observation_id: ObservationId = Field(default_factory=new_observation_id)
    observed_at: UtcDatetime
    effective_at: UtcDatetime
    source: SourceRef

    @abstractmethod
    def fact_key(self) -> str:
        """Identity of the *fact* this observation describes. Revisions share a fact_key."""

    def subject_player_id(self) -> PlayerId | None:
        return None

    def subject_team_id(self) -> NFLTeamId | None:
        return None
