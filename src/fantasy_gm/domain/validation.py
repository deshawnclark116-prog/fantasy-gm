"""Model validation state (append-only records; fail closed)."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from pydantic import Field

from fantasy_gm.domain.autonomy import DecisionType
from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.ids import ValidationRecordId, new_validation_record_id
from fantasy_gm.domain.time import UtcDatetime


class ValidationState(StrEnum):
    UNVALIDATED = "unvalidated"
    VALIDATED = "validated"
    DEGRADED = "degraded"  # monitoring detected drift / calibration loss
    REVOKED = "revoked"


class ModelValidationRecord(DomainModel):
    """Approval (or withdrawal) of an artifact for one decision type in one regime.

    ``regime`` is an explicit key (e.g. "regular_season", "live_draft"); there is no wildcard,
    so approval never silently generalises to a regime it was not validated for.
    """

    record_id: ValidationRecordId = Field(default_factory=new_validation_record_id)
    artifact_hash: str
    decision_type: DecisionType
    regime: str = Field(min_length=1)
    state: ValidationState
    effective_at: UtcDatetime
    approved_by: str = Field(min_length=1)
    evidence_ref: str | None = None  # e.g. hash of a calibration / forward-validation report
    notes: str = ""


def current_validation_state(
    records: Iterable[tuple[ModelValidationRecord, UtcDatetime]],
    artifact_hash: str,
    decision_type: DecisionType,
    regime: str,
    as_of: UtcDatetime,
) -> ValidationState:
    """Latest record (by effective_at, then recorded_at) that was both effective and recorded
    by ``as_of``. No record -> UNVALIDATED."""
    best: tuple[UtcDatetime, UtcDatetime, ModelValidationRecord] | None = None
    for record, recorded_at in records:
        if (
            record.artifact_hash != artifact_hash
            or record.decision_type is not decision_type
            or record.regime != regime
            or record.effective_at > as_of
            or recorded_at > as_of
        ):
            continue
        key = (record.effective_at, recorded_at, record)
        if best is None or key[:2] > best[:2]:
            best = key
    return ValidationState.UNVALIDATED if best is None else best[2].state
