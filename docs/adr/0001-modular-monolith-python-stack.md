# ADR 0001 — Modular monolith on Python 3.12 / FastAPI / Pydantic v2 / SQLAlchemy 2 / Alembic

**Status:** accepted (Foundation v0.1)

## Context
The repository was empty. The product needs clean domain boundaries (data, player state,
organizational intent, leagues, draft, decisions, simulation, grading) but has one team and no
scale pressure that justifies network boundaries.

## Decision
* One deployable Python package (`src/fantasy_gm`) with one sub-package per bounded context.
* `domain` holds pure pydantic models and invariants and imports nothing from other packages.
* Engines are pure functions over domain objects. IO lives in `persistence`, `api`, and (later)
  provider adapters.
* Persistence uses **SQLAlchemy Core** (not the ORM): domain objects are immutable pydantic
  models persisted as validated JSON payloads alongside indexed query columns. This avoids a
  second, mutable object model and an identity map that could hide in-place mutation.
* PostgreSQL in production (JSONB via type variant); SQLite for tests and local dev.
* Simulation uses numpy (PCG64 via `SeedSequence`).

## Consequences
* A context can later be extracted into a service by replacing its protocol implementation.
* JSON payloads make schema evolution a payload-versioning concern (see REVIEW_NOTES).
