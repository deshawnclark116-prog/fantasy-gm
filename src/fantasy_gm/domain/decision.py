"""Decision ledger records.

A ``Decision`` is written once and never mutated. Everything that happens afterwards --
approval, execution attempts, observed outcomes, grades, model-based counterfactual estimates --
is a *separate* append-only record keyed by ``decision_id`` (ADR 0004, ADR 0013).

Three times are kept deliberately distinct:
* ``decision_time``       -- the logical time of the decision (historical in a replay);
* ``information_cutoff``  -- the knowledge boundary of its evidence (<= decision_time);
* ledger ``recorded_at``  -- when the ledger physically wrote it; stamped by the ledger's own
  clock, never supplied by the caller and therefore not a field of this model.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from itertools import pairwise
from types import MappingProxyType
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from fantasy_gm.domain.actions import (
    Action,
    DraftSelection,
    FreeAgentAddDrop,
    NoAction,
    ProposeTrade,
    RespondToTrade,
    SetLineup,
    WaiverClaim,
)
from fantasy_gm.domain.autonomy import AutonomyMode, DecisionType
from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.evidence import EvidenceKind, EvidenceManifest
from fantasy_gm.domain.frozen import FrozenMap, FrozenMapping
from fantasy_gm.domain.identity import ProviderName
from fantasy_gm.domain.ids import (
    CandidateId,
    DecisionId,
    EstimateId,
    FantasyTeamId,
    GradeId,
    LeagueId,
    OutcomeId,
    StatusEventId,
    new_candidate_id,
    new_decision_id,
    new_estimate_id,
    new_grade_id,
    new_outcome_id,
    new_status_event_id,
)
from fantasy_gm.domain.run_context import RunContext
from fantasy_gm.domain.seeds import SeedSpec
from fantasy_gm.domain.time import UtcDatetime

# Module-level lookup table, not a pydantic field; wrapped so it cannot be mutated in place at
# process scope (ADR 0019 -- the audit covers shallowly frozen containers generally, not only
# pydantic model fields).
_ALLOWED_ACTIONS: Mapping[DecisionType, tuple[type[DomainModel], ...]] = MappingProxyType(
    {
        DecisionType.DRAFT_PICK: (DraftSelection,),
        DecisionType.LINEUP: (SetLineup, NoAction),
        DecisionType.WAIVER_CLAIM: (WaiverClaim, NoAction),
        DecisionType.FREE_AGENT_ADD: (FreeAgentAddDrop, NoAction),
        DecisionType.DROP: (FreeAgentAddDrop, NoAction),
        DecisionType.TRADE_PROPOSAL: (ProposeTrade, NoAction),
        DecisionType.TRADE_RESPONSE: (RespondToTrade,),
    }
)


class OutcomeDistribution(DomainModel):
    """A predictive distribution for one scalar metric. Quantile keys are probabilities."""

    metric: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    mean: float
    stdev: float | None = Field(default=None, ge=0.0)
    quantiles: FrozenMapping[float, float] = Field(default_factory=dict)
    n_samples: int | None = Field(default=None, ge=1)
    method: str = Field(min_length=1)
    higher_is_better: bool = True

    @field_validator("quantiles")
    @classmethod
    def _monotone(cls, value: Mapping[float, float]) -> Mapping[float, float]:
        ordered = sorted(value.items())
        for prob, _ in ordered:
            if not 0.0 < prob < 1.0:
                raise ValueError("quantile probabilities must be in (0, 1)")
        for (_, lo), (_, hi) in pairwise(ordered):
            if hi < lo:
                raise ValueError("quantile values must be non-decreasing")
        # Re-wrap in canonical (sorted) order: a plain `dict(ordered)` here would silently
        # replace the FrozenMapping that the Annotated schema already produced, undoing the
        # freeze (a field_validator's return value becomes the final field value, bypassing
        # no further freezing). FrozenMap keeps this value immutable end to end.
        return FrozenMap(dict(ordered))


class ConfidenceAssessment(DomainModel):
    """Descriptive only. It never authorises execution (see ValidationState / risk policy)."""

    score: float = Field(ge=0.0, le=1.0)
    insufficient_evidence: bool
    reasons: tuple[str, ...] = ()


class DecisionCandidate(DomainModel):
    candidate_id: CandidateId = Field(default_factory=new_candidate_id)
    action: Action
    estimated_outcome: OutcomeDistribution | None = None
    rationale: str = ""
    evidence_ids: tuple[str, ...] = ()


class Decision(DomainModel):
    decision_id: DecisionId = Field(default_factory=new_decision_id)
    league_id: LeagueId
    fantasy_team_id: FantasyTeamId
    decision_type: DecisionType
    run: RunContext
    decision_time: UtcDatetime
    information_cutoff: UtcDatetime
    candidates: tuple[DecisionCandidate, ...] = Field(min_length=1)
    selected_candidate_id: CandidateId
    engine_versions: FrozenMapping[str, str] = Field(min_length=1)
    # Artifact (model/heuristic) that produced the decision; it must appear in the manifest and
    # is what validation state is checked against before any autonomous execution.
    decision_artifact_hash: str
    regime: str = Field(min_length=1)
    evidence: EvidenceManifest
    confidence: ConfidenceAssessment
    autonomy_mode: AutonomyMode
    simulation_seed: SeedSpec | None = None
    notes: str = ""

    @model_validator(mode="after")
    def _invariants(self) -> Self:
        if self.information_cutoff > self.decision_time:
            raise ValueError("information_cutoff cannot be after decision_time")
        manifest = self.evidence
        if manifest.cutoff.as_of != self.information_cutoff:
            raise ValueError("evidence manifest cutoff must equal the decision information_cutoff")
        if manifest.cutoff.mode is not self.run.knowledge_mode:
            raise ValueError("evidence manifest knowledge mode must match the run context")
        if manifest.run_id != self.run.run_id or manifest.run_mode is not self.run.mode:
            raise ValueError("evidence manifest belongs to a different run")
        artifact_ids = {
            e.reference_id for e in manifest.entries if e.kind is EvidenceKind.MODEL_ARTIFACT
        }
        if self.decision_artifact_hash not in artifact_ids:
            raise ValueError("decision_artifact_hash must be declared in the evidence manifest")
        ids = [c.candidate_id for c in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate ids must be unique")
        if self.selected_candidate_id not in ids:
            raise ValueError("selected_candidate_id must reference a candidate")
        known = manifest.ids()
        for cand in self.candidates:
            missing = set(cand.evidence_ids) - known
            if missing:
                raise ValueError(f"candidate {cand.candidate_id} cites unknown evidence {missing}")
            allowed = _ALLOWED_ACTIONS[self.decision_type]
            if not isinstance(cand.action, allowed):
                raise ValueError(
                    f"{type(cand.action).__name__} is not a valid action for {self.decision_type}"
                )
        return self

    @property
    def selected(self) -> DecisionCandidate:
        return next(c for c in self.candidates if c.candidate_id == self.selected_candidate_id)


# --------------------------------------------------------------------------- lifecycle


class DecisionStatus(StrEnum):
    RECORDED = "recorded"  # OBSERVE mode / PAPER / REPLAY: kept for grading only
    RECOMMENDED = "recommended"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    EXECUTION_FAILED = "execution_failed"  # provider definitively did not execute
    EXECUTION_BLOCKED = "execution_blocked"  # a gate refused execution
    EXECUTION_UNCERTAIN = "execution_uncertain"  # provider outcome unknown; reconcile first
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class ActorKind(StrEnum):
    SYSTEM = "system"
    POLICY = "policy"
    USER = "user"


class Actor(DomainModel):
    kind: ActorKind
    actor_id: str


class ExecutionResult(DomainModel):
    provider: ProviderName
    success: bool
    executed_at: UtcDatetime
    provider_transaction_ref: str | None = None
    message: str = ""


class DecisionStatusEvent(DomainModel):
    event_id: StatusEventId = Field(default_factory=new_status_event_id)
    decision_id: DecisionId
    status: DecisionStatus
    occurred_at: UtcDatetime
    actor: Actor
    reason: str = ""
    execution_result: ExecutionResult | None = None

    @model_validator(mode="after")
    def _execution_result(self) -> Self:
        needs = self.status in (DecisionStatus.EXECUTED, DecisionStatus.EXECUTION_FAILED)
        if needs and self.execution_result is None:
            raise ValueError(f"{self.status} requires an execution_result")
        if self.execution_result is not None:
            ok = self.execution_result.success
            if (self.status is DecisionStatus.EXECUTED and not ok) or (
                self.status is DecisionStatus.EXECUTION_FAILED and ok
            ):
                raise ValueError("execution_result.success contradicts status")
        return self


# --------------------------------------------------------------------------- observed outcomes


class ObservedOutcome(DomainModel):
    """Facts that really happened. Never contains simulated or model-based values."""

    basis: Literal["observed"] = "observed"
    outcome_id: OutcomeId = Field(default_factory=new_outcome_id)
    decision_id: DecisionId
    known_at: UtcDatetime  # when these facts became knowable
    realized: FrozenMapping[str, float] = Field(min_length=1)
    # Realised values of the same metric for non-selected candidates, only where directly
    # observable (e.g. points a benched player actually scored). Absent = unknown, not zero.
    observed_alternatives: FrozenMapping[CandidateId, float] = Field(default_factory=dict)
    source_refs: tuple[str, ...] = ()
    notes: str = ""


class ObservedGrade(DomainModel):
    basis: Literal["observed"] = "observed"
    grade_id: GradeId = Field(default_factory=new_grade_id)
    decision_id: DecisionId
    outcome_id: OutcomeId
    graded_at: UtcDatetime
    grader_version: str
    metric: str
    decision_hash: str
    predicted_mean: float | None
    realized: float
    error: float | None
    pit: float | None = Field(default=None, ge=0.0, le=1.0)
    pit_out_of_range: bool = False
    observed_regret: float | None = None  # only over *observed* alternatives
    observed_rank: int | None = None
    alternatives_observed: int = 0


# --------------------------------------------------------------------------- counterfactuals


class CounterfactualEstimate(DomainModel):
    """A simulated/model-based estimate of what a non-selected candidate would have produced.

    Deliberately a different type (and table) from ``ObservedOutcome``: it can never be read,
    stored or graded as observed truth.
    """

    basis: Literal["model_based"] = "model_based"
    estimate_id: EstimateId = Field(default_factory=new_estimate_id)
    decision_id: DecisionId
    candidate_id: CandidateId
    metric: str
    distribution: OutcomeDistribution
    method: str = Field(min_length=1)
    computed_at: UtcDatetime
    model_artifact_hashes: tuple[str, ...] = Field(min_length=1)
    simulation_request_hash: str | None = None
    # Counterfactual evaluation may legitimately use post-decision facts (e.g. which players
    # actually got hurt). That is hindsight by design and must be declared.
    uses_post_decision_information: bool


class CounterfactualEvaluation(DomainModel):
    basis: Literal["model_based"] = "model_based"
    evaluation_id: GradeId = Field(default_factory=new_grade_id)
    decision_id: DecisionId
    outcome_id: OutcomeId
    estimate_ids: tuple[EstimateId, ...] = Field(min_length=1)
    graded_at: UtcDatetime
    grader_version: str
    metric: str
    decision_hash: str
    selected_realized: float  # observed
    counterfactual_means: FrozenMapping[CandidateId, float]  # model-based
    estimated_regret: float  # model-based: best counterfactual mean - observed selected
