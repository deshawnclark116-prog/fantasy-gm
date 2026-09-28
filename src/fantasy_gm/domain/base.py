"""Shared base model for all domain types."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict


class DomainModel(BaseModel):
    """Immutable, strict-by-default base for domain records.

    ``frozen`` prevents attribute *reassignment*, but a plain ``dict``/``list`` field would
    still be mutable in place (``obj.field[k] = v``). Every dict-typed domain field is
    therefore declared with ``fantasy_gm.domain.frozen.FrozenMapping`` (or, for opaque JSON
    payloads, ``FrozenJsonObject``), which validates as a normal mapping but stores a deeply
    immutable ``FrozenMap`` -- see ADR 0019. Sequence fields use ``tuple``, which is already
    immutable.

    Durable records (e.g. the decision ledger) additionally store a serialized copy plus a
    content hash rather than trusting in-memory object identity, so tamper-evidence never
    depends on any one process's object graph staying unmutated.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    def canonical_json(self) -> str:
        """Deterministic JSON (sorted keys, no whitespace) used for hashing."""
        return canonical_json(self.model_dump(mode="json"))

    def content_hash(self) -> str:
        return sha256_hex(self.canonical_json())


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
