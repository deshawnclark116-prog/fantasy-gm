"""SQL model-artifact and validation registry (append-only)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Engine, select

from fantasy_gm.domain.artifacts import ModelArtifactManifest
from fantasy_gm.domain.autonomy import DecisionType
from fantasy_gm.domain.clock import Clock
from fantasy_gm.domain.validation import (
    ModelValidationRecord,
    ValidationState,
    current_validation_state,
)
from fantasy_gm.models.registry import UnknownArtifactError
from fantasy_gm.persistence.ledger import encoded_from_row, payload_values
from fantasy_gm.persistence.tables import model_artifacts, model_validation_records
from fantasy_gm.records.registry import DEFAULT_CODECS, CodecSet

_COLS = ("record_type", "schema_version", "payload", "payload_hash", "recorded_at")


class SqlModelRegistry:
    def __init__(self, engine: Engine, clock: Clock, codecs: CodecSet = DEFAULT_CODECS) -> None:
        self._engine = engine
        self._clock = clock
        self._codecs = codecs

    def register_artifact(self, artifact: ModelArtifactManifest) -> str:
        h = artifact.artifact_hash
        with self._engine.begin() as conn:
            exists = conn.execute(
                select(model_artifacts.c.artifact_hash).where(model_artifacts.c.artifact_hash == h)
            ).first()
            if exists is None:
                enc = self._codecs.model_artifact.encode(artifact)
                conn.execute(
                    model_artifacts.insert().values(
                        artifact_hash=h,
                        name=artifact.name,
                        version=artifact.version,
                        **payload_values(enc, self._clock.now()),
                    )
                )
        return h

    def get_artifact(self, artifact_hash: str) -> ModelArtifactManifest:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(*[model_artifacts.c[c] for c in _COLS]).where(
                    model_artifacts.c.artifact_hash == artifact_hash
                )
            ).first()
        if row is None:
            raise UnknownArtifactError(artifact_hash)
        artifact = self._codecs.model_artifact.decode_record(encoded_from_row(row))
        if artifact.artifact_hash != artifact_hash:
            raise UnknownArtifactError(f"{artifact_hash}: stored artifact hashes differently")
        return artifact

    def record_validation(self, record: ModelValidationRecord) -> datetime:
        self.get_artifact(record.artifact_hash)
        now = self._clock.now()
        enc = self._codecs.validation_record.encode(record)
        with self._engine.begin() as conn:
            conn.execute(
                model_validation_records.insert().values(
                    record_id=record.record_id,
                    artifact_hash=record.artifact_hash,
                    decision_type=record.decision_type.value,
                    regime=record.regime,
                    state=record.state.value,
                    effective_at=record.effective_at,
                    **payload_values(enc, now),
                )
            )
        return now

    def validation_state(
        self, artifact_hash: str, decision_type: DecisionType, regime: str, as_of: datetime
    ) -> ValidationState:
        t = model_validation_records
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(*[t.c[c] for c in _COLS]).where(
                    t.c.artifact_hash == artifact_hash,
                    t.c.decision_type == decision_type.value,
                    t.c.regime == regime,
                )
            ).all()
        decoded = [
            (self._codecs.validation_record.decode_record(encoded_from_row(r)), r.recorded_at)
            for r in rows
        ]
        return current_validation_state(decoded, artifact_hash, decision_type, regime, as_of)
