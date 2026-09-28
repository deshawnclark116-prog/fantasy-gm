# ADR 0007 — Deterministic, label-keyed simulation seeding

**Status:** accepted

## Decision
* `SeedSpec(root_seed, path: tuple[str, ...])` is plain, storeable data (it can sit on a
  `Decision`).
* Generators: `SeedSequence(entropy=root_seed, spawn_key=sha256(labels))` → PCG64. Python's
  salted `hash()` is never used; streams depend on labels, not call order.
* Reference samplers derive one stream per `(player, season, week)`, so adding a player to a
  lineup does not perturb other players' draws (common random numbers when comparing
  candidates).
* Every result carries `SimulationRunRecord(simulator, version, seed, n_sims, request_hash,
  numpy_version)`.
* Simulators consume `PlayerWeekDistribution`s produced upstream; they contain no projection
  logic. The v0.1 reference samplers assume independence between players (known limitation).
