"""Grade a recorded decision against a later outcome.

The grader reads the decision exactly as recorded (hash-verified by the ledger) and never
modifies it. Grades are append-only records carrying the decision hash they were computed
against.
"""

from __future__ import annotations

from datetime import datetime
from itertools import pairwise

from fantasy_gm.decisions.ledger import LedgerEntry
from fantasy_gm.domain.decision import DecisionGrade, DecisionOutcome, OutcomeDistribution

GRADER_VERSION = "grader_v0"


def pit_from_quantiles(dist: OutcomeDistribution, realized: float) -> tuple[float | None, bool]:
    """Probability integral transform by linear interpolation between stored quantiles.

    Returns (pit, out_of_range). Outside the stored quantile range the PIT is clamped to the
    nearest stored probability and flagged; uniformity of PITs across many decisions is the
    calibration check.
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


def grade_decision(
    entry: LedgerEntry, outcome: DecisionOutcome, graded_at: datetime
) -> DecisionGrade:
    decision = entry.decision
    if outcome.decision_id != decision.decision_id:
        raise ValueError("outcome does not belong to this decision")
    selected = decision.selected
    dist = selected.estimated_outcome
    if dist is None and len(outcome.realized) != 1:
        raise ValueError("selected candidate has no estimated metric; outcome is ambiguous")
    metric = next(iter(outcome.realized)) if dist is None else dist.metric
    if metric not in outcome.realized:
        raise ValueError(f"outcome lacks realised value for metric {metric!r}")
    realized = outcome.realized[metric]

    pit, out_of_range = (None, False) if dist is None else pit_from_quantiles(dist, realized)

    # Orient so that larger == better for regret/rank.
    sign = 1.0 if dist is None or dist.higher_is_better else -1.0
    observed = {cid: sign * v for cid, v in outcome.candidate_realized.items()}
    observed.setdefault(selected.candidate_id, sign * realized)
    regret: float | None = None
    rank: int | None = None
    if len(observed) > 1:
        mine = observed[selected.candidate_id]
        regret = max(observed.values()) - mine
        rank = 1 + sum(1 for v in observed.values() if v > mine)

    return DecisionGrade(
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
        regret=regret,
        selected_rank=rank,
        candidates_observed=len(observed),
    )
