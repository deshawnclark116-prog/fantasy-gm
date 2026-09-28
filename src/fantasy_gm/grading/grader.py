"""Grading.

Two deliberately separate paths (ADR 0013):
* ``grade_observed`` -- compares the decision's prediction with facts that really happened, and
  computes regret only over alternatives whose outcomes were *observed*.
* ``evaluate_counterfactual`` -- a model-based evaluation using ``CounterfactualEstimate``s.
  Its output type is labelled ``model_based`` and can never be stored or read as observed.

Neither modifies the decision; both quote the stored decision hash.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from itertools import pairwise

from fantasy_gm.decisions.ledger import LedgerEntry
from fantasy_gm.domain.decision import (
    CounterfactualEstimate,
    CounterfactualEvaluation,
    ObservedGrade,
    ObservedOutcome,
    OutcomeDistribution,
)

GRADER_VERSION = "grader_v0.1.1"


def pit_from_quantiles(dist: OutcomeDistribution, realized: float) -> tuple[float | None, bool]:
    """Probability integral transform by linear interpolation between stored quantiles.

    Returns (pit, out_of_range). Outside the stored range the PIT is clamped and flagged.
    """
    if len(dist.quantiles) < 2:
        return None, False
    points = sorted(dist.quantiles.items())
    lo_p, lo_v = points[0]
    hi_p, hi_v = points[-1]
    if realized < lo_v:
        return lo_p, True
    if realized > hi_v:
        return hi_p, True
    for (p0, v0), (p1, v1) in pairwise(points):
        if v0 <= realized <= v1:
            if v1 == v0:
                return (p0 + p1) / 2, False
            return p0 + (p1 - p0) * (realized - v0) / (v1 - v0), False
    return None, False  # pragma: no cover - unreachable given monotone quantiles


def _metric(entry: LedgerEntry, outcome: ObservedOutcome) -> tuple[str, OutcomeDistribution | None]:
    dist = entry.decision.value.selected.estimated_outcome
    if dist is None and len(outcome.realized) != 1:
        raise ValueError("selected candidate has no estimated metric; outcome is ambiguous")
    metric = next(iter(outcome.realized)) if dist is None else dist.metric
    if metric not in outcome.realized:
        raise ValueError(f"outcome lacks realised value for metric {metric!r}")
    return metric, dist


def grade_observed(
    entry: LedgerEntry, outcome: ObservedOutcome, graded_at: datetime
) -> ObservedGrade:
    decision = entry.decision.value
    if outcome.decision_id != decision.decision_id:
        raise ValueError("outcome does not belong to this decision")
    metric, dist = _metric(entry, outcome)
    realized = outcome.realized[metric]
    pit, out_of_range = (None, False) if dist is None else pit_from_quantiles(dist, realized)
    sign = 1.0 if dist is None or dist.higher_is_better else -1.0
    selected = decision.selected_candidate_id
    observed = {cid: sign * v for cid, v in outcome.observed_alternatives.items()}
    observed[selected] = sign * realized
    regret: float | None = None
    rank: int | None = None
    if len(observed) > 1:
        mine = observed[selected]
        regret = max(observed.values()) - mine
        rank = 1 + sum(1 for v in observed.values() if v > mine)
    return ObservedGrade(
        decision_id=decision.decision_id,
        outcome_id=outcome.outcome_id,
        graded_at=graded_at,
        grader_version=GRADER_VERSION,
        metric=metric,
        decision_hash=entry.decision_hash,
        predicted_mean=None if dist is None else dist.mean,
        realized=realized,
        error=None if dist is None else realized - dist.mean,
        pit=pit,
        pit_out_of_range=out_of_range,
        observed_regret=regret,
        observed_rank=rank,
        alternatives_observed=len(observed) - 1,
    )


def evaluate_counterfactual(
    entry: LedgerEntry,
    outcome: ObservedOutcome,
    estimates: Sequence[CounterfactualEstimate],
    graded_at: datetime,
) -> CounterfactualEvaluation:
    decision = entry.decision.value
    metric, dist = _metric(entry, outcome)
    if not estimates:
        raise ValueError("counterfactual evaluation needs at least one estimate")
    for est in estimates:
        if est.decision_id != decision.decision_id or est.metric != metric:
            raise ValueError("estimate does not match this decision/metric")
    sign = 1.0 if dist is None or dist.higher_is_better else -1.0
    means = {est.candidate_id: est.distribution.mean for est in estimates}
    realized = outcome.realized[metric]
    best = max(sign * m for m in means.values())
    return CounterfactualEvaluation(
        decision_id=decision.decision_id,
        outcome_id=outcome.outcome_id,
        estimate_ids=tuple(e.estimate_id for e in estimates),
        graded_at=graded_at,
        grader_version=GRADER_VERSION,
        metric=metric,
        decision_hash=entry.decision_hash,
        selected_realized=realized,
        counterfactual_means=means,
        estimated_regret=max(0.0, best - sign * realized),
    )
