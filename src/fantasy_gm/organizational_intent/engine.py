"""Organizational Intent v0 engine: evidence bundle -> explainable snapshot (pure)."""

from __future__ import annotations

from datetime import datetime

from fantasy_gm.domain.knowledge import LeakageError, assert_known
from fantasy_gm.organizational_intent import components as c
from fantasy_gm.organizational_intent.config import IntentConfigV0
from fantasy_gm.organizational_intent.inputs import IntentInputs
from fantasy_gm.organizational_intent.snapshot import (
    COMPONENT_CATEGORY,
    ComponentCategory,
    ComponentSignal,
    ConflictKind,
    EvidenceBalance,
    IntentComponent,
    IntentConflict,
    OrganizationalIntentSnapshot,
)

ENGINE_VERSION = "org_intent_v0"


def compute_intent_snapshot(
    inputs: IntentInputs,
    computed_at: datetime,
    config: IntentConfigV0 | None = None,
) -> OrganizationalIntentSnapshot:
    cfg = config or IntentConfigV0()
    as_of = inputs.cutoff.as_of

    # Defence in depth: the gatherer already filtered, but the engine refuses leaked inputs.
    observations = list(inputs.all_observations())
    assert_known(observations, inputs.cutoff)
    for obs in observations:
        if obs.effective_at > as_of:
            raise LeakageError(f"{obs.kind} {obs.observation_id} is not yet effective at {as_of}")

    coaching = c.coaching_continuity(inputs.coaching, inputs.team_id, as_of, cfg)
    regime_start = coaching.regime_start
    usage_result, n_eff = c.actual_usage(
        inputs.usage, inputs.position, inputs.season, inputs.team_id, regime_start, cfg
    )
    results: dict[IntentComponent, c.ComponentResult] = {
        IntentComponent.DRAFT_INVESTMENT: c.draft_investment(
            inputs.draft, inputs.team_id, as_of, cfg
        ),
        IntentComponent.CONTRACT_INVESTMENT: c.contract_investment(
            inputs.contracts, inputs.team_id, cfg
        ),
        IntentComponent.ROSTER_COMPETITION: c.roster_competition(
            inputs.draft,
            inputs.contracts,
            inputs.competitors,
            inputs.position,
            inputs.team_id,
            as_of,
            cfg,
        ),
        IntentComponent.RECENT_TRANSACTIONS: c.recent_transactions(
            inputs.transactions,
            inputs.player.player_id,
            inputs.position,
            inputs.team_id,
            as_of,
            cfg,
        ),
        IntentComponent.DEPTH_CHART: c.depth_chart(
            inputs.depth_charts, inputs.position, inputs.team_id, as_of, regime_start, cfg
        ),
        IntentComponent.COACHING_CONTINUITY: coaching.component,
        IntentComponent.PRESEASON_DEPLOYMENT: c.preseason_deployment(
            inputs.usage, inputs.season, inputs.team_id, cfg
        ),
        IntentComponent.ACTUAL_USAGE: usage_result,
    }

    influences, balance = _allocate_influence(results, n_eff, cfg)
    signals = tuple(
        ComponentSignal(
            component=comp,
            category=COMPONENT_CATEGORY[comp],
            score=res.score,
            confidence=res.confidence if res.score is not None else 0.0,
            influence=influences.get(comp, 0.0),
            method=res.method,
            measurements=res.measurements,
            provenance=res.provenance,
            notes=res.notes,
        )
        for comp, res in ((comp, results[comp]) for comp in IntentComponent)
    )
    conflicts = _detect_conflicts(results, n_eff, cfg)
    insufficient, reasons = _sufficiency(results, n_eff, inputs.team_id is None)

    return OrganizationalIntentSnapshot(
        player_id=inputs.player.player_id,
        team_id=inputs.team_id,
        position=inputs.position,
        season=inputs.season,
        as_of=as_of,
        knowledge_mode=inputs.cutoff.mode,
        computed_at=computed_at,
        engine_version=ENGINE_VERSION,
        config_hash=cfg.content_hash(),
        components=signals,
        evidence_balance=balance,
        conflicts=conflicts,
        insufficient_evidence=insufficient,
        insufficiency_reasons=reasons,
        regime_start=regime_start,
    )


