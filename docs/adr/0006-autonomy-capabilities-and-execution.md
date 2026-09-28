# ADR 0006 — Autonomy modes, provider capabilities, and a separate execution path

**Status:** accepted

## Decision
* Modes: OBSERVE, RECOMMEND, APPROVAL_REQUIRED, AUTONOMOUS; per-decision-type overrides; the
  effective mode is the **most restrictive** of the decision's mode and the current policy.
* Providers declare `ProviderCapability`s (`READ`, `LINEUP_WRITE`, `ADD_DROP_WRITE`,
  `WAIVER_WRITE`, `TRADE_PROPOSE_WRITE`, `TRADE_RESPOND_WRITE`, `DRAFT_WRITE`). "Read-only" is
  the absence of write capabilities (`is_read_only`) rather than a `READ_ONLY` member, so a
  contradictory set cannot be declared. Every action type declares its required capability.
* `route_decision` (record time): missing capability → NOTIFY with `manual_action_required`;
  AUTONOMOUS is downgraded to approval on low confidence, insufficient evidence, or a FAAB bid
  above the autonomous ceiling.
* `authorize_execution` (execution time) re-checks: global kill switch, status, current policy
  mode, capability, decision staleness, and (for policy approvals) the autonomy blockers.
* `ExecutionService` is the only caller of provider writes; refusals are recorded as
  `execution_blocked`; provider exceptions as `execution_failed` with the error message.
* No credential scraping: provider adapters own authentication via official/authorised APIs.
