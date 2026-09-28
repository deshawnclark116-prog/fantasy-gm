# ADR 0015 — Reproducibility envelope and dependency lock

**Status:** accepted (v0.1.1). Amends ADR 0007.

## Decision
* Every simulation result carries a `ReproducibilityEnvelope` with:
  * a `RuntimeFingerprint`: Python, numpy, platform, package version, code commit and
    `uv.lock` hash;
  * the simulator and its version, and the config hash;
  * the seed, the number of simulations, the request hash and the model-artifact hashes.
* Dependencies are pinned in `uv.lock`, and CI installs with `uv sync --frozen`.
  Bit-for-bit reproduction is claimed **only** when the runtime fingerprint matches. It is not
  claimed across arbitrary future library versions.
* CPU-heavy simulation runs off the event loop through `simulation.offload.run_offloop`
  (process pool). Results are identical across the process boundary because all randomness
  derives from the request's `SeedSpec`.
