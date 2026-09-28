# ADR 0009 — Versioned, hash-verified durable records

**Status:** accepted (v0.1.1)

## Decision
* The following are stored as `record_type` + `schema_version` + canonical JSON **text** +
  SHA-256 of that text + `recorded_at`:
  * decisions, status events, execution events, observed outcomes and grades;
  * counterfactual estimates and evaluations;
  * observations, identity-mapping events, model artifacts and validation records.
* `VersionedCodec` handles both directions:
  * **Write:** encode the current model.
  * **Read:** verify the stored bytes against the stored hash **before** parsing, then dispatch
    on `schema_version` to a reader. Legacy readers adapt old payloads to the current model at
    read time. Historical payloads are never rewritten.
* Golden v1 fixtures live in `tests/fixtures/records/` and must decode with the production
  codecs. If a model changes incompatibly, the fixture test fails until a legacy reader is
  registered and the codec version is bumped.
* PostgreSQL JSONB normalises key order and whitespace, so it is never used for integrity. The
  text column is authoritative. `observations.payload_json` is a derived copy for ad-hoc
  queries only.

## Migration note
The pre-release v0.1 schema (commit 0755909) was never deployed and never held data. Migration
`0001_foundation` was therefore rewritten in place, not stacked. From v0.1.1 onward migrations
are append-only.
