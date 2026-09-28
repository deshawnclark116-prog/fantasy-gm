"""Evidence manifests: everything a decision read, sealed by a knowledge session."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from fantasy_gm.domain.artifacts import FitKind
from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.ids import RunId
from fantasy_gm.domain.run_context import RunMode
from fantasy_gm.domain.time import KnowledgeCutoff, KnowledgeMode, TimestampQuality, UtcDatetime


class EvidenceKind(StrEnum):
    OBSERVATION = "observation"
    MODEL_ARTIFACT = "model_artifact"
    DERIVED = "derived"  # computed inside the session from other evidence (snapshots, states)
    SIMULATION_RUN = "simulation_run"


class EvidenceEntry(DomainModel):
    evidence_id: str = Field(min_length=1)  # stable: "<kind>:<reference_id>"
    kind: EvidenceKind
    reference_type: str
    reference_id: str
    reference_hash: str  # hash of the referenced record's stored content
    known_at: UtcDatetime | None = None
    ingested_at: UtcDatetime | None = None
    effective_at: UtcDatetime | None = None
    provider: str | None = None
    timestamp_quality: TimestampQuality | None = None
    timestamp_suspect: bool = False
    fit_kind: FitKind | None = None
    trained_through: UtcDatetime | None = None
    derived_from: tuple[str, ...] = ()
    summary: str = ""

    @model_validator(mode="after")
    def _kind_fields(self) -> Self:
        if self.kind is EvidenceKind.OBSERVATION and (
            self.known_at is None or self.ingested_at is None or self.timestamp_quality is None
        ):
            raise ValueError("observation evidence needs known_at, ingested_at, timestamp_quality")
        if self.kind is EvidenceKind.MODEL_ARTIFACT and self.fit_kind is None:
            raise ValueError("model-artifact evidence needs fit_kind")
        if self.kind in (EvidenceKind.DERIVED, EvidenceKind.SIMULATION_RUN) and (
            self.known_at is None
        ):
            raise ValueError("derived evidence needs known_at")
        return self


def evidence_id(kind: EvidenceKind, reference_id: str) -> str:
    return f"{kind.value}:{reference_id}"


class EvidenceManifest(DomainModel):
    run_id: RunId
    run_mode: RunMode
    cutoff: KnowledgeCutoff
    sealed_at: UtcDatetime
    entries: tuple[EvidenceEntry, ...] = ()

    @model_validator(mode="after")
    def _knowable(self) -> Self:
        as_of = self.cutoff.as_of
        ids = [e.evidence_id for e in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate evidence ids in manifest")
        known_ids = set(ids)
        for e in self.entries:
            if e.known_at is not None and e.known_at > as_of:
                raise ValueError(f"evidence {e.evidence_id} known_at is after the cutoff (leakage)")
            if (
                self.cutoff.mode is KnowledgeMode.SYSTEM_KNOWLEDGE
                and e.ingested_at is not None
                and e.ingested_at > as_of
            ):
                raise ValueError(f"evidence {e.evidence_id} ingested after the cutoff (leakage)")
            if e.fit_kind is FitKind.FITTED and (
                e.trained_through is None or e.trained_through > as_of
            ):
                raise ValueError(
                    f"model artifact {e.reference_id} trained through data after the cutoff"
                )
            missing = set(e.derived_from) - known_ids
            if missing:
                raise ValueError(f"evidence {e.evidence_id} derived from unknown {missing}")
        return self

    def ids(self) -> frozenset[str]:
        return frozenset(e.evidence_id for e in self.entries)

    def entry(self, evidence_id_: str) -> EvidenceEntry:
        return next(e for e in self.entries if e.evidence_id == evidence_id_)

    def artifact_hashes(self) -> frozenset[str]:
        return frozenset(
            e.reference_id for e in self.entries if e.kind is EvidenceKind.MODEL_ARTIFACT
        )

    @property
    def hand_specified_artifacts(self) -> tuple[str, ...]:
        return tuple(e.reference_id for e in self.entries if e.fit_kind is FitKind.HAND_SPECIFIED)

    @property
    def timestamp_suspect_ids(self) -> tuple[str, ...]:
        return tuple(e.evidence_id for e in self.entries if e.timestamp_suspect)
