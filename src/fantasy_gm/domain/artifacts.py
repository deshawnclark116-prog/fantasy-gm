"""Model-artifact provenance.

Every model, calibrator or hand-specified heuristic that influences a decision is described by
a ``ModelArtifactManifest``. Its content hash is its identity. A FITTED artifact trained through
a time later than a decision's information cutoff can never be used by that decision.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.time import UtcDatetime


class FitKind(StrEnum):
    FITTED = "fitted"  # parameters estimated from data up to trained_through
    # Parameters chosen by a person, not fitted to data. There is no trained_through; the
    # residual risk that the author's knowledge leaks into replays is flagged, not hidden.
    HAND_SPECIFIED = "hand_specified"


class RuntimeFingerprint(DomainModel):
    python_version: str
    numpy_version: str
    platform: str
    package_version: str
    code_commit: str | None = None
    lockfile_hash: str | None = None


class ModelArtifactManifest(DomainModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    fit_kind: FitKind
    code_version: str | None = None
    trained_through: UtcDatetime | None = None
    training_data_manifest_hash: str | None = None
    feature_schema_version: str | None = None
    feature_schema_hash: str | None = None
    calibration_artifact_hash: str | None = None
    calibration_version: str | None = None
    config_hash: str = Field(min_length=1)
    runtime: RuntimeFingerprint | None = None
    created_at: UtcDatetime
    notes: str = ""

    @model_validator(mode="after")
    def _fit_consistency(self) -> Self:
        if self.fit_kind is FitKind.FITTED:
            if self.trained_through is None or self.training_data_manifest_hash is None:
                raise ValueError("FITTED artifacts require trained_through and a training manifest")
            if self.trained_through > self.created_at:
                raise ValueError("an artifact cannot be trained through a time after its creation")
        elif self.trained_through is not None or self.training_data_manifest_hash is not None:
            raise ValueError("HAND_SPECIFIED artifacts have no training window")
        if (self.calibration_artifact_hash is None) != (self.calibration_version is None):
            raise ValueError("calibration hash and version must be given together")
        return self

    @property
    def artifact_hash(self) -> str:
        return self.content_hash()
