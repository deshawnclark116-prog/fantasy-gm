"""Observation storage and cutoff-bound reading.

* ``ObservationStore`` is infrastructure: append-only, and even its query method requires a
  ``KnowledgeCutoff``. Only services that open knowledge sessions hold a store.
* ``ObservationReader`` is what engines receive: a reader already bound to a cutoff, knowledge
  mode and run (``fantasy_gm.knowledge.session.KnowledgeSession``). There is no unfiltered read.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from datetime import datetime
from typing import Protocol

from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.clock import Clock, SystemClock
from fantasy_gm.domain.ids import LeagueId, NFLTeamId, ObservationId, PlayerId
from fantasy_gm.domain.knowledge import resolve_known
from fantasy_gm.domain.observation import Observation
from fantasy_gm.domain.time import KnowledgeCutoff
from fantasy_gm.records.codec import EncodedRecord
from fantasy_gm.records.registry import DEFAULT_CODECS, CodecSet


class ObservationQuery(DomainModel):
    kinds: frozenset[str] | None = None
    player_id: PlayerId | None = None
    team_id: NFLTeamId | None = None
    league_id: LeagueId | None = None

    def matches(self, obs: Observation) -> bool:
        if self.kinds is not None and obs.kind not in self.kinds:
            return False
        if self.player_id is not None and obs.subject_player_id() != self.player_id:
            return False
        if self.league_id is not None and obs.subject_league_id() != self.league_id:
            return False
        return self.team_id is None or obs.subject_team_id() == self.team_id


class DuplicateObservationError(ValueError):
    pass


class ObservationStore(Protocol):
    def add(self, observation: Observation) -> None: ...

    def add_many(self, observations: Iterable[Observation]) -> None: ...

    def known_as_of(
        self, cutoff: KnowledgeCutoff, query: ObservationQuery
    ) -> list[Observation]: ...


class ObservationReader(Protocol):
    @property
    def cutoff(self) -> KnowledgeCutoff: ...

    def read(
        self, query: ObservationQuery, *, include_future_effective: bool = False
    ) -> list[Observation]: ...


class InMemoryObservationStore:
    """Reference store. Keeps only encoded records and decodes (hash-verified) on every read.

    Each row is stamped with the store clock's ``recorded_at``. Under SYSTEM_KNOWLEDGE a record
    is knowable only if it was physically stored by the cutoff, so a caller-supplied (possibly
    backdated) ``ingested_at`` can never make backfilled data visible to an earlier replay.
    """

    def __init__(self, clock: Clock | None = None, codecs: CodecSet = DEFAULT_CODECS) -> None:
        self._clock: Clock = clock or SystemClock()
        self._codecs = codecs
        self._rows: dict[ObservationId, tuple[str, EncodedRecord, datetime]] = {}
        self._lock = threading.Lock()

    def add(self, observation: Observation) -> None:
        encoded = self._codecs.observation(observation.kind).encode(observation)
        with self._lock:
            existing = self._rows.get(observation.observation_id)
            if existing is not None:
                if existing[1].payload_hash != encoded.payload_hash:
                    raise DuplicateObservationError(
                        f"observation {observation.observation_id} already stored with other "
                        "content"
                    )
                return
            self._rows[observation.observation_id] = (
                observation.kind,
                encoded,
                self._clock.now(),
            )

    def add_many(self, observations: Iterable[Observation]) -> None:
        for obs in observations:
            self.add(obs)

    def known_as_of(self, cutoff: KnowledgeCutoff, query: ObservationQuery) -> list[Observation]:
        bound = cutoff.storage_bound()
        with self._lock:
            rows = [
                (kind, rec)
                for kind, rec, recorded_at in self._rows.values()
                if bound is None or recorded_at <= bound
            ]
        decoded = (self._codecs.observation(kind).decode_record(rec) for kind, rec in rows)
        return resolve_known((o for o in decoded if query.matches(o)), cutoff)
