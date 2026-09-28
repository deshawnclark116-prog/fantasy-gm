# Fantasy GM

An autonomous fantasy-football operating system: draft, lineups, waivers, drops, trades,
rest-of-season strategy, playoffs, alerts, and (where a platform safely permits it) autonomous
execution. Decisions optimise **league-specific expected championship equity** from
**raw NFL data**, with explicit uncertainty, and every decision is recorded so it can be graded
honestly later.

> Status: **Foundation v0.1.1** (auditability, temporal integrity and execution safety).
> What exists:
> * the canonical domain, a leak-safe observation store and knowledge sessions;
> * Organizational Intent v0, the versioned immutable ledger and a fail-closed autonomy gate;
> * idempotent, crash-safe execution attempts and seedable simulation interfaces.
>
> There is **no projection model, no real provider integration, no candidate generation or
> optimiser, no UI and no full simulator yet**. Those need an architecture review first.

## Quick start

```bash
make install        # uv sync --frozen --all-extras (exact locked dependency set)
make check          # ruff lint + format check, mypy --strict, pytest
make test-pg        # PostgreSQL-only tests; needs FANTASY_GM_TEST_POSTGRES_URL
make migrate        # alembic upgrade head (uses FANTASY_GM_DATABASE_URL)
```

Copy `.env.example` to `.env` to configure. Provider writes are off by default
(`FANTASY_GM_EXECUTION_ENABLED=false`, a global kill switch).

Tests exercise three backends:
* in-memory reference implementations;
* file SQLite;
* a real PostgreSQL server, when `FANTASY_GM_TEST_POSTGRES_URL` is set.

CI runs a PostgreSQL 16 service and sets `FANTASY_GM_REQUIRE_POSTGRES=1`, so PostgreSQL tests
can never be silently skipped there.

## Target architecture

```
Data Providers ─► Canonical NFL Data Layer ─► Event Detector ─► Player State Engine
   ─► Organizational Intent Engine ─► Probabilistic Projection Models ─► League Digital Twin
   ─► Candidate Generators ─► Season/Draft Simulation ─► Decision Optimizer
   ─► Risk/Confidence Gate ─► Recommend / Ask Approval / Execute
   ─► Immutable Decision Ledger ─► Outcome Grading ─► Calibration / Model Monitoring
```

Bold = exists in v0.1 (at least as a contract):

| Layer | v0.1.1 |
|---|---|
| **Data providers** | Protocols + provider-shaped records + ID-resolving ingestion mappers. No vendor adapters. |
| **Canonical NFL data layer** | Bitemporal observations, internal IDs, provider-ID registry, as-of store (memory + SQL). |
| Event detector | — (observations are append-only facts, so events can be derived later) |
| **Player state** | `PlayerState` container assembled as-of a cutoff with explicit data gaps. No projections. |
| **Organizational intent** | v0 engine: 8 component signals + confidence + provenance, prior/usage balance, conflicts. |
| Projection models | — |
| **League digital twin** | League/scoring/roster/draft/waiver/playoff configuration, scoring engine, roster legality. |
| Candidate generators / optimiser | — (candidate & action types exist) |
| **Simulation** | Seeded interfaces for weekly, matchup, remaining-season, draft-continuation; reference weekly/matchup samplers. |
| **Risk/confidence gate** | Autonomy routing + pre-execution authorisation (capabilities, confidence, staleness, FAAB ceiling, kill switch). |
| **Execution** | `ExecutionService` — the only code path that calls provider writes. |
| **Decision ledger** | Append-only, hash-verified decisions; status events, outcomes and grades as separate records; DB triggers forbid UPDATE/DELETE. |
| **Outcome grading** | Error, PIT (calibration), regret/rank vs observed alternatives. |

## Package layout (modular monolith)

