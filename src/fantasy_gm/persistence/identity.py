"""SQL identity registry (same invariants as ``InMemoryIdentityRegistry``, DB-enforced too)."""

from __future__ import annotations

from sqlalchemy import Engine, and_, select

from fantasy_gm.domain.identity import (
    EntityType,
    MappingMethod,
    ProviderIdMapping,
    ProviderRef,
)
from fantasy_gm.persistence.tables import provider_id_mappings as t
from fantasy_gm.providers.identity import IdentityConflictError


class SqlIdentityRegistry:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def link(self, mapping: ProviderIdMapping) -> None:
        ref = mapping.ref
        with self._engine.begin() as conn:
            existing = conn.execute(
                select(t.c.internal_id).where(
                    and_(
                        t.c.provider == ref.provider,
                        t.c.entity_type == ref.entity_type.value,
                        t.c.external_id == ref.external_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing != mapping.internal_id:
                    raise IdentityConflictError(f"{ref.key()} already mapped to {existing}")
                return
            slot = conn.execute(
                select(t.c.external_id).where(
                    and_(
                        t.c.internal_id == mapping.internal_id,
                        t.c.provider == ref.provider,
                        t.c.entity_type == ref.entity_type.value,
                    )
                )
            ).scalar_one_or_none()
            if slot is not None:
                raise IdentityConflictError(
                    f"{mapping.internal_id} already has a {ref.provider} {ref.entity_type} id"
                )
            conn.execute(
                t.insert().values(
                    internal_id=mapping.internal_id,
                    provider=ref.provider,
                    entity_type=ref.entity_type.value,
                    external_id=ref.external_id,
                    method=mapping.method.value,
                    confidence=mapping.confidence,
                    observed_at=mapping.observed_at,
                )
            )

    def resolve(self, ref: ProviderRef) -> str | None:
        with self._engine.connect() as conn:
            found: str | None = conn.execute(
                select(t.c.internal_id).where(
                    and_(
                        t.c.provider == ref.provider,
                        t.c.entity_type == ref.entity_type.value,
                        t.c.external_id == ref.external_id,
                    )
                )
            ).scalar_one_or_none()
            return found

    def mappings_for(self, internal_id: str) -> list[ProviderIdMapping]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(t).where(t.c.internal_id == internal_id).order_by(t.c.id)
            ).all()
        return [
            ProviderIdMapping(
                internal_id=r.internal_id,
                ref=ProviderRef(
                    provider=r.provider,
                    entity_type=EntityType(r.entity_type),
                    external_id=r.external_id,
                ),
                method=MappingMethod(r.method),
                confidence=r.confidence,
                observed_at=r.observed_at,
            )
            for r in rows
        ]
