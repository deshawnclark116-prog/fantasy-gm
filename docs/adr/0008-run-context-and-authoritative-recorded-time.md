# ADR 0008 — Run context and authoritative recorded time

**Status:** accepted (v0.1.1)

## Context
In v0.1, `Decision.created_at` was supplied by the caller, so a live decision could be backdated.
Historical replays, though, legitimately describe 2024 decisions while being written in 2026.

## Decision
* Three distinct times:
  * `decision_time` is the logical time of the decision. It is historical in a replay.
  * `information_cutoff` is the knowledge boundary, and must be `<= decision_time`.
  * The ledger's `recorded_at` is the physical write time. The ledger stamps it from an
    injected `Clock` that it owns. It is not a field of `Decision`, so no caller can supply
    it.
* `RunContext(run_id, mode, knowledge_mode)` with three modes:
  * `LIVE` and `PAPER` must use `SYSTEM_KNOWLEDGE`.
  * `REPLAY` may use `PUBLIC_AVAILABILITY`.
* Ledger time invariants (`LedgerTimePolicy`, defaults: 5 min lag and 30 s future skew):
  * Every record: logical time `<= recorded_at + future_skew`.
  * LIVE/PAPER: `recorded_at - logical_time <= max_record_lag`. This applies to the decision,
    the evidence seal, every status event and every execution event, so none of them can be
    backdated.
  * REPLAY keeps its historical `decision_time`. `recorded_at` is still the real write time.
* PAPER and REPLAY decisions can only be recorded as `RECORDED`. The ledger refuses to store
  execution events for them.
* Knowledge sessions for LIVE/PAPER runs refuse cutoffs in the future. A REPLAY cutoff must be
  in the past.

## Consequences
Clock skew between services must be under the configured tolerance. A server-side database
clock would be stronger still (see REVIEW_NOTES).