```
src/fantasy_gm/
  domain/                 pure data + invariants, no IO
    time.py clock.py      UTC timestamps, KnowledgeCutoff, TimestampQuality, injected clocks
    run_context.py        LIVE / PAPER / REPLAY
    observation.py        bitemporal Observation base + SourceRef (timestamp trust, raw ts)
    knowledge.py          THE as-of semantics (resolve_known) + LeakageError
    evidence.py           EvidenceManifest (sealed by a knowledge session)
    artifacts.py          ModelArtifactManifest, RuntimeFingerprint
    validation.py risk.py validation state; action risk classes
    nfl.py league.py      NFL facts; stable League identity + temporal league settings
    roles.py              latent role dimensions (deployment vs target earning vs ...)
    decision.py           Decision, statuses, ObservedOutcome/Grade, Counterfactual* (separate)
    execution.py          execution-attempt events, idempotency keys, derived state
    identity.py           append-only, correctable provider-ID mapping events
    autonomy.py           AutonomyMode, AutonomyPolicy, FreshnessPolicy
  records/                versioned, hash-verified codecs for every durable record
  knowledge/              KnowledgeSession: the only read interface engines receive
  providers/              async protocols, provider records, identity registry, ingestion
  player_state/           stores (append-only), observed role vectors, PlayerState builder
  organizational_intent/  config (hashed heuristics), pure components, engine, inputs
  leagues/                scoring, roster validation, temporal league-state reconstruction
  draft/                  pick order
  decisions/              builder, ledger, autonomy gate, execution service
  models/                 model-artifact + validation registry
  simulation/             seeds, distributions, protocols, reference samplers, offload
  grading/                observed grading; model-based counterfactual evaluation
  persistence/            SQLAlchemy Core tables + SQL store / ledger / identity / models
  api/                    FastAPI app (health + read-only ledger)
  runtime.py              runtime/build fingerprint
alembic/                  migrations (append-only triggers incl. TRUNCATE on PostgreSQL)
docs/adr/                 architecture decision records 0001-0018
docs/REVIEW_NOTES.md      weaknesses & open questions for architecture review
tests/acceptance/         v0.1 criteria + v0.1.1 criteria (one class per review item)
tests/fixtures/records/   golden v1 payloads that must stay readable forever
```

## Core invariants

1. **Internal IDs only.** Provider IDs are correctable, append-only mapping assertions.
   Ingestion accepts VERIFIED mappings only, and there is no silent reassignment.
2. **Bitemporal, append-only observations.**
   * Each carries `effective_at`, `observed_at` (with its timestamp quality and the raw
     provider value) and `ingested_at`, plus the store-stamped `recorded_at`.
   * Under SYSTEM_KNOWLEDGE a fact is knowable only if it was physically stored by the
     cutoff.
3. **Engines read only through a `KnowledgeSession`.** Every read is cutoff-filtered and
   recorded in an evidence manifest, and the manifest is sealed into the decision. Future-
   trained model artifacts are refused.
4. **Three times, one authority.** `decision_time` and `information_cutoff` are logical times.
   `recorded_at` is stamped by the ledger clock, and LIVE records cannot be backdated.
5. **Every durable record is versioned and hash-verified.** The stored bytes are verified
   before parsing, then decoded by the reader for their schema version. History is never
   rewritten.
6. **Autonomy fails closed.** It requires all of: a LIVE run, a VALIDATED model and
   calibrator for this decision type and regime, a risk class within the policy ceiling,
   fresh information, the provider capability, and the kill switch on.
7. **Execution is at most once.**
   * A claim with a lease is taken before any call.
   * The idempotency key is deterministic.
   * An unknown outcome is reconciled before any retry, after a settle window.
   * Decision appends are serialised per decision.
8. **Observed outcomes are never mixed with model-based counterfactuals.** They use different
   types, codecs and tables.
9. **Roles are multidimensional.** Deployment is separate from target earning. There is no
   aggregate organizational-intent score.
10. **Reproducibility envelope** on every simulation, with `uv.lock` pinning dependencies.

See `docs/adr/` for rationale and `docs/REVIEW_NOTES.md` for known weaknesses.
