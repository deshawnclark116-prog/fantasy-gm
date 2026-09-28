"""Provider identifier mapping (anti-corruption layer between providers and the domain).

Mappings are an append-only history of *assertions*. A mistaken mapping is corrected by a new
assertion that explicitly supersedes the old one with a reason -- never by silent reassignment --
so a historical run can reconstruct the mapping that was believed at its cutoff.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.ids import IdentityEventId, new_identity_event_id
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
    NAME_MATCH = "name_match"  # heuristic; can never be VERIFIED directly


class MappingStatus(StrEnum):
    VERIFIED = "verified"  # usable by ingestion and execution
    UNVERIFIED = "unverified"  # candidate only; ingestion refuses it
    CONFLICTED = "conflicted"  # known ambiguity; ingestion refuses it
    RETRACTED = "retracted"  # mapping withdrawn; ref maps to nothing


class IdentityMappingEvent(DomainModel):
    event_id: IdentityEventId = Field(default_factory=new_identity_event_id)
    ref: ProviderRef
    internal_id: str | None
    status: MappingStatus
    method: MappingMethod
    confidence: float = Field(ge=0.0, le=1.0)
    asserted_by: str = Field(min_length=1)  # provenance: crosswalk name/version, user, job
    evidence: str = ""
    supersedes_event_id: IdentityEventId | None = None
    reason: str = ""

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.status is MappingStatus.RETRACTED) != (self.internal_id is None):
            raise ValueError("internal_id must be None exactly when the mapping is retracted")
        if self.method is MappingMethod.NAME_MATCH and self.status is MappingStatus.VERIFIED:
            raise ValueError("name matches cannot be VERIFIED without human/crosswalk confirmation")
        if self.supersedes_event_id is not None and not self.reason.strip():
            raise ValueError("a correction must state its reason")
        return self


class MappingResolution(DomainModel):
    ref: ProviderRef
    internal_id: str | None
    status: MappingStatus
    event_id: IdentityEventId
    recorded_at: UtcDatetime


def resolve_mapping(
    history: Iterable[tuple[IdentityMappingEvent, UtcDatetime]], as_of: UtcDatetime | None
) -> MappingResolution | None:
    """Mapping believed at ``as_of`` (by registry recording time); latest if ``as_of`` is None."""
    best: tuple[IdentityMappingEvent, UtcDatetime] | None = None
    for event, recorded_at in history:
        if as_of is not None and recorded_at > as_of:
            continue
        if best is None or recorded_at >= best[1]:
            best = (event, recorded_at)
    if best is None:
        return None
    event, recorded_at = best
    return MappingResolution(
        ref=event.ref,
        internal_id=event.internal_id,
        status=event.status,
        event_id=event.event_id,
        recorded_at=recorded_at,
    )
