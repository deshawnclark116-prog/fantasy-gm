# ADR 0013 — Observed outcomes vs model-based counterfactuals

**Status:** accepted (v0.1.1). Amends ADR 0004.

## Decision
* **Observed side:** `ObservedOutcome` and `ObservedGrade`.
  * Both are marked `basis: Literal["observed"]`.
  * Regret and rank are computed only over *observed* alternatives, for example the points a
    benched player actually scored.
* **Model-based side:** `CounterfactualEstimate` and `CounterfactualEvaluation`.
  * Both are marked `basis: Literal["model_based"]`.
  * They carry the model artifact hashes and simulation request hash.
  * They declare `uses_post_decision_information`.
  * They are stored in a separate `decision_counterfactuals` table.
* The two families are different types and codecs. With `extra="forbid"` and literal `basis`
  fields, one can never be parsed, encoded or stored as the other.
* `grade_observed` and `evaluate_counterfactual` are separate functions. Neither modifies the
  decision, and both quote the stored decision hash.
