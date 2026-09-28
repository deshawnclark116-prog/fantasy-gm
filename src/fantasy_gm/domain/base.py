"""Shared base model for all domain types."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict


class DomainModel(BaseModel):
    """Immutable, strict-by-default base for domain records.

    ``frozen`` prevents attribute reassignment. Container fields (dicts/lists) inside a
    frozen pydantic model are still mutable Python objects, so anything that must be
    tamper-evident (e.g. the decision ledger) stores a serialized copy plus a content hash
    rather than trusting in-memory object identity.
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
