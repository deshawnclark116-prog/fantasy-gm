# ADR 0005 — Organizational Intent v0: components, not a score

**Status:** accepted (v0; expected to change after validation)

## Decision
* The snapshot contains one `ComponentSignal` per component: draft investment, contract
  investment, roster competition, recent transactions, depth chart, coaching continuity,
  preseason deployment, actual usage. Each has `score ∈ [0,1] | None`, `confidence`, `method`,
  raw `measurements`, `provenance` (observation IDs + facts) and `notes`.
* **No aggregate intent score.** Nothing is claimed about predictive weights.
* Categories: PRIOR (organisational signals), EVIDENCE (actual regular-season usage), CONTEXT
  (coaching continuity — it changes trust in other evidence rather than expressing commitment).
* Evidence balance: `usage_share = n_eff / (n_eff + k)` where `n_eff` counts regular-season
  games with the current team (discounted for prior seasons, previous coaching regime, and
  fallback metrics) and `k = prior_pseudo_games`. Priors share the remainder in proportion to
  their confidence. `influence` is reported per component so it is inspectable, and is
  explicitly *not* a validated weight.
* Evidence produced under a previous coaching regime (before the latest HC/play-caller change)
  is discounted.
* A conflict is emitted when usage diverges from the confidence-weighted prior consensus by more
  than a threshold after enough games.
* Explicit refusals to invent data: rookie contracts defer to draft capital; contracts without a
  positional-market percentile are unscored; total preseason snap share is not used as a
  first-team proxy; absence of transactions is unscored (feed coverage unknown); depth charts
  inferred from usage are excluded (double counting).
* All tunables live in `IntentConfigV0`, whose hash is stored on every snapshot.

## Consequences
Component scores live on heterogeneous scales; comparing them (conflict detection) is a
heuristic. Forward validation must decide which components survive and how they combine.
