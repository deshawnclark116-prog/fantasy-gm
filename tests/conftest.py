"""Backend-parametrised fixtures.

Every storage-backed test runs against three backends:
* ``memory``   -- reference in-memory implementations;
* ``sqlite``   -- file-based SQLite (real multi-connection locking via BEGIN IMMEDIATE);
* ``postgres`` -- a real PostgreSQL server, one throwaway schema per test.

PostgreSQL tests are skipped when ``FANTASY_GM_TEST_POSTGRES_URL`` is unset, unless
``FANTASY_GM_REQUIRE_POSTGRES=1`` (as in CI), in which case a missing server is a failure.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text

from fantasy_gm.decisions.ledger import DecisionLedger, InMemoryDecisionLedger
from fantasy_gm.domain.clock import ManualClock
from fantasy_gm.models.registry import InMemoryModelRegistry, ModelRegistry
from fantasy_gm.persistence.database import create_schema, make_engine
from fantasy_gm.persistence.identity import SqlIdentityRegistry
from fantasy_gm.persistence.ledger import SqlDecisionLedger
from fantasy_gm.persistence.models import SqlModelRegistry
from fantasy_gm.persistence.observation_store import SqlObservationStore
from fantasy_gm.player_state.store import InMemoryObservationStore, ObservationStore
from fantasy_gm.providers.identity import IdentityRegistry, InMemoryIdentityRegistry
from tests.factories import EPOCH, NOW

PG_URL = os.environ.get("FANTASY_GM_TEST_POSTGRES_URL")
REQUIRE_PG = os.environ.get("FANTASY_GM_REQUIRE_POSTGRES") == "1"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        callspec = getattr(item, "callspec", None)
        if callspec is not None and "postgres" in callspec.params.values():
            item.add_marker(pytest.mark.postgres)


def _require_pg() -> str:
    if PG_URL:
        return PG_URL
    if REQUIRE_PG:
        pytest.fail("FANTASY_GM_REQUIRE_POSTGRES=1 but FANTASY_GM_TEST_POSTGRES_URL is unset")
    pytest.skip("PostgreSQL not configured (set FANTASY_GM_TEST_POSTGRES_URL)")


@pytest.fixture
def pg_schema_url() -> Iterator[str]:
    """A URL whose search_path is a fresh, throwaway schema."""
    url = _require_pg()
    schema = f"t_{uuid.uuid4().hex[:12]}"
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    sep = "&" if "?" in url else "?"
    try:
        yield f"{url}{sep}options=-csearch_path%3D{schema}"
    finally:
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _engine_for(kind: str, tmp_path: Path, request: pytest.FixtureRequest) -> Engine:
    if kind == "sqlite":
        return make_engine(f"sqlite:///{tmp_path / 'fgm.sqlite3'}")
    url: str = request.getfixturevalue("pg_schema_url")
    return make_engine(url, pool_size=10)


@dataclass
class Backend:
    kind: str
    engine: Engine | None


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOW)


@pytest.fixture
def ingest_clock() -> ManualClock:
    """Physical clock of the observation store (see tests.factories.EPOCH)."""
    return ManualClock(EPOCH)


@pytest.fixture(params=["memory", "sqlite", "postgres"])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Backend]:
    if request.param == "memory":
        yield Backend("memory", None)
        return
    engine = _engine_for(request.param, tmp_path, request)
    create_schema(engine)
    yield Backend(request.param, engine)
    engine.dispose()


@pytest.fixture(params=["sqlite", "postgres"])
def sql_engine(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Engine]:
    engine = _engine_for(request.param, tmp_path, request)
    create_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def ledger(backend: Backend, clock: ManualClock) -> DecisionLedger:
    if backend.engine is None:
        return InMemoryDecisionLedger(clock)
    return SqlDecisionLedger(backend.engine, clock)


@pytest.fixture
def store(backend: Backend, ingest_clock: ManualClock) -> ObservationStore:
    if backend.engine is None:
        return InMemoryObservationStore(ingest_clock)
    return SqlObservationStore(backend.engine, ingest_clock)


@pytest.fixture
def identity(backend: Backend, clock: ManualClock) -> IdentityRegistry:
    if backend.engine is None:
        return InMemoryIdentityRegistry(clock)
    return SqlIdentityRegistry(backend.engine, clock)


@pytest.fixture
def models(backend: Backend, clock: ManualClock) -> ModelRegistry:
    if backend.engine is None:
        return InMemoryModelRegistry(clock)
    return SqlModelRegistry(backend.engine, clock)
