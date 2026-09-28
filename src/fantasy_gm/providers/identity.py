"""Identity registry: provider ref <-> stable internal ID.

Invariants:
* one provider ref maps to exactly one internal ID (conflicting links raise);
* one internal ID may carry refs from many providers, but at most one ref per
  (provider, entity_type) -- two Sleeper IDs for the same player is a data problem to surface,
  not silently accept;
* unknown refs are never auto-created during ingestion (``UnresolvedIdentityError``).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from fantasy_gm.domain.identity import EntityType, ProviderIdMapping, ProviderRef


class IdentityConflictError(ValueError):
    pass


class UnresolvedIdentityError(LookupError):
    def __init__(self, ref: ProviderRef) -> None:
        super().__init__(
            f"no internal id mapped for {ref.provider}:{ref.entity_type}:{ref.external_id}"
        )
        self.ref = ref


class IdentityRegistry(Protocol):
    def link(self, mapping: ProviderIdMapping) -> None: ...

    def resolve(self, ref: ProviderRef) -> str | None: ...

    def mappings_for(self, internal_id: str) -> list[ProviderIdMapping]: ...


def require(registry: IdentityRegistry, ref: ProviderRef) -> str:
    internal = registry.resolve(ref)
    if internal is None:
        raise UnresolvedIdentityError(ref)
    return internal


class InMemoryIdentityRegistry:
    def __init__(self, mappings: Iterable[ProviderIdMapping] = ()) -> None:
        self._by_ref: dict[tuple[str, str, str], ProviderIdMapping] = {}
        self._by_internal: dict[str, list[ProviderIdMapping]] = {}
        for m in mappings:
            self.link(m)

    def link(self, mapping: ProviderIdMapping) -> None:
        existing = self._by_ref.get(mapping.ref.key())
        if existing is not None:
            if existing.internal_id != mapping.internal_id:
                raise IdentityConflictError(
                    f"{mapping.ref.key()} already mapped to {existing.internal_id}"
                )
            return  # idempotent re-link
        for other in self._by_internal.get(mapping.internal_id, []):
            same_slot = (other.ref.provider, other.ref.entity_type) == (
                mapping.ref.provider,
                mapping.ref.entity_type,
            )
            if same_slot:
                raise IdentityConflictError(
                    f"{mapping.internal_id} already has a {mapping.ref.provider} "
                    f"{mapping.ref.entity_type} id ({other.ref.external_id})"
                )
        self._by_ref[mapping.ref.key()] = mapping
        self._by_internal.setdefault(mapping.internal_id, []).append(mapping)

    def resolve(self, ref: ProviderRef) -> str | None:
        found = self._by_ref.get(ref.key())
        return None if found is None else found.internal_id

    def mappings_for(self, internal_id: str) -> list[ProviderIdMapping]:
        return list(self._by_internal.get(internal_id, []))

    def external_id(self, internal_id: str, provider: str, entity_type: EntityType) -> str:
        for m in self._by_internal.get(internal_id, []):
            if m.ref.provider == provider and m.ref.entity_type is entity_type:
                return m.ref.external_id
        raise LookupError(f"{internal_id} has no {provider} {entity_type} id")
