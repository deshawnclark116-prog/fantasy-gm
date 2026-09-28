"""SQL observation store. Verifies stored bytes on every read; delegates as-of semantics to
``domain.knowledge.resolve_known``."""

from __future__ import annotations

import json
from collections.abc import Iterable

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError

from fantasy_gm.domain.clock import Clock
from fantasy_gm.domain.knowledge import resolve_known
from fantasy_gm.domain.observation import Observation
from fantasy_gm.domain.time import KnowledgeCutoff, KnowledgeMode
from fantasy_gm.persistence.tables import observations as t
from fantasy_gm.player_state.store import DuplicateObservationError, ObservationQuery
from fantasy_gm.records.codec import EncodedRecord
from fantasy_gm.records.registry import DEFAULT_CODECS, CodecSet


class SqlObservationStore:
    def __init__(self, engine: Engine, clock: Clock, codecs: CodecSet = DEFAULT_CODECS) -> None:
        self._engine = engine
        self._clock = clock
        self._codecs = codecs

    def add(self, observation: Observation) -> None:
        self.add_many([observation])

    def add_many(self, items: Iterable[Observation]) -> None:
        with self._engine.begin() as conn:
            for obs in items:
                enc = self._codecs.observation(obs.kind).encode(obs)
                existing = conn.execute(
                    select(t.c.payload_hash).where(t.c.observation_id == obs.observation_id)
                ).scalar_one_or_none()
                if existing is not None:
                    if existing != enc.payload_hash:
                        raise DuplicateObservationError(
                            f"observation {obs.observation_id} already stored with other content"
                        )
                    continue
                try:
                    conn.execute(
                        t.insert().values(
                            observation_id=obs.observation_id,
                            kind=obs.kind,
                            fact_key=obs.fact_key(),
                            subject_player_id=obs.subject_player_id(),
                            subject_team_id=obs.subject_team_id(),
                            subject_league_id=obs.subject_league_id(),
                            observed_at=obs.observed_at,
                            effective_at=obs.effective_at,
                            ingested_at=obs.source.ingested_at,
                            provider=obs.source.provider,
                            timestamp_quality=obs.source.timestamp_quality.value,
                            record_type=enc.record_type,
                            schema_version=enc.schema_version,
                            payload=enc.payload,
                            payload_hash=enc.payload_hash,
                            recorded_at=self._clock.now(),
                            payload_json=json.loads(enc.payload),
                        )
                    )
                except IntegrityError as exc:  # pragma: no cover - race on same id
                    raise DuplicateObservationError(str(exc)) from exc

    def known_as_of(self, cutoff: KnowledgeCutoff, query: ObservationQuery) -> list[Observation]:
        stmt = select(
            t.c.kind, t.c.record_type, t.c.schema_version, t.c.payload, t.c.payload_hash
        ).where(t.c.observed_at <= cutoff.as_of)
        if cutoff.mode is KnowledgeMode.SYSTEM_KNOWLEDGE:
            stmt = stmt.where(t.c.ingested_at <= cutoff.as_of)
        bound = cutoff.storage_bound()
        if bound is not None:  # physically stored by the cutoff / reproducibility pin
            stmt = stmt.where(t.c.recorded_at <= bound)
        if query.kinds is not None:
            stmt = stmt.where(t.c.kind.in_(sorted(query.kinds)))
        if query.player_id is not None:
            stmt = stmt.where(t.c.subject_player_id == query.player_id)
        if query.team_id is not None:
            stmt = stmt.where(t.c.subject_team_id == query.team_id)
        if query.league_id is not None:
            stmt = stmt.where(t.c.subject_league_id == query.league_id)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        decoded = (
            self._codecs.observation(r.kind).decode_record(
                EncodedRecord(
                    record_type=r.record_type,
                    schema_version=r.schema_version,
                    payload=r.payload,
                    payload_hash=r.payload_hash,
                )
            )
            for r in rows
        )
        return resolve_known((o for o in decoded if query.matches(o)), cutoff)
