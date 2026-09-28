"""Deeply immutable containers for domain values (ADR 0019).

Pydantic ``frozen=True`` only blocks attribute *reassignment*; a ``dict`` field can still be
mutated in place. Evidence admitted into a knowledge session must be immutable all the way
down, otherwise an engine could compute from a value that no longer matches the evidence hash
sealed in the manifest. So every mapping-typed domain field is declared as

    FrozenMapping[K, V]   ==   Annotated[Mapping[K, V], <validator>]

* **Static typing:** the declared type is the read-only ``collections.abc.Mapping``, so mypy
  rejects ``obj.field[k] = v`` at type-check time.
* **Runtime:** the validated value is a ``FrozenMap``, which has no mutating methods and
  raises ``TypeError`` on item assignment/deletion. Input dicts are copied, so the caller's
  dict cannot alias the stored value.
* **Serialisation is unchanged:** a ``FrozenMap`` serialises exactly like the dict it replaces,
  so canonical JSON, content hashes and versioned codecs (including golden v1 fixtures) are
  byte-identical.

Tuples and frozensets are used for sequences and sets. ``DomainModel.model_copy`` re-validates,
so ``update=`` can never smuggle a mutable container past this boundary.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Annotated, Any, NoReturn, TypeVar

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

K = TypeVar("K")
V = TypeVar("V")


class FrozenMap[KT, VT](Mapping[KT, VT]):
    """An immutable, hashable mapping preserving insertion order."""

    __slots__ = ("_data", "_hash")
    # Annotation-only declarations paired with __slots__ (no default value is created): mypy
    # needs these to know the attribute types, since __slots__ itself carries none.
    _data: dict[KT, VT]
    _hash: int | None

    def __init__(self, data: Mapping[KT, VT] | None = None) -> None:
        object.__setattr__(self, "_data", dict(data or {}))  # private copy: no aliasing
        object.__setattr__(self, "_hash", None)

    # -- Mapping protocol ----------------------------------------------------------------
    def __getitem__(self, key: KT) -> VT:
        return self._data[key]

    def __iter__(self) -> Iterator[KT]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"FrozenMap({self._data!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            return dict(self.items()) == dict(other.items())
        return NotImplemented

    def __hash__(self) -> int:
        cached: int | None = self._hash
        if cached is None:
            cached = hash(frozenset(self._data.items()))
            object.__setattr__(self, "_hash", cached)
        return cached

    # -- immutability --------------------------------------------------------------------
    def _immutable(self, *_: Any, **__: Any) -> NoReturn:
        raise TypeError("FrozenMap is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __setattr__ = _immutable
    __delattr__ = _immutable
    __ior__ = _immutable
    clear = pop = popitem = setdefault = update = _immutable

    def __reduce__(self) -> tuple[type[FrozenMap[KT, VT]], tuple[dict[KT, VT]]]:
        return (type(self), (dict(self._data),))

    def __copy__(self) -> FrozenMap[KT, VT]:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> FrozenMap[KT, VT]:
        return self


class _FrozenMappingSchema:
    """Pydantic hook: validate as the declared Mapping, then freeze; serialise as a dict."""

    def __get_pydantic_core_schema__(
        self, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        inner = handler(source)
        return core_schema.no_info_after_validator_function(
            _freeze_mapping,
            inner,
            serialization=core_schema.wrap_serializer_function_ser_schema(
                lambda value, nxt: nxt(dict(value)), schema=inner
            ),
        )


def _freeze_mapping(value: Mapping[Any, Any]) -> FrozenMap[Any, Any]:
    return value if isinstance(value, FrozenMap) else FrozenMap(value)


FrozenMapping = Annotated[Mapping[K, V], _FrozenMappingSchema()]


# --------------------------------------------------------------------------- canonical frozensets

T = TypeVar("T")


class _CanonicalFrozenSetSchema:
    """Serialise a ``frozenset`` field in a fixed, sorted order.

    ``frozenset`` is already immutable -- no wrapper class is needed for the mutation threat
    this module otherwise addresses. But CPython's set/frozenset iteration order can depend on
    *insertion history*, not only on the final elements: two frozensets holding the identical
    elements can iterate (and therefore JSON-serialise, since a plain list is not re-sorted by
    ``sort_keys``) in a different order depending on how they were built -- e.g. constructed
    directly versus reconstructed element-by-element from a JSON array during
    ``model_validate_json``. Left alone this silently breaks ``canonical_json()`` /
    ``content_hash()`` stability across an ordinary round trip (observed directly: see ADR
    0019). Every current element type is string-like (a ``StrEnum`` or a ``NewType(str)`` id),
    so sorting by string value gives a total, deterministic order without inventing a new
    ordering rule for the domain.
    """

    def __get_pydantic_core_schema__(
        self, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        inner = handler(source)
        # The sorted result is a list, not a frozenset, so it is serialised through a list
        # schema over the same item type -- reusing `inner` (a frozenset schema) directly would
        # only produce a Pydantic "unexpected value" warning on every dump.
        items = inner.get("items_schema", core_schema.any_schema())
        list_of_items = core_schema.list_schema(items)
        return core_schema.no_info_after_validator_function(
            lambda value: value,
            inner,
            serialization=core_schema.wrap_serializer_function_ser_schema(
                lambda value, nxt: nxt(sorted(value, key=str)), schema=list_of_items
            ),
        )


FrozenSet = Annotated[frozenset[T], _CanonicalFrozenSetSchema()]


# --------------------------------------------------------------------------- JSON-like payloads


def deep_freeze(value: Any) -> Any:
    """Recursively convert dicts -> FrozenMap and lists -> tuples (for opaque JSON payloads)."""
    if isinstance(value, Mapping):
        return FrozenMap({k: deep_freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(deep_freeze(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(deep_freeze(v) for v in value)
    return value


def deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: deep_thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [deep_thaw(v) for v in value]
    return value


class _FrozenJsonSchema:
    def __get_pydantic_core_schema__(
        self, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(
            deep_freeze,
            handler(source),
            serialization=core_schema.plain_serializer_function_ser_schema(deep_thaw),
        )


FrozenJsonObject = Annotated[Mapping[str, object], _FrozenJsonSchema()]
