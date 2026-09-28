# ADR 0012 — Execution attempts, idempotency, reconciliation and ledger concurrency

**Status:** accepted (v0.1.1)

## Decision
* **Execution-attempt log.** An append-only `execution_events` table holds attempt events:
  `ATTEMPT_STARTED` (the claim, with a lease), `PROVIDER_CONFIRMED`, `PROVIDER_REJECTED`,
  `OUTCOME_UNKNOWN`, `RECONCILED_EXECUTED` and `RECONCILED_NOT_EXECUTED`.
  * Each event carries the attempt id, a deterministic idempotency key (per decision and
    action), the action fingerprint, the provider, the worker and the provider transaction ref.
  * `derive_execution_state` is a pure function. Its phases are READY, IN_FLIGHT, UNCERTAIN and
    SUCCEEDED. An expired lease counts as UNCERTAIN.
* **Claim.** A worker must append `ATTEMPT_STARTED` with `expected_seq` under the
  per-decision lock. Only one claimant can win. The losers get `IN_PROGRESS_ELSEWHERE` and
  never call the provider. No database transaction is held open across the network call.
* **Unknown outcomes.** Timeouts, lost responses and hung providers are recorded as
  `OUTCOME_UNKNOWN`, and the decision status becomes `EXECUTION_UNCERTAIN`. The next attempt
  goes one of three ways:
  * Platforms with native idempotency may re-submit with the **same** key.
  * Otherwise the service reconciles against provider history:
    * `FOUND` means the action executed.
    * `NOT_FOUND` is trusted only after a settle window (default 2 minutes) has passed since
      the attempt started. After that it is safe to retry.
    * `UNDETERMINABLE` means stop and escalate.
  * Reconciliation needs only READ capability. A new attempt is fully re-authorised, so a
    capability revoked since the decision blocks the retry.
* A decision cannot be EXPIRED, SUPERSEDED or REJECTED while an execution is IN_FLIGHT or
  UNCERTAIN. `EXECUTION_UNCERTAIN` can only be resolved by reconciliation.
* **Ledger concurrency.**
  * Every append takes `SELECT ... FOR UPDATE` on the decision row in PostgreSQL. SQLite uses
    `BEGIN IMMEDIATE` on every transaction.
  * The append then re-reads the history, validates, and inserts with the next sequence
    number. Callers may pass `expected_seq` for optimistic concurrency.
  * The unique `(decision_id, seq)` constraint is a backstop. It surfaces as
    `ConcurrentModificationError`, never as a raw integrity error.
  * Identity writes use a PostgreSQL transaction-scoped advisory lock per
    (provider, entity_type).
