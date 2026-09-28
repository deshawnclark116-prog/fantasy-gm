"""Provider identifier mapping (anti-corruption layer between providers and the domain)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import Field, StringConstraints

from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.time import UtcDatetime

ProviderName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]


class EntityType(StrEnum):
    PLAYER = "player"
    NFL_TEAM = "nfl_team"
    GAME = "game"
    COACH = "coach"
    LEAGUE = "league"
    FANTASY_TEAM = "fantasy_team"


class ProviderRef(DomainModel):
    """A provider's identifier for an entity. Never used as a domain primary key."""

    provider: ProviderName
    entity_type: EntityType
    external_id: Annotated[str, StringConstraints(min_length=1, max_length=256)]

    def key(self) -> tuple[str, str, str]:
        return (self.provider, self.entity_type.value, self.external_id)


class MappingMethod(StrEnum):
    PROVIDER_CROSSWALK = "provider_crosswalk"  # the provider itself publishes the link
    CURATED_CROSSWALK = "curated_crosswalk"  # a maintained third-party / in-house crosswalk
    MANUAL = "manual"
    NAME_MATCH = "name_match"  # heuristic; must carry < 1.0 confidence


class ProviderIdMapping(DomainModel):
    internal_id: str
    ref: ProviderRef
    method: MappingMethod
    confidence: float = Field(ge=0.0, le=1.0)
    observed_at: UtcDatetime
