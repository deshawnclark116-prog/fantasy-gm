"""Decision ledger records.

A ``Decision`` is written once and never mutated. Everything that happens afterwards --
approval, execution, the realised outcome, grading -- is a *separate* append-only record keyed by
``decision_id`` (see ADR 0004).
"""

from __future__ import annotations

from enum import StrEnum
from itertools import pairwise
from typing import Self

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
from fantasy_gm.domain.identity import ProviderName
from fantasy_gm.domain.ids import (
    CandidateId,
    DecisionId,
    EvidenceId,
    FantasyTeamId,
    GradeId,
    LeagueId,
    OutcomeId,
    StatusEventId,
    new_candidate_id,
    new_decision_id,
    new_evidence_id,
    new_grade_id,
    new_outcome_id,
    new_status_event_id,
)
from fantasy_gm.domain.seeds import SeedSpec
from fantasy_gm.domain.time import KnowledgeMode, UtcDatetime

_ALLOWED_ACTIONS: dict[DecisionType, tuple[type[DomainModel], ...]] = {
    DecisionType.DRAFT_PICK: (DraftSelection,),
    DecisionType.LINEUP: (SetLineup, NoAction),
    DecisionType.WAIVER_CLAIM: (WaiverClaim, NoAction),
    DecisionType.FREE_AGENT_ADD: (FreeAgentAddDrop, NoAction),
    DecisionType.DROP: (FreeAgentAddDrop, NoAction),
    DecisionType.TRADE_PROPOSAL: (ProposeTrade, NoAction),
    DecisionType.TRADE_RESPONSE: (RespondToTrade,),
}


class OutcomeDistribution(DomainModel):
    """A predictive distribution for one scalar metric (e.g. delta championship probability,
    weekly points). Quantile keys are probabilities in (0, 1)."""

    metric: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    mean: float
    stdev: float | None = Field(default=None, ge=0.0)
    quantiles: dict[float, float] = Field(default_factory=dict)
    n_samples: int | None = Field(default=None, ge=1)
    method: str = Field(min_length=1)
    higher_is_better: bool = True

    @field_validator("quantiles")
    @classmethod
    def _monotone(cls, value: dict[float, float]) -> dict[float, float]:
        ordered = sorted(value.items())
        for prob, _ in ordered:
            if not 0.0 < prob < 1.0:
                raise ValueError("quantile probabilities must be in (0, 1)")
        for (_, lo), (_, hi) in pairwise(ordered):
            if hi < lo:
                raise ValueError("quantile values must be non-decreasing")
        return dict(ordered)


class ConfidenceAssessment(DomainModel):
    score: float = Field(ge=0.0, le=1.0)
    insufficient_evidence: bool
    reasons: tuple[str, ...] = ()


class EvidenceKind(StrEnum):
    OBSERVATION = "observation"
    INTENT_SNAPSHOT = "intent_snapshot"
    PLAYER_STATE = "player_state"
    PROJECTION = "projection"
    SIMULATION_RUN = "simulation_run"
    LEAGUE_STATE = "league_state"
    MARKET = "market"


class DecisionEvidence(DomainModel):
    """A pointer to information the decision used, with the time it became knowable."""

    evidence_id: EvidenceId = Field(default_factory=new_evidence_id)
    kind: EvidenceKind
    reference_id: str = Field(min_length=1)
    observed_at: UtcDatetime
    summary: str
    reference_hash: str | None = None  # hash of the referenced record, when available


class DecisionCandidate(DomainModel):
    candidate_id: CandidateId = Field(default_factory=new_candidate_id)
    action: Action
    estimated_outcome: OutcomeDistribution | None = None
    rationale: str = ""
    evidence_ids: tuple[EvidenceId, ...] = ()


class Decision(DomainModel):
    decision_id: DecisionId = Field(default_factory=new_decision_id)
    league_id: LeagueId
    fantasy_team_id: FantasyTeamId
    decision_type: DecisionType
    created_at: UtcDatetime
    information_cutoff: UtcDatetime
    knowledge_mode: KnowledgeMode = KnowledgeMode.SYSTEM_KNOWLEDGE
    candidates: tuple[DecisionCandidate, ...] = Field(min_length=1)
    selected_candidate_id: CandidateId
    model_versions: dict[str, str] = Field(min_length=1)
    evidence: tuple[DecisionEvidence, ...] = ()
    confidence: ConfidenceAssessment
    autonomy_mode: AutonomyMode
    simulation_seed: SeedSpec | None = None
    notes: str = ""

    @model_validator(mode="after")
    def _invariants(self) -> Self:
        if self.information_cutoff > self.created_at:
            raise ValueError("information_cutoff cannot be after created_at")
        for ev in self.evidence:
            if ev.observed_at > self.information_cutoff:
                raise ValueError(
                    f"evidence {ev.evidence_id} observed_at {ev.observed_at.isoformat()} is after "
                    f"the information cutoff {self.information_cutoff.isoformat()} (leakage)"
                )
        ids = [c.candidate_id for c in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate ids must be unique")
        if self.selected_candidate_id not in ids:
            raise ValueError("selected_candidate_id must reference a candidate")
        evidence_ids = {e.evidence_id for e in self.evidence}
        for cand in self.candidates:
            missing = set(cand.evidence_ids) - evidence_ids
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
    RECORDED = "recorded"  # OBSERVE mode: kept for grading only
    RECOMMENDED = "recommended"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    EXECUTION_FAILED = "execution_failed"
    EXECUTION_BLOCKED = "execution_blocked"  # a gate refused execution (capability, staleness..)
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


# --------------------------------------------------------------------------- outcomes & grades


class DecisionOutcome(DomainModel):
    """What actually happened. Attached later; never alters the decision it describes."""

    outcome_id: OutcomeId = Field(default_factory=new_outcome_id)
    decision_id: DecisionId
    recorded_at: UtcDatetime
    realized: dict[str, float] = Field(min_length=1)
    # Realised value of the *same metric* for non-selected candidates, where it is observable
    # (e.g. the points a benched player scored). Absent entries are unknown, not zero.
    candidate_realized: dict[CandidateId, float] = Field(default_factory=dict)
    evidence: tuple[DecisionEvidence, ...] = ()
    notes: str = ""


class DecisionGrade(DomainModel):
    grade_id: GradeId = Field(default_factory=new_grade_id)
    decision_id: DecisionId
    outcome_id: OutcomeId
    graded_at: UtcDatetime
    grader_version: str
    metric: str
    decision_hash: str  # hash of the decision as recorded; proves what was graded
    predicted_mean: float | None
    realized: float
    error: float | None  # realized - predicted_mean
    pit: float | None = Field(default=None, ge=0.0, le=1.0)  # probability integral transform
    pit_out_of_range: bool = False
    regret: float | None = None  # best observed candidate value - selected value (>= 0)
    selected_rank: int | None = None  # 1 = best among candidates with observed values
    candidates_observed: int = 0