def _scored_priors(
    results: dict[IntentComponent, c.ComponentResult],
) -> dict[IntentComponent, c.ComponentResult]:
    return {
        comp: res
        for comp, res in results.items()
        if COMPONENT_CATEGORY[comp] is ComponentCategory.PRIOR
        and res.score is not None
        and res.confidence > 0
    }


def _allocate_influence(
    results: dict[IntentComponent, c.ComponentResult], n_eff: float, cfg: IntentConfigV0
) -> tuple[dict[IntentComponent, float], EvidenceBalance]:
    """Split evidence mass between observed usage and priors.

    usage_share = n_eff / (n_eff + prior_pseudo_games): each effective game of real usage
    shifts mass away from priors; priors split the remainder in proportion to confidence.
    Equal treatment of priors (up to confidence) is a deliberate "we don't know the weights yet".
    """
    priors = _scored_priors(results)
    usage_scored = results[IntentComponent.ACTUAL_USAGE].score is not None and n_eff > 0
    usage_share = n_eff / (n_eff + cfg.prior_pseudo_games) if usage_scored else 0.0
    if not priors:
        usage_share = 1.0 if usage_scored else 0.0
    prior_share = (1.0 - usage_share) if priors else 0.0
    influences: dict[IntentComponent, float] = {}
    if usage_scored:
        influences[IntentComponent.ACTUAL_USAGE] = usage_share
    total_conf = sum(r.confidence for r in priors.values())
    for comp, res in priors.items():
        influences[comp] = prior_share * res.confidence / total_conf
    balance = EvidenceBalance(
        usage_effective_games=n_eff,
        prior_pseudo_games=cfg.prior_pseudo_games,
        usage_share=usage_share,
        prior_share=prior_share,
    )
    return influences, balance


def _detect_conflicts(
    results: dict[IntentComponent, c.ComponentResult], n_eff: float, cfg: IntentConfigV0
) -> tuple[IntentConflict, ...]:
    usage = results[IntentComponent.ACTUAL_USAGE]
    priors = _scored_priors(results)
    if usage.score is None or not priors or n_eff < cfg.conflict_min_effective_games:
        return ()
    consensus = c.weighted_mean(
        (res.score, res.confidence) for res in priors.values() if res.score is not None
    )
    if consensus is None:
        return ()
    gap = usage.score - consensus
    if abs(gap) < cfg.conflict_threshold:
        return ()
    kind = ConflictKind.USAGE_BELOW_PRIORS if gap < 0 else ConflictKind.USAGE_ABOVE_PRIORS
    return (
        IntentConflict(
            kind=kind,
            prior_consensus=consensus,
            usage_score=usage.score,
            magnitude=abs(gap),
            prior_components=tuple(priors),
            explanation=(
                f"observed usage ({usage.score:.2f}) diverges from confidence-weighted prior "
                f"commitment ({consensus:.2f}) over {n_eff:.1f} effective games; real usage is "
                "the stronger evidence as it accumulates"
            ),
        ),
    )


def _sufficiency(
    results: dict[IntentComponent, c.ComponentResult], n_eff: float, no_team: bool
) -> tuple[bool, tuple[str, ...]]:
    reasons: list[str] = []
    if no_team:
        reasons.append("player has no current NFL team")
    priors = _scored_priors(results)
    if n_eff < 1.0 and len(priors) < 2:
        reasons.append(
            f"only {len(priors)} scored prior component(s) and {n_eff:.2f} effective usage games"
        )
    return bool(reasons), tuple(reasons)
