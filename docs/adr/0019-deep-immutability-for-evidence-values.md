# ADR 0019 — Deep immutability for evidence and decision-input values

**Status:** accepted (v0.1.2)

## Context

`DomainModel` sets `frozen=True`, which blocks attribute *reassignment* (`obj.field = x`), but a
`dict`-typed field is still a plain, mutable Python object: `obj.field[k] = v` succeeds silently.

`KnowledgeSession.read()` (ADR 0011) computes an observation's `content_hash()` into the sealed
evidence manifest and then hands that same object to an engine. If a nested container such as
`UsageSnapshot.metrics` could be mutated afterwards, the engine could go on to compute from a
value that no longer matches the hash already sealed into the manifest — the exact tamper-evidence
gap this milestone closes.

While auditing for this, a second, unrelated bug in the same neighbourhood was found: **CPython's
`frozenset` iteration order can depend on insertion history**, not only on the final elements.
Two `frozenset`s holding identical members can iterate — and therefore JSON-serialise, since a
plain list is not touched by `sort_keys=True` — in different orders depending on how they were
built (e.g. constructed directly versus reconstructed element-by-element from a JSON array during
`model_validate_json`). This silently broke `canonical_json()` / `content_hash()` stability across
an ordinary round trip (`tests/unit/test_domain_invariants.py::test_decision_hash_is_stable_and_content_sensitive`
flaked roughly 1 run in 5). `frozenset` itself needed no mutability fix — it already has no
mutating methods — but its *canonical serialisation* was not actually canonical.

## Decision

### `FrozenMapping[K, V]`

`src/fantasy_gm/domain/frozen.py` defines `FrozenMap`, a `Mapping` subclass with `__slots__` that
has no mutating methods (`__setitem__`, `__delitem__`, `update`, `pop`, `popitem`, `clear`,
`setdefault`, `__ior__`, `__setattr__` all raise `TypeError`), is hashable (cached
`hash(frozenset(items))`), and compares equal to any `Mapping` with the same contents. It copies
its input on construction, so a caller's dict can never alias the stored value.

`FrozenMapping[K, V] = Annotated[Mapping[K, V], <pydantic hook>]`:

* **Static typing:** the declared type is `collections.abc.Mapping`, so `mypy --strict` rejects
  `obj.field[k] = v` at the type level, independent of the runtime check.
* **Runtime:** a `no_info_after_validator_function` freezes the validated dict into a `FrozenMap`.
  Because `DomainModel.model_config` already sets `validate_default=True`, `default_factory` dicts
  are frozen the same way as explicitly-supplied ones — no special-casing needed.
* **Serialisation:** a `wrap_serializer_function_ser_schema` dumps `dict(value)` through the
  mapping's own inner schema, so `model_dump`, `canonical_json()` and every versioned codec
  produce byte-identical output to before this change.

Every `field_validator` on a field of this type must return the (already frozen) value unchanged,
or re-wrap any reconstructed dict in `FrozenMap` explicitly — a plain `dict(...)` return would
silently replace the frozen value with a fresh mutable one, since a field_validator's return value
becomes the final field value on top of the Annotated schema's own output.
`OutcomeDistribution._monotone` does this (`return FrozenMap(dict(ordered))`) after re-sorting.

### `FrozenJsonObject`

For opaque, arbitrarily-nested provider JSON (`ProviderLeagueSnapshot.raw`), `deep_freeze`
recursively converts every nested mapping to `FrozenMap`, every list/tuple to `tuple`, and every
set to `frozenset`, so nested structures are frozen at every level, not only the top one.

### `FrozenSet[T]`

Unlike mappings, a `frozenset` needs no wrapper class — it is already immutable. `FrozenSet[T]`
only changes *serialisation*: it always dumps a `sorted(value, key=str)` list rather than
iteration order. Every current element type (a `StrEnum` member or a `NewType(str)` id) is
string-like, so sorting by string value gives a total, deterministic order without inventing a new
domain ordering rule. Applied to every `frozenset`-typed model field (`Player.positions`,
`ScoringRules`/`RosterRules` position sets, `TimestampPolicy.accepted`,
`RiskPolicy.protected_player_ids`, `RoleDimensionSpec.positions`,
`ProviderPlayerRecord.positions`).

### Module-level lookup tables

The audit also covered non-pydantic module constants that are state machines or configuration
tables read by engines: `decisions.ledger.TRANSITIONS` / `INITIAL_STATUSES`,
`domain.decision._ALLOWED_ACTIONS`, `domain.roles.ROLE_DIMENSIONS`,
`organizational_intent.snapshot.COMPONENT_CATEGORY` / `COMPONENT_INFORMS`, and
`domain.risk._RANK`. These are wrapped in `types.MappingProxyType`: a mutation here would silently
change ledger-transition legality, or role/component semantics, for every future call at process
scope — the same bug class as an evidence value, just at module rather than instance scope.

### `WeeklyOutcomeResult.samples`

`WeeklyOutcomeResult` is a `@dataclass(frozen=True)`, not a `DomainModel` — no pydantic validation
is needed for a pure simulator return value, and `frozen=True` on a dataclass has exactly the same
shallow-freeze gap as pydantic's. `simulation.reference._sample_players` now wraps its result dict
in `types.MappingProxyType` before returning it; the per-array `arr.setflags(write=False)` from
v0.1.1 already protects each array's contents.

## What was audited and left unchanged

* **Tuple/frozenset-typed fields other than the mapping-adjacent ones above** were already
  immutable and needed no change.
* **`model_copy(update={...})`** bypasses pydantic validation by design (documented Pydantic v2
  behaviour, not specific to this change): a raw dict passed this way is *not* re-frozen. This
  creates a **new** object, though — it never mutates an object already read through a
  `KnowledgeSession` and sealed into a manifest, which is the property this ADR protects. Existing
  test-fixture usages of this pattern only affect locally-held objects that are re-validated (and
  therefore re-frozen) when they are later persisted and read back through a versioned codec.
* **`InMemoryModelRegistry._artifacts` / observation-store internal caches** are deliberately
  mutable: they are the store's own growing index, never handed to an engine as "the evidence".
* **`ObservationQuery.kinds`** (a query/filter parameter, never stored or hashed as evidence) was
  left as a plain `frozenset[str] | None` — out of scope for the same reason.
* **Function-parameter type hints** that already accepted `Mapping`/`frozenset` for read-only use
  (e.g. `leagues.roster_validation.eligibility`) needed no change.

## Consequences

* `ScoringRules.rate()` and `IntentConfigV0`-derived lookups now use `mapping.get(key)` followed by
  an explicit `is not None` check instead of `mapping.get(key, {})`, to avoid mypy ambiguity over
  the fallback's type against a `Mapping[..., Mapping[...]]` field.
* Discovering and fixing the `frozenset` serialisation-order bug means `content_hash()` is now
  provably stable across process restarts and hash-seed randomisation for every domain type that
  has a `frozenset` field — verified by rerunning the full suite under multiple explicit
  `PYTHONHASHSEED` values, not merely observed once.
* No canonical serialisation was weakened: byte-for-byte output for every existing field is
  unchanged (mapping order was already normalised by `sort_keys=True`); only the previously
  *non*-canonical `frozenset` list order was fixed, and golden v1 fixtures continue to decode
  unchanged (`tests/acceptance/test_foundation_v012.py::TestGoldenV1FixturesStillLoadAndAreDeeplyFrozen`).
