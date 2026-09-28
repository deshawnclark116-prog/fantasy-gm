"""Production codec set. Bump a codec's version and register a legacy reader whenever a
durable model changes incompatibly; golden fixtures in tests/fixtures/records guard this."""

from __future__ import annotations

from dataclasses import dataclass, field

from fantasy_gm.domain.artifacts import ModelArtifactManifest
from fantasy_gm.domain.decision import (
    CounterfactualEstimate,
    CounterfactualEvaluation,
    Decision,
    DecisionStatusEvent,
    ObservedGrade,
    ObservedOutcome,
)
from fantasy_gm.domain.execution import ExecutionEvent
from fantasy_gm.domain.identity import IdentityMappingEvent
from fantasy_gm.domain.observation import Observation
from fantasy_gm.domain.observation_registry import OBSERVATION_TYPES
from fantasy_gm.domain.validation import ModelValidationRecord
from fantasy_gm.records.codec import VersionedCodec


def _observation_codecs() -> dict[str, VersionedCodec[Observation]]:
    return {
        kind: VersionedCodec(f"observation:{kind}", cls, cls.schema_version)
        for kind, cls in OBSERVATION_TYPES.items()
    }


@dataclass(frozen=True)
class CodecSet:
    decision: VersionedCodec[Decision] = field(
        default_factory=lambda: VersionedCodec("decision", Decision)
    )
    status_event: VersionedCodec[DecisionStatusEvent] = field(
        default_factory=lambda: VersionedCodec("decision_status_event", DecisionStatusEvent)
    )
    execution_event: VersionedCodec[ExecutionEvent] = field(
        default_factory=lambda: VersionedCodec("execution_event", ExecutionEvent)
    )
    observed_outcome: VersionedCodec[ObservedOutcome] = field(
        default_factory=lambda: VersionedCodec("observed_outcome", ObservedOutcome)
    )
    observed_grade: VersionedCodec[ObservedGrade] = field(
        default_factory=lambda: VersionedCodec("observed_grade", ObservedGrade)
    )
    counterfactual_estimate: VersionedCodec[CounterfactualEstimate] = field(
        default_factory=lambda: VersionedCodec("counterfactual_estimate", CounterfactualEstimate)
    )
    counterfactual_evaluation: VersionedCodec[CounterfactualEvaluation] = field(
        default_factory=lambda: VersionedCodec(
            "counterfactual_evaluation", CounterfactualEvaluation
        )
    )
    identity_event: VersionedCodec[IdentityMappingEvent] = field(
        default_factory=lambda: VersionedCodec("identity_mapping_event", IdentityMappingEvent)
    )
    model_artifact: VersionedCodec[ModelArtifactManifest] = field(
        default_factory=lambda: VersionedCodec("model_artifact", ModelArtifactManifest)
    )
    validation_record: VersionedCodec[ModelValidationRecord] = field(
        default_factory=lambda: VersionedCodec("model_validation_record", ModelValidationRecord)
    )
    observations: dict[str, VersionedCodec[Observation]] = field(
        default_factory=_observation_codecs
    )

    def observation(self, kind: str) -> VersionedCodec[Observation]:
        try:
            return self.observations[kind]
        except KeyError as exc:
            raise ValueError(f"unknown observation kind {kind!r}") from exc


DEFAULT_CODECS = CodecSet()
