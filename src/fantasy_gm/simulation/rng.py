"""Seed derivation.

Child streams are keyed by stable label hashes (SHA-256), never by Python's salted ``hash`` and
never by call order, so results are reproducible across processes and adding a new consumer of
randomness does not perturb existing streams.

Bit-exact reproducibility also depends on the numpy version (BitGenerator/distribution
algorithms); the runtime fingerprint is recorded on every run.
"""

from __future__ import annotations

import hashlib

import numpy as np

from fantasy_gm.domain.seeds import SeedSpec


def _label_to_int(label: str) -> int:
    return int.from_bytes(hashlib.sha256(label.encode("utf-8")).digest()[:8], "big")


def generator_for(seed: SeedSpec) -> np.random.Generator:
    sequence = np.random.SeedSequence(
        entropy=seed.root_seed, spawn_key=tuple(_label_to_int(p) for p in seed.path)
    )
    return np.random.Generator(np.random.PCG64(sequence))
