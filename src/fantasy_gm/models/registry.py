"""Artifact + validation registry. Append-only; validation fails closed to UNVALIDATED."""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Protocol

from fantasy_gm.domain.artifacts import ModelArtifactManifest
from fantasy_gm.domain.autonomy import DecisionType
from fantasy_gm.domain.clock import Clock
from fantasy_gm.domain.validation import (
    ModelValidationRecord,
    ValidationState,
    current_validation_state,
)
from fantasy_gm.records.codec import EncodedRecord
from fantasy_gm.records.registry import DEFAULT_CODECS, CodecSet


class UnknownArtifactError(LookupError):
    pass


class ModelRegistry(Protocol):
    def register_artifact(self, artifact: ModelArtifactManifest) -> str: ...

    def get_artifact(self, artifact_hash: str) -> ModelArtifactManifest: ...

    def record_validation(self, record: ModelValidationRecord) -> datetime: ...

    def validation_state(
        self, artifact_hash: str, decision_type: DecisionType, regime: str, as_of: datetime
    ) -> ValidationState: ...


def effective_validation_state(
    registry: ModelRegistry,
    artifact_hash: str,
    decision_type: DecisionType,
    regime: str,
    as_of: datetime,
) -> tuple[ValidationState, tuple[str, ...]]:
    """State of the decision artifact AND its calibrator (if any); the worse one wins."""
    try:
        artifact = registry.get_artifact(artifact_hash)
    except UnknownArtifactError:
        return ValidationState.UNVALIDATED, (f"artifact {artifact_hash[:12]} is not registered",)
    reasons: list[str] = []
    state = registry.validation_state(artifact_hash, decision_type, regime, as_of)
    if state is not ValidationState.VALIDATED:
        reasons.append(
            f"{artifact.name}@{artifact.version} is {state} for {decision_type}/{regime}"
        )
    if artifact.calibration_artifact_hash is not None:
        cal = registry.validation_state(
            artifact.calibration_artifact_hash, decision_type, regime, as_of
        )
        if cal is not ValidationState.VALIDATED:
            reasons.append(f"calibrator is {cal} for {decision_type}/{regime}")
            state = cal if state is ValidationState.VALIDATED else state
    return state, tuple(reasons)


class InMemoryModelRegistry:
    def __init__(self, clock: Clock, codecs: CodecSet = DEFAULT_CODECS) -> None:
        self._clock = clock
        self._codecs = codecs
        self._artifacts: dict[str, EncodedRecord] = {}
        self._validations: list[tuple[EncodedRecord, datetime]] = []
        self._lock = threading.Lock()

    def register_artifact(self, artifact: ModelArtifactManifest) -> str:
        encoded = self._codecs.model_artifact.encode(artifact)
        with self._lock:
            self._artifacts.setdefault(artifact.artifact_hash, encoded)
        return artifact.artifact_hash

    def get_artifact(self, artifact_hash: str) -> ModelArtifactManifest:
        with self._lock:
            rec = self._artifacts.get(artifact_hash)
        if rec is None:
            raise UnknownArtifactError(artifact_hash)
        return self._codecs.model_artifact.decode_record(rec)

    def record_validation(self, record: ModelValidationRecord) -> datetime:
        self.get_artifact(record.artifact_hash)  # must be registered
        with self._lock:
            at = self._clock.now()
            self._validations.append((self._codecs.validation_record.encode(record), at))
            return at

    def validation_state(
        self, artifact_hash: str, decision_type: DecisionType, regime: str, as_of: datetime
    ) -> ValidationState:
        with self._lock:
            rows = list(self._validations)
        decoded = [(self._codecs.validation_record.decode_record(r), at) for r, at in rows]
        return current_validation_state(decoded, artifact_hash, decision_type, regime, as_of)
