"""Observation store protocol + in-memory implementation.

Stores are append-only. Queries always take a ``KnowledgeCutoff``; there is intentionally no
"give me everything" method on the protocol, so historical code cannot accidentally read the
future.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.ids import NFLTeamId, ObservationId, PlayerId
from fantasy_gm.domain.knowledge import resolve_known
from fantasy_gm.domain.observation import Observation
from fantasy_gm.domain.time import KnowledgeCutoff


class ObservationQuery(DomainModel):
    kinds: frozenset[str] | None = None
    player_id: PlayerId | None = None
    team_id: NFLTeamId | None = None

    def matches(self, obs: Observation) -> bool:
        if self.kinds is not None and obs.kind not in self.kinds:
            return False
        if self.player_id is not None and obs.subject_player_id() != self.player_id:
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


class InMemoryObservationStore:
    def __init__(self) -> None:
        self._rows: dict[ObservationId, str] = {}
        self._objects: dict[ObservationId, Observation] = {}

    def add(self, observation: Observation) -> None:
        payload = observation.canonical_json()
        existing = self._rows.get(observation.observation_id)
        if existing is not None:
            if existing != payload:
                raise DuplicateObservationError(
                    f"observation {observation.observation_id} already stored with other content"
                )
            return
        self._rows[observation.observation_id] = payload
        # Store a private copy so callers mutating nested containers cannot rewrite history.
        self._objects[observation.observation_id] = observation.model_copy(deep=True)

    def add_many(self, observations: Iterable[Observation]) -> None:
        for obs in observations:
            self.add(obs)

    def known_as_of(self, cutoff: KnowledgeCutoff, query: ObservationQuery) -> list[Observation]:
        candidates = (o for o in self._objects.values() if query.matches(o))
        return [o.model_copy(deep=True) for o in resolve_known(candidates, cutoff)]
