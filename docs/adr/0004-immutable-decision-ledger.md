# ADR 0004 — Immutable decision ledger with separate lifecycle, outcome and grade records

**Status:** accepted; amended by ADR 0008 (ledger-stamped recorded_at), ADR 0009 (versioned records), ADR 0012 (execution attempts, concurrency) and ADR 0013 (observed vs counterfactual).

## Context
Decisions must be graded later without the grade, the outcome, or later status changes being
able to alter what the decision saw or claimed.

## Decision
* `Decision` is a frozen record: candidates considered, selected candidate, model versions,
  information cutoff, knowledge mode, evidence references (each with `observed_at`), predictive
  distribution(s), confidence (incl. `insufficient_evidence`), autonomy mode, optional seed.
* Construction rejects: evidence observed after the cutoff; cutoff after creation; selected
  candidate not in candidates; candidate evidence not declared; actions invalid for the
  decision type.
* The ledger stores the decision's canonical JSON and its SHA-256; reads verify the stored
  bytes against the hash before parsing. Returned objects are fresh copies.
* Lifecycle (`recommended`, `awaiting_approval`, `approved`, `rejected`, `executed`,
  `execution_failed`, `execution_blocked`, `expired`, `superseded`, `recorded`) is an append-only
  sequence of `DecisionStatusEvent`s validated against an explicit transition table. Initial
  status must be consistent with (or more conservative than) the autonomy mode.
* `DecisionOutcome` and `DecisionGrade` are separate append-only records; outcomes must be
  recorded after the information cutoff; grades must quote the stored decision hash.
* SQL tables are protected by `BEFORE UPDATE/DELETE` triggers (SQLite and PostgreSQL).

## Deviation from the brief
The brief lists "whether it was recommended/approved/rejected/executed" and "execution result"
as parts of the decision snapshot. They are stored as events keyed by `decision_id` instead of
fields on the decision, because they happen *after* the decision and would otherwise require
mutating it. `LedgerEntry` presents the combined view.
