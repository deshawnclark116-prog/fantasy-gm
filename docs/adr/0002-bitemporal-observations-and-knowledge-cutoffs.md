# ADR 0002 — Bitemporal, append-only observations and explicit knowledge cutoffs

**Status:** accepted; amended by ADR 0010 (timestamp trust, physical storage bound) and ADR 0011 (reads only via knowledge sessions).

## Context
Principles 9 and 10 require every decision to be reconstructable using only information that
was available at decision time, with no hindsight leakage (stat corrections, later depth-chart
updates, backfilled injury reports).

## Decision
* Every time-varying fact is an `Observation` with `effective_at` (true in the world),
  `observed_at` (publicly observable) and `source.ingested_at` (received by us).
* Observations are never updated. A revision is a new observation sharing the same `fact_key`.
* `domain.knowledge.resolve_known(observations, cutoff)` is the **only** implementation of
  "what was known": filter by knowability, then keep the latest-known revision per fact.
  Every store delegates to it.
* `KnowledgeCutoff.mode`:
  * `SYSTEM_KNOWLEDGE` (default): `observed_at <= as_of` **and** `ingested_at <= as_of`.
    Live decisions must use it.
  * `PUBLIC_AVAILABILITY`: `observed_at <= as_of` only — required for backtests on backfilled
    data; only as honest as providers' publication timestamps.
* Store protocols expose no unfiltered read method.
* Pure engines additionally call `assert_known` on their inputs (defence in depth).
* Timestamps are UTC-aware; naive datetimes are rejected at validation and at the DB boundary.

## Consequences
* Point-in-time queries resolve revisions in Python after an indexed SQL pre-filter. Fine at
  v0.1 scale; will need a window-function query or materialised views at full-league scale.
