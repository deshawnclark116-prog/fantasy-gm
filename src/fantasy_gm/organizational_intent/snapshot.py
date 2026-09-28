"""Organizational intent snapshot: component signals + confidence + provenance."""

from __future__ import annotations

import math
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Self

from pydantic import Field, model_validator

from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.frozen import FrozenMapping
from fantasy_gm.domain.identity import ProviderName
from fantasy_gm.domain.ids import NFLTeamId, ObservationId, PlayerId, SnapshotId, new_snapshot_id
from fantasy_gm.domain.nfl import Position
from fantasy_gm.domain.roles import ROLE_DIMENSIONS, RoleDimension, RoleFamily
from fantasy_gm.domain.time import KnowledgeMode, UtcDatetime


class IntentComponent(StrEnum):
    DRAFT_INVESTMENT = "draft_investment"
    CONTRACT_INVESTMENT = "contract_investment"
    ROSTER_COMPETITION = "roster_competition"
    RECENT_TRANSACTIONS = "recent_transactions"
    DEPTH_CHART = "depth_chart"
    COACHING_CONTINUITY = "coaching_continuity"
    PRESEASON_DEPLOYMENT = "preseason_deployment"
    ACTUAL_USAGE = "actual_usage"


class ComponentCategory(StrEnum):
    PRIOR = "prior"  # organisational signals that precede/predict deployment
    EVIDENCE = "evidence"  # observed real NFL deployment
    CONTEXT = "context"  # modifies trust in other components; not itself a commitment level


# Module-level lookup tables, not pydantic fields; wrapped so they cannot be mutated in place
# at process scope, which would silently change every future snapshot computed (ADR 0019).
COMPONENT_CATEGORY: Mapping[IntentComponent, ComponentCategory] = MappingProxyType(
    {
        IntentComponent.DRAFT_INVESTMENT: ComponentCategory.PRIOR,
        IntentComponent.CONTRACT_INVESTMENT: ComponentCategory.PRIOR,
        IntentComponent.ROSTER_COMPETITION: ComponentCategory.PRIOR,
        IntentComponent.RECENT_TRANSACTIONS: ComponentCategory.PRIOR,
        IntentComponent.DEPTH_CHART: ComponentCategory.PRIOR,
        IntentComponent.PRESEASON_DEPLOYMENT: ComponentCategory.PRIOR,
        IntentComponent.COACHING_CONTINUITY: ComponentCategory.CONTEXT,
        IntentComponent.ACTUAL_USAGE: ComponentCategory.EVIDENCE,
    }
)


# Which latent-role families each component is evidence about. Future role models consume
# components as priors/covariates on exactly these dimensions -- never as one intent score.
COMPONENT_INFORMS: Mapping[IntentComponent, tuple[RoleFamily, ...]] = MappingProxyType(
    {
        IntentComponent.DRAFT_INVESTMENT: (RoleFamily.DEPLOYMENT, RoleFamily.TARGET_EARNING),
        IntentComponent.CONTRACT_INVESTMENT: (RoleFamily.DEPLOYMENT,),
        IntentComponent.ROSTER_COMPETITION: (RoleFamily.DEPLOYMENT,),
        IntentComponent.RECENT_TRANSACTIONS: (RoleFamily.DEPLOYMENT,),
        IntentComponent.DEPTH_CHART: (RoleFamily.DEPLOYMENT,),
        IntentComponent.COACHING_CONTINUITY: (RoleFamily.ENVIRONMENT,),
        IntentComponent.PRESEASON_DEPLOYMENT: (RoleFamily.DEPLOYMENT,),
        IntentComponent.ACTUAL_USAGE: (RoleFamily.DEPLOYMENT,),
    }
)


def informed_dimensions(
    component: IntentComponent, position: Position
) -> tuple[RoleDimension, ...]:
    families = COMPONENT_INFORMS[component]
    return tuple(
        d
        for d, spec in ROLE_DIMENSIONS.items()
        if position in spec.positions and spec.family in families and spec.supported
    )


class ProvenanceRecord(DomainModel):
    observation_id: ObservationId
    observation_kind: str
    provider: ProviderName
    observed_at: UtcDatetime
    effective_at: UtcDatetime
    fact: str


class ComponentSignal(DomainModel):
    """One component. ``score`` is a commitment level in [0, 1] (1 = strongest organisational
    commitment to a primary role), or ``None`` when evidence is absent/unusable. For CONTEXT
    components the score is a continuity level instead.

    ``influence`` is this component's share of the snapshot's evidence mass after prior decay.
    It is an explicit, inspectable bookkeeping quantity -- NOT a validated predictive weight.
    """

    component: IntentComponent
    category: ComponentCategory
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    influence: float = Field(default=0.0, ge=0.0, le=1.0)
    method: str
    informs: tuple[RoleDimension, ...] = ()
    measurements: FrozenMapping[str, float] = Field(default_factory=dict)
    provenance: tuple[ProvenanceRecord, ...] = ()
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _absent_means_no_confidence(self) -> Self:
        if self.score is None and (self.confidence > 0 or self.influence > 0):
            raise ValueError("a component without a score cannot carry confidence or influence")
        if self.category is ComponentCategory.CONTEXT and self.influence > 0:
            raise ValueError("context components never carry influence")
        return self


class EvidenceBalance(DomainModel):
    usage_effective_games: float = Field(ge=0.0)
    prior_pseudo_games: float = Field(gt=0.0)
    usage_share: float = Field(ge=0.0, le=1.0)
    prior_share: float = Field(ge=0.0, le=1.0)


class ConflictKind(StrEnum):
    USAGE_BELOW_PRIORS = "usage_below_priors"
    USAGE_ABOVE_PRIORS = "usage_above_priors"


class IntentConflict(DomainModel):
    kind: ConflictKind
    prior_consensus: float
    usage_score: float
    magnitude: float
    prior_components: tuple[IntentComponent, ...]
    explanation: str


class OrganizationalIntentSnapshot(DomainModel):
    snapshot_id: SnapshotId = Field(default_factory=new_snapshot_id)
    player_id: PlayerId
    team_id: NFLTeamId | None
    position: Position
    season: int
    as_of: UtcDatetime
    knowledge_mode: KnowledgeMode
    computed_at: UtcDatetime
    engine_version: str
    config_hash: str
    components: tuple[ComponentSignal, ...]
    evidence_balance: EvidenceBalance
    conflicts: tuple[IntentConflict, ...] = ()
    insufficient_evidence: bool
    insufficiency_reasons: tuple[str, ...] = ()
    regime_start: UtcDatetime | None = None

    @model_validator(mode="after")
    def _invariants(self) -> Self:
        seen = [c.component for c in self.components]
        if sorted(seen) != sorted(IntentComponent):
            raise ValueError("snapshot must contain exactly one signal per IntentComponent")
        total = sum(c.influence for c in self.components)
        if not (math.isclose(total, 1.0, abs_tol=1e-9) or math.isclose(total, 0.0, abs_tol=1e-12)):
            raise ValueError(f"component influences must sum to 1 (or 0); got {total}")
        if self.computed_at < self.as_of:
            raise ValueError("computed_at cannot precede as_of")
        return self

    def component(self, component: IntentComponent) -> ComponentSignal:
        return next(c for c in self.components if c.component is component)

    @property
    def missing_components(self) -> tuple[IntentComponent, ...]:
        return tuple(c.component for c in self.components if c.score is None)
