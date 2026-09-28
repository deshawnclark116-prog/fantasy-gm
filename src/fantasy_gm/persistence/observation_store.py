"""SQL observation store. Delegates as-of semantics to ``domain.knowledge.resolve_known``."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError

from fantasy_gm.domain.knowledge import resolve_known
from fantasy_gm.domain.observation import Observation
from fantasy_gm.domain.observation_registry import OBSERVATION_TYPES, deserialize_observation
from fantasy_gm.domain.time import KnowledgeCutoff, KnowledgeMode
from fantasy_gm.persistence.tables import observations
from fantasy_gm.player_state.store import DuplicateObservationError, ObservationQuery


class SqlObservationStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def add(self, observation: Observation) -> None:
        self.add_many([observation])

    def add_many(self, items: Iterable[Observation]) -> None:
        with self._engine.begin() as conn:
            for obs in items:
                if obs.kind not in OBSERVATION_TYPES:
                    raise ValueError(f"unregistered observation kind {obs.kind!r}")
                digest = obs.content_hash()
                existing = conn.execute(
                    select(observations.c.payload_hash).where(
                        observations.c.observation_id == obs.observation_id
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if existing != digest:
                        raise DuplicateObservationError(
                            f"observation {obs.observation_id} already stored with other content"
                        )
                    continue
                try:
                    conn.execute(
                        observations.insert().values(
                            observation_id=obs.observation_id,
                            kind=obs.kind,
                            fact_key=obs.fact_key(),
                            subject_player_id=obs.subject_player_id(),
                            subject_team_id=obs.subject_team_id(),
                            observed_at=obs.observed_at,
                            effective_at=obs.effective_at,
                            ingested_at=obs.source.ingested_at,
                            provider=obs.source.provider,
                            payload=obs.model_dump(mode="json"),
                            payload_hash=digest,
                        )
                    )
                except IntegrityError as exc:  # pragma: no cover - race
                    raise DuplicateObservationError(str(exc)) from exc

    def known_as_of(self, cutoff: KnowledgeCutoff, query: ObservationQuery) -> list[Observation]:
        stmt = select(observations.c.kind, observations.c.payload).where(
            observations.c.observed_at <= cutoff.as_of
        )
        if cutoff.mode is KnowledgeMode.SYSTEM_KNOWLEDGE:
            stmt = stmt.where(observations.c.ingested_at <= cutoff.as_of)
        if query.kinds is not None:
            stmt = stmt.where(observations.c.kind.in_(sorted(query.kinds)))
        if query.player_id is not None:
            stmt = stmt.where(observations.c.subject_player_id == query.player_id)
        if query.team_id is not None:
            stmt = stmt.where(observations.c.subject_team_id == query.team_id)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return resolve_known((deserialize_observation(r.kind, r.payload) for r in rows), cutoff)
