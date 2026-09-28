# Foundation v0.1.1: self-review, compromises, remaining weaknesses, open questions

Written by the implementation engineer for the architecture review of v0.1.1. The v0.1 notes
were resolved by this milestone or folded in below. Items are ordered by risk.

## A. Leakage paths found and closed during the v0.1.1 self-review

These were found while reviewing my own v0.1.1 implementation, beyond the review's list.

1. **Backdated `ingested_at` could leak backfill into SYSTEM_KNOWLEDGE replays.**
   `ingested_at` is supplied by the ingestion caller.
   * Fix: stores stamp their own `recorded_at`, and SYSTEM_KNOWLEDGE also requires
     `recorded_at <= cutoff`.
   * Test: `TestPhysicalStorageBoundsKnowledge`.
2. **PUBLIC_AVAILABILITY replays drift as more backfill arrives.**
   * Fix: `KnowledgeCutoff.stored_by` pins an experiment to a store snapshot.
   * Test: the snapshot-pin test in the same class.
3. **A decision could be EXPIRED or SUPERSEDED while an execution was in flight.** That would
   have hidden an execution that really happened.
   * Fix: the ledger refuses those transitions while the execution phase is IN_FLIGHT or
     UNCERTAIN, and `EXECUTION_UNCERTAIN` can only be resolved by reconciliation.
4. **Double execution through an early NOT_FOUND.** A request we timed out on can still be
   committed late by the platform.
   * Fix: NOT_FOUND is trusted only after a settle window.
   * Test: `test_retry_after_timeout_without_execution` shows the early retry being refused.
5. **The execution service silently skipped status updates** when a transition was invalid.
   * Fix: only `EXECUTION_BLOCKED` may be skipped. `EXECUTED`, `UNCERTAIN` and `FAILED` raise,
     and concurrent-append conflicts are retried.

## B. Design compromises

1. **Migration `0001_foundation` was rewritten in place instead of stacking a `0002`.** The
   v0.1 schema was never deployed and held no data (see ADR 0009). From now on, migrations are
   append-only.
2. **Physical time comes from an injected application clock**, not the database server clock.
   This keeps tests deterministic (`ManualClock`). PostgreSQL could enforce
   `recorded_at := now()` in a trigger; see D1.
3. **Store metadata is outside the payload hash.** `recorded_at`, `seq` and the index columns
   are not covered. Payload hashes detect corruption and naive edits of the payload. A
   privileged writer could still INSERT a self-consistent forged row. See C3.
4. **Isolation of the raw store from engines is structural, not absolute.**
   * The session keeps the store in a name-mangled attribute.
   * An AST architecture test (`tests/unit/test_architecture.py`) forbids engine modules from
     importing persistence or calling `known_as_of`.
   * Python cannot stop deliberate reflection.
5. **Sync SQLAlchemy runs under the async execution service** via `asyncio.to_thread`. Provider
   IO is async, and database calls are short and run in threads. An async database driver is
   deferred (D7).
6. **Hand-specified artifacts are allowed in replays.** They are flagged in the manifest as
   `hand_specified_artifacts`. Organizational Intent v0 is one, so disallowing them would
   block every backtest today.
7. **`CounterfactualEvaluation.estimated_regret` mixes quantities.** It compares the best
   *model-estimated* alternative with the *observed* selected outcome. It is labelled
   model-based, but it is not apples-to-apples (D6).
8. **SQLite (dev/test) takes `BEGIN IMMEDIATE` for every transaction, reads included.** Writes
   are therefore fully serialised. That is correct but slow, which is acceptable for dev.
   PostgreSQL uses row locks.
9. **The migration test runs Alembic `upgrade`/`downgrade` from empty.** There is no data
   migration yet to exercise.

## C. Remaining safety weaknesses

1. **Reconciliation is only as good as the adapter.** NOT_FOUND must mean that platform history
   is complete for the window and that fingerprints match the platform's representation of the
   action. The settle window (2 minutes) is a default, not a measured per-platform bound.
2. **Native idempotency is taken on trust.** If a platform's dedupe window is shorter than our
   retry horizon, a native-idempotent retry could double-execute. Adapters need to declare
   their dedupe window.
3. **No tamper evidence against privileged writers.** Anyone who can INSERT can append a
   forged but hash-consistent row, and a database superuser can drop the triggers. This needs
   a per-decision hash chain (or signed or anchored digests) plus an application database role
   without DDL or trigger privileges.
4. **Validation records have no authorisation.** `approved_by` is free text, and anything
   holding the registry can mark an artifact VALIDATED. Authentication and authorisation must
   exist before any real autonomy.
5. **The caller supplies `FreshnessContext`** (on-the-clock, minutes to lock). A wrong context
   loosens the staleness limits. It should be derived from schedule and draft-state
   observations in `LeagueState`.
6. **There is no operator path for a permanently UNCERTAIN execution** (reconciliation always
   UNDETERMINABLE). The decision stays uncertain forever, which is safe but operationally
   incomplete (D4).
7. **Identity corrections do not re-attribute already-ingested observations.**
   `source.identity_basis` makes the affected set findable, but there is no re-attribution
   workflow. Historical decisions correctly keep what they saw.
8. **`PlayerState.observed_roles` pools across teams and seasons** without regime or season
   discounts. The organizational-intent usage component does discount. Pooling rules belong
   to the future role model.
9. **Preseason `starters_rested` is team-level.** A veteran who played while the other
   starters rested is not distinguishable yet.
10. **Default LIVE record-lag tolerance is 5 minutes.** That may be too tight for queued
    workers and too loose for live drafts. It is configurable, but no per-decision-type value
    has been chosen.

## D. Questions for the next architecture decision

1. **Authoritative time and tamper evidence.** Should physical timestamps become
   database-authoritative (a PostgreSQL trigger plus a skew check), and should we add a
   per-decision hash chain or signed ledger digests now?
2. **Experiment protocol.** Should every PUBLIC_AVAILABILITY replay be required to pin
   `stored_by`? Should an experiment/run registry become a first-class durable record, holding
   the run context, pins, artifact set and code commit?
3. **Validation governance.**
   * Who may create validation records?
   * What evidence is required: calibration report schema, minimum forward sample, drift
     monitors?
   * What is the canonical taxonomy of regimes?
4. **Uncertain-execution resolution.** Do we allow a human "attested executed / attested not
   executed" event, and what evidence must it carry?
5. **Provider execution descriptor.** Where should per-adapter metadata live (native dedupe
   window, settle time, history-completeness guarantee, fingerprint normalisation)? Is it part
   of `capabilities`?
6. **Counterfactual method.** Should model-based evaluation compare model estimates for *all*
   candidates, including the selected one, instead of model vs observed?
7. **Async database access.** Adopt an async driver before the live-draft work, or keep sync
   calls in threads?
8. **Hand-specified artifacts in replays.** Keep allowing them flagged, or require an
   `authored_before` date and exclude replays earlier than it?
9. **Next milestone scope.** First real provider adapter (which vendor for which data, given
   timestamp-quality needs), or role-model scaffolding that consumes intent components as
   priors on role dimensions?
