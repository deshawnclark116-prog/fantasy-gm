# ADR 0016 — Validation state, action risk classes and decision freshness

**Status:** accepted (v0.1.1). Amends ADR 0006.

## Decision
* **Confidence is descriptive only.** The v0.1 `min_autonomous_confidence` threshold is
  removed.
* **Validation state** comes from append-only `ModelValidationRecord`s: UNVALIDATED,
  VALIDATED, DEGRADED or REVOKED.
  * Records are keyed by artifact hash, decision type and an explicit regime. There is no
    wildcard.
  * AUTONOMOUS execution fails closed unless the decision artifact **and** its calibrator are
    VALIDATED for that decision type and regime at execution time.
  * An unregistered artifact counts as UNVALIDATED.
* **Risk policy** (`RiskPolicy`, configurable per league/user) classifies each action as LOW,
  MEDIUM, HIGH or CRITICAL:
  * lineup is LOW;
  * add-only is LOW, and add/drop is MEDIUM;
  * a waiver is MEDIUM, becoming HIGH above 20% of the FAAB budget and CRITICAL above 40%;
  * dropping a protected player is HIGH;
  * proposing a trade is HIGH, accepting one is CRITICAL, and declining one is MEDIUM;
  * a draft selection is CRITICAL.

  Autonomy additionally requires the risk class to be at or below `max_autonomous_risk`
  (default MEDIUM). No action class is banned in the architecture: raising the ceiling is a
  policy change.
* **Freshness** is decision-type and context specific (`FreshnessPolicy`). Defaults:

  | Decision | Normal limit | Tightened limit |
  |---|---|---|
  | Draft pick | 90 s | 30 s when on the clock |
  | Lineup / add / drop | 30 min | 5 min within 90 min of lock |
  | Waiver | 6 h | — |
  | Trade response | 12 h | — |
  | Trade proposal | 48 h | — |

  Age is measured from the information cutoff.
