# Fantasy GM

An autonomous fantasy-football operating system: draft, lineups, waivers, drops, trades,
rest-of-season strategy, playoffs, alerts, and (where a platform safely permits it) autonomous
execution. Decisions optimise **league-specific expected championship equity** from
**raw NFL data**, with explicit uncertainty, and every decision is recorded so it can be graded
honestly later.

> Status: **Foundation v0.1**. The backend skeleton, canonical domain, leak-safe
> observation store, Organizational Intent v0, the decision ledger, the autonomy gate and
> seedable simulation interfaces exist. There is **no projection model, no real provider
> integration, no UI and no full simulator yet**. Those need an architecture review first.

## Quick start

```bash
make install        # uv venv (Python 3.12) + editable install with dev extras
make check          # ruff lint + format check, mypy --strict, pytest
make migrate        # alembic upgrade head (uses FANTASY_GM_DATABASE_URL)
```

Copy `.env.example` to `.env` to configure. Provider writes are off by default
(`FANTASY_GM_EXECUTION_ENABLED=false`, a global kill switch).

## Target architecture

```
Data Providers ─► Canonical NFL Data Layer ─► Event Detector ─► Player State Engine
   ─► Organizational Intent Engine ─► Probabilistic Projection Models ─► League Digital Twin
   ─► Candidate Generators ─► Season/Draft Simulation ─► Decision Optimizer
   ─► Risk/Confidence Gate ─► Recommend / Ask Approval / Execute
   ─► Immutable Decision Ledger ─► Outcome Grading ─► Calibration / Model Monitoring
```

Bold = exists in v0.1 (at least as a contract):

| Layer | v0.1 |
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
    time.py               UTC-only timestamps, KnowledgeCutoff / KnowledgeMode
    ids.py identity.py    internal IDs; ProviderRef / ProviderIdMapping
    observation.py        bitemporal Observation base (observed_at, effective_at, ingested_at)
    knowledge.py          THE as-of semantics (resolve_known) + LeakageError
    nfl.py                Player, NFLTeam, NFLGame, PlayerTeamAssignment, DraftCapital,
                          ContractSignal, DepthChartSignal, UsageSnapshot, InjuryStatus,
                          RosterTransactionSignal, CoachingAssignment
    league.py             ScoringRules, RosterRules, RosterSlot, League, FantasyTeam,
                          FantasyRoster, DraftPick, draft/waiver/playoff settings
    actions.py            typed actions, each declaring its required provider capability
    decision.py           Decision, DecisionCandidate, DecisionEvidence, OutcomeDistribution,
                          DecisionStatusEvent, ExecutionResult, DecisionOutcome, DecisionGrade
    autonomy.py           AutonomyMode, DecisionType, AutonomyPolicy
    capabilities.py       ProviderCapability
    state.py market.py seeds.py
  providers/              protocols, provider records, identity registry, ingestion mappers
  player_state/           ObservationStore protocol + in-memory store, PlayerState builder
  organizational_intent/  config (all heuristics, hashed), components (pure), engine, inputs
  leagues/                scoring engine, roster validation
  draft/                  pick-order generation, picks-until-next-turn
  decisions/              ledger, autonomy gate, execution service
  simulation/             seed derivation, distributions, protocols, reference samplers
  grading/                grader
  persistence/            SQLAlchemy Core tables, SQL store / ledger / identity registry
  api/                    FastAPI app (health + read-only ledger)
alembic/                  migrations (0001_foundation incl. append-only triggers)
docs/adr/                 architecture decision records
docs/REVIEW_NOTES.md      weaknesses & open questions for architecture review
tests/acceptance/         the ten v0.1 acceptance criteria, one class each
```

Dependency direction: `domain` depends on nothing; engines (`organizational_intent`,
`leagues`, `draft`, `simulation`, `grading`, `decisions`) depend on `domain` and protocols;
`persistence` and `api` are adapters at the edge.

## Core invariants

1. **Internal IDs only.** Provider IDs live in `ProviderIdMapping`; one ref → one internal ID;
   unknown refs fail ingestion instead of silently creating players.
2. **Bitemporal, append-only observations.** Every time-varying fact carries `effective_at`,
   `observed_at` and `source.ingested_at`. Corrections are new observations with the same
   `fact_key`. Queries *require* a `KnowledgeCutoff`; there is no "read everything" API.
3. **Two knowledge modes.** `SYSTEM_KNOWLEDGE` (default; observed *and* ingested by the cutoff)
   for live decisions, `PUBLIC_AVAILABILITY` for backtests over backfilled data.
4. **Decisions are immutable.** Recorded once as canonical JSON + SHA-256; re-verified on every
   read; evidence observed after the cutoff is rejected at construction; outcomes must post-date
   the cutoff; grades carry the decision hash they were computed against.
5. **Execution is separated from intelligence** and gated twice (routing at record time,
   authorisation immediately before the provider call). A missing capability produces an
   actionable manual notification, never an attempted write.
6. **Reproducible randomness.** `SeedSpec(root_seed, path)` derives label-keyed streams via
   SHA-256 → numpy `SeedSequence`; every run records seed, request hash and numpy version.
7. **No invented numbers.** Organizational Intent v0 heuristics are all in one hashed config,
   labelled unvalidated; absent evidence yields `score=None`, never zero.

See `docs/adr/` for rationale and `docs/REVIEW_NOTES.md` for known weaknesses.
