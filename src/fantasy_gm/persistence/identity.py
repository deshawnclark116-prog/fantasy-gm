"""SQL identity registry: append-only mapping assertions, serialised per (provider, entity)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, Row, and_, func, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from fantasy_gm.domain.clock import Clock
from fantasy_gm.domain.identity import (
    EntityType,
    IdentityMappingEvent,
    MappingResolution,
    MappingStatus,
    ProviderRef,
    resolve_mapping,
)
from fantasy_gm.persistence.ledger import encoded_from_row, payload_values
from fantasy_gm.persistence.tables import identity_mapping_events as t
from fantasy_gm.providers.identity import IdentityConflictError, validate_assertion
from fantasy_gm.records.registry import DEFAULT_CODECS, CodecSet


class SqlIdentityRegistry:
    def __init__(self, engine: Engine, clock: Clock, codecs: CodecSet = DEFAULT_CODECS) -> None:
        self._engine = engine
        self._clock = clock
        self._codec = codecs.identity_event

    def assert_mapping(self, event: IdentityMappingEvent) -> datetime:
        ref = event.ref
        with self._engine.begin() as conn:
            if conn.dialect.name == "postgresql":
                # Identity writes are rare: one transaction-scoped advisory lock per
                # (provider, entity_type) serialises the cross-ref VERIFIED-slot invariant.
                conn.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                    {"k": f"identity:{ref.provider}:{ref.entity_type.value}"},
                )
            current = resolve_mapping(self._history(conn, ref), None)
            holders = (
                []
                if event.internal_id is None
                else self._refs_for(conn, event.internal_id, None, ref.provider, ref.entity_type)
            )
            if not validate_assertion(event, current, holders):
                assert current is not None
                return current.recorded_at
            seq = conn.execute(select(func.count()).where(self._ref_filter(ref))).scalar_one()
            now = self._clock.now()
            enc = self._codec.encode(event)
            try:
                conn.execute(
                    t.insert().values(
                        event_id=event.event_id,
                        seq=seq,
                        provider=ref.provider,
                        entity_type=ref.entity_type.value,
                        external_id=ref.external_id,
                        internal_id=event.internal_id,
                        status=event.status.value,
                        **payload_values(enc, now),
                    )
                )
            except IntegrityError as exc:
                raise IdentityConflictError(f"concurrent identity assertion: {exc.orig}") from exc
            return now

    def resolve(self, ref: ProviderRef, as_of: datetime | None = None) -> MappingResolution | None:
        with self._engine.connect() as conn:
            return resolve_mapping(self._history(conn, ref), as_of)

    def history(self, ref: ProviderRef) -> list[tuple[IdentityMappingEvent, datetime]]:
        with self._engine.connect() as conn:
            return self._history(conn, ref)

    def refs_for(self, internal_id: str, as_of: datetime | None = None) -> list[MappingResolution]:
        with self._engine.connect() as conn:
            return self._refs_for(conn, internal_id, as_of, None, None)

    def external_id(self, internal_id: str, provider: str, entity_type: EntityType) -> str:
        for res in self.refs_for(internal_id):
            if (
                res.ref.provider == provider
                and res.ref.entity_type is entity_type
                and res.status is MappingStatus.VERIFIED
            ):
                return res.ref.external_id
        raise LookupError(f"{internal_id} has no VERIFIED {provider} {entity_type} id")

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _ref_filter(ref: ProviderRef) -> Any:
        return and_(
            t.c.provider == ref.provider,
            t.c.entity_type == ref.entity_type.value,
            t.c.external_id == ref.external_id,
        )

    def _decode(self, rows: Sequence[Row[Any]]) -> list[tuple[IdentityMappingEvent, datetime]]:
        return [(self._codec.decode_record(encoded_from_row(r)), r.recorded_at) for r in rows]

    def _history(
        self, conn: Connection, ref: ProviderRef
    ) -> list[tuple[IdentityMappingEvent, datetime]]:
        rows = conn.execute(
            select(
                t.c.record_type, t.c.schema_version, t.c.payload, t.c.payload_hash, t.c.recorded_at
            )
            .where(self._ref_filter(ref))
            .order_by(t.c.seq)
        ).all()
        return self._decode(rows)

    def _refs_for(
        self,
        conn: Connection,
        internal_id: str,
        as_of: datetime | None,
        provider: str | None,
        entity_type: EntityType | None,
    ) -> list[MappingResolution]:
        # Any ref that has *ever* pointed at internal_id; resolve each to its belief at as_of.
        stmt = select(t.c.provider, t.c.entity_type, t.c.external_id).where(
            t.c.internal_id == internal_id
        )
        if provider is not None and entity_type is not None:
            stmt = stmt.where(t.c.provider == provider, t.c.entity_type == entity_type.value)
        out: list[MappingResolution] = []
        for p, e, x in conn.execute(stmt.distinct()).all():
            ref = ProviderRef(provider=p, entity_type=EntityType(e), external_id=x)
            res = resolve_mapping(self._history(conn, ref), as_of)
            if res is not None and res.internal_id == internal_id:
                out.append(res)
        return out
