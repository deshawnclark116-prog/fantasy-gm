# ADR 0014 — Correctable, auditable identity mapping

**Status:** accepted (v0.1.1). Supersedes the one-shot mapping in ADR 0003; internal-ID
principles in ADR 0003 are unchanged.

## Decision
* Mappings are an append-only history of `IdentityMappingEvent`s. Each event records:
  * the ref and the internal id;
  * a status: VERIFIED, UNVERIFIED, CONFLICTED or RETRACTED;
  * the method, confidence and provenance (`asserted_by`, `evidence`);
  * `supersedes_event_id` and `reason`.
* Invariants:
  * **No silent reassignment.** A change must supersede the ref's current event and give a
    reason.
  * A `NAME_MATCH` can never be VERIFIED directly.
  * At most one VERIFIED ref per (internal id, provider, entity type) at a time.
* `resolve(ref, as_of)` returns the mapping believed at `as_of`, using the registry's own
  `recorded_at`.
* Ingestion accepts VERIFIED mappings only. Anything else raises `UnresolvedIdentityError`
  with a reason, and there is no fuzzy matching during ingestion.
* Each observation records the mapping events it was attributed through
  (`source.identity_basis`), so a correction can find the observations it affects.

## Consequences
Re-attributing observations after a correction is a future, explicit operation. Old
observations stay as they were ingested.
