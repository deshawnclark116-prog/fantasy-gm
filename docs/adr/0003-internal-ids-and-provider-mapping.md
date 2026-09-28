# ADR 0003 — Stable internal IDs with provider-ID mapping

**Status:** accepted; the one-shot mapping model is superseded by ADR 0014 (correctable identity). Internal-ID principles unchanged.

## Decision
* Internal IDs are random, prefixed (`plr_`, `nfl_`, `gam_`, `lg_`, `dec_` ...) and minted once.
  They are never derived from provider IDs.
* `ProviderIdMapping(internal_id, ProviderRef(provider, entity_type, external_id), method,
  confidence)`. Invariants (enforced in memory and by DB unique constraints): one ref maps to
  one internal ID; one internal ID has at most one ref per (provider, entity_type).
* Providers return provider-shaped records (`providers/records.py`). Ingestion mappers resolve
  refs through the registry and **fail** on unknown refs (`UnresolvedIdentityError`); new
  identities are created only by an explicit linking step.
* At execution time providers receive an `IdentityLookup` to translate internal → external IDs.

## Consequences
* A player-identity resolution workflow (crosswalk import, manual review queue for
  `NAME_MATCH` mappings) is required before real ingestion. Not built in v0.1.
