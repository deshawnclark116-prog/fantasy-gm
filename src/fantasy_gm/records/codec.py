"""Versioned codecs for immutable durable records.

Write path: ``encode`` serialises the *current* model to canonical JSON and hashes those exact
bytes. Read path: ``decode`` (1) verifies the stored bytes against the stored hash *before*
parsing, then (2) dispatches on the stored ``schema_version`` to a reader that adapts that
version to the current model. Historical payloads are never rewritten to upgrade them.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel

from fantasy_gm.domain.base import DomainModel, sha256_hex


class TamperDetectedError(RuntimeError):
    pass


class UnknownSchemaVersionError(LookupError):
    pass


class EncodedRecord(DomainModel):
    record_type: str
    schema_version: int
    payload: str
    payload_hash: str


Reader = Callable[[dict[str, Any]], Any]


def verify_payload(payload: str, expected_hash: str, what: str) -> None:
    actual = sha256_hex(payload)
    if actual != expected_hash:
        raise TamperDetectedError(f"{what}: payload hash mismatch ({actual} != {expected_hash})")


class VersionedCodec[M: BaseModel]:
    def __init__(
        self,
        record_type: str,
        model: type[M],
        current_version: int = 1,
        legacy_readers: Mapping[int, Callable[[dict[str, Any]], M]] | None = None,
    ) -> None:
        self.record_type = record_type
        self.model = model
        self.current_version = current_version
        self._readers: dict[int, Callable[[dict[str, Any]], M]] = dict(legacy_readers or {})
        self._readers[current_version] = model.model_validate

    @property
    def readable_versions(self) -> frozenset[int]:
        return frozenset(self._readers)

    def encode(self, value: M) -> EncodedRecord:
        if not isinstance(value, self.model):
            raise TypeError(f"{self.record_type} codec cannot encode {type(value).__name__}")
        payload = (
            value.canonical_json()
            if isinstance(value, DomainModel)
            else json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        )
        return EncodedRecord(
            record_type=self.record_type,
            schema_version=self.current_version,
            payload=payload,
            payload_hash=sha256_hex(payload),
        )

    def decode(self, schema_version: int, payload: str, expected_hash: str) -> M:
        verify_payload(payload, expected_hash, self.record_type)
        reader = self._readers.get(schema_version)
        if reader is None:
            raise UnknownSchemaVersionError(
                f"{self.record_type} schema_version {schema_version} has no reader "
                f"(readable: {sorted(self._readers)})"
            )
        return reader(json.loads(payload))

    def decode_record(self, record: EncodedRecord) -> M:
        if record.record_type != self.record_type:
            raise ValueError(f"expected {self.record_type}, got {record.record_type}")
        return self.decode(record.schema_version, record.payload, record.payload_hash)
