"""Identity registry: append-only, correctable, auditable provider-ID mappings (ADR 0014).

Invariants (enforced here and, for the SQL registry, under a database lock):
* no silent reassignment -- changing what a ref maps to requires an event that supersedes the
  ref's current event and states a reason;
* at most one *VERIFIED* ref per (internal id, provider, entity type) at any time;
* ingestion accepts only VERIFIED mappings (``require``); UNVERIFIED/CONFLICTED/RETRACTED or
  missing mappings raise ``UnresolvedIdentityError`` -- no fuzzy matching during ingestion;
* history is never rewritten, so ``resolve(ref, as_of=T)`` returns the mapping believed at T.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Protocol

from fantasy_gm.domain.clock import Clock
from fantasy_gm.domain.identity import (
    EntityType,
    IdentityMappingEvent,
    MappingResolution,
    MappingStatus,
    ProviderRef,
    resolve_mapping,
)
from fantasy_gm.records.codec import EncodedRecord
from fantasy_gm.records.registry import DEFAULT_CODECS, CodecSet


class IdentityConflictError(ValueError):
    pass


class UnresolvedIdentityError(LookupError):
    def __init__(self, ref: ProviderRef, reason: str = "no mapping") -> None:
        super().__init__(
            f"{ref.provider}:{ref.entity_type}:{ref.external_id} unresolved ({reason})"
        )
        self.ref = ref
        self.reason = reason


class IdentityRegistry(Protocol):
    def assert_mapping(self, event: IdentityMappingEvent) -> datetime:
        """Append an assertion; returns the registry-stamped recorded_at."""
        ...

    def resolve(
        self, ref: ProviderRef, as_of: datetime | None = None
    ) -> MappingResolution | None: ...

    def history(self, ref: ProviderRef) -> list[tuple[IdentityMappingEvent, datetime]]: ...

    def refs_for(
        self, internal_id: str, as_of: datetime | None = None
    ) -> list[MappingResolution]: ...

    def external_id(self, internal_id: str, provider: str, entity_type: EntityType) -> str: ...


def validate_assertion(
    event: IdentityMappingEvent,
    current: MappingResolution | None,
    slot_holders: Iterable[MappingResolution],
) -> bool:
    """Returns False for an idempotent repeat (nothing to append); raises on violations."""
    if current is None:
        if event.supersedes_event_id is not None:
            raise IdentityConflictError("supersedes an event, but the ref has no mapping")
    else:
        same = current.internal_id == event.internal_id and current.status is event.status
        if same and event.supersedes_event_id is None:
            return False
        if event.supersedes_event_id != current.event_id:
            raise IdentityConflictError(
                f"{event.ref.key()} currently maps to {current.internal_id} "
                f"({current.status}); a change must supersede event {current.event_id} "
                "with a reason (no silent reassignment)"
            )
    if event.status is MappingStatus.VERIFIED:
        for other in slot_holders:
            if other.ref.key() != event.ref.key() and other.status is MappingStatus.VERIFIED:
                raise IdentityConflictError(
                    f"{event.internal_id} already has a VERIFIED {event.ref.provider} "
                    f"{event.ref.entity_type} id ({other.ref.external_id}); retract it first"
                )
    return True


def require(
    registry: IdentityRegistry, ref: ProviderRef, as_of: datetime | None = None
) -> tuple[str, str]:
    """(internal_id, mapping_event_id) for a VERIFIED mapping, else UnresolvedIdentityError."""
    res = registry.resolve(ref, as_of)
    if res is None:
        raise UnresolvedIdentityError(ref)
    if res.status is not MappingStatus.VERIFIED or res.internal_id is None:
        raise UnresolvedIdentityError(ref, f"mapping is {res.status}")
    return res.internal_id, res.event_id


class InMemoryIdentityRegistry:
    def __init__(self, clock: Clock, codecs: CodecSet = DEFAULT_CODECS) -> None:
        self._clock = clock
        self._codec = codecs.identity_event
        self._by_ref: dict[tuple[str, str, str], list[tuple[EncodedRecord, datetime]]] = {}
        self._lock = threading.Lock()

    def assert_mapping(self, event: IdentityMappingEvent) -> datetime:
        with self._lock:
            current = self._resolve_locked(event.ref, None)
            holders = self._slot_holders_locked(event)
            if not validate_assertion(event, current, holders):
                assert current is not None
                return current.recorded_at
            recorded_at = self._clock.now()
            self._by_ref.setdefault(event.ref.key(), []).append(
                (self._codec.encode(event), recorded_at)
            )
            return recorded_at

    def resolve(self, ref: ProviderRef, as_of: datetime | None = None) -> MappingResolution | None:
        with self._lock:
            return self._resolve_locked(ref, as_of)

    def history(self, ref: ProviderRef) -> list[tuple[IdentityMappingEvent, datetime]]:
        with self._lock:
            return self._history_locked(ref)

    def refs_for(self, internal_id: str, as_of: datetime | None = None) -> list[MappingResolution]:
        with self._lock:
            return self._refs_for_locked(internal_id, as_of, lambda _: True)

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

    def _history_locked(self, ref: ProviderRef) -> list[tuple[IdentityMappingEvent, datetime]]:
        return [(self._codec.decode_record(rec), at) for rec, at in self._by_ref.get(ref.key(), [])]

    def _resolve_locked(self, ref: ProviderRef, as_of: datetime | None) -> MappingResolution | None:
        return resolve_mapping(self._history_locked(ref), as_of)

    def _refs_for_locked(
        self,
        internal_id: str,
        as_of: datetime | None,
        keep: Callable[[ProviderRef], bool],
    ) -> list[MappingResolution]:
        out: list[MappingResolution] = []
        for key in self._by_ref:
            ref = ProviderRef(provider=key[0], entity_type=EntityType(key[1]), external_id=key[2])
            if not keep(ref):
                continue
            res = self._resolve_locked(ref, as_of)
            if res is not None and res.internal_id == internal_id:
                out.append(res)
        return out

    def _slot_holders_locked(self, event: IdentityMappingEvent) -> list[MappingResolution]:
        if event.internal_id is None:
            return []
        return self._refs_for_locked(
            event.internal_id,
            None,
            lambda r: r.provider == event.ref.provider and r.entity_type is event.ref.entity_type,
        )
