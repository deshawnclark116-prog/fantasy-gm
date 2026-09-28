from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import DatabaseError

from fantasy_gm.api.app import create_app
from fantasy_gm.config import Settings
from fantasy_gm.decisions.ledger import InMemoryDecisionLedger
from fantasy_gm.domain.clock import ManualClock
from fantasy_gm.domain.decision import DecisionStatus
from fantasy_gm.domain.time import KnowledgeCutoff
from fantasy_gm.persistence.database import make_engine
from fantasy_gm.persistence.ledger import SqlDecisionLedger
from fantasy_gm.persistence.observation_store import SqlObservationStore
from fantasy_gm.persistence.tables import APPEND_ONLY_TABLES
from fantasy_gm.player_state.store import (
    DuplicateObservationError,
    ObservationQuery,
    ObservationStore,
)
from tests.factories import (
    EPOCH,
    NOW,
    lineup_decision,
    make_player,
    memory_store,
    open_session,
    status_event,
    team_id,
    ts,
    wr_usage,
)

ROOT = Path(__file__).resolve().parents[2]


def _alembic(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


@pytest.fixture(params=["sqlite", "postgres"])
def migration_url(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[str]:
    if request.param == "sqlite":
        yield f"sqlite:///{tmp_path / 'm.sqlite3'}"
    else:
        yield request.getfixturevalue("pg_schema_url")


def test_alembic_upgrade_downgrade(migration_url: str, clock: ManualClock) -> None:
    cfg = _alembic(migration_url)
    command.upgrade(cfg, "head")
    engine = make_engine(migration_url)
    assert set(APPEND_ONLY_TABLES) <= set(inspect(engine).get_table_names())
    ledger = SqlDecisionLedger(engine, clock)
    store = SqlObservationStore(engine, clock)
    d = lineup_decision(open_session(store, clock, ts(days=6)))
    ledger.record(d, status_event(d, DecisionStatus.RECORDED))
    assert ledger.get(d.decision_id).decision.value == d
    for table in ("decisions", "decision_status_events"):
        with pytest.raises(DatabaseError, match="append-only"), engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {table}"))
    engine.dispose()
    command.downgrade(cfg, "base")
    check = make_engine(migration_url)
    assert "decisions" not in set(inspect(check).get_table_names())
    check.dispose()


def test_observation_ids_are_write_once(backend_store: ObservationStore) -> None:
    u = wr_usage(make_player(), team_id(), 1, routes=20)
    backend_store.add(u)
    backend_store.add(u)  # idempotent
    with pytest.raises(DuplicateObservationError):
        backend_store.add(u.model_copy(update={"week": 2}))


@pytest.fixture
def backend_store(store: ObservationStore) -> ObservationStore:
    return store


def test_api_health_and_decision_read(clock: ManualClock) -> None:
    ledger = InMemoryDecisionLedger(clock)
    d = lineup_decision(open_session(memory_store(), clock, ts(days=6)))
    ledger.record(d, status_event(d, DecisionStatus.RECORDED))
    client = TestClient(create_app(ledger, Settings(execution_enabled=False)))
    health = client.get("/health").json()
    assert health["status"] == "ok" and health["execution_enabled"] is False
    body = client.get(f"/v1/decisions/{d.decision_id}").json()
    assert body["current_status"] == "recorded"
    assert body["decision_hash"] == d.content_hash()
    assert body["recorded_at"] == NOW.isoformat()
    assert client.get("/v1/decisions/dec_missing").status_code == 404


# --------------------------------------------------------------------------- PostgreSQL-only


@pytest.fixture
def pg_engine(pg_schema_url: str) -> Iterator[Engine]:
    from fantasy_gm.persistence.database import create_schema

    engine = make_engine(pg_schema_url)
    create_schema(engine)
    yield engine
    engine.dispose()


@pytest.mark.postgres
def test_pg_utc_roundtrip_independent_of_session_timezone(
    pg_schema_url: str, ingest_clock: ManualClock
) -> None:
    from fantasy_gm.persistence.database import create_schema

    ny = make_engine(pg_schema_url + "%20-cTimeZone%3DAmerica/New_York")
    create_schema(ny)
    with ny.connect() as conn:
        assert conn.execute(text("SHOW TimeZone")).scalar_one() == "America/New_York"
    store = SqlObservationStore(ny, ingest_clock)
    est = timezone(timedelta(hours=-5))
    u = wr_usage(make_player(), team_id(), 1, routes=20, effective=ts(days=7).astimezone(est))
    store.add(u)
    with ny.connect() as conn:
        eff, rec = conn.execute(text("SELECT effective_at, recorded_at FROM observations")).one()
    assert eff == ts(days=7) and eff.utcoffset() is not None
    # The raw driver hands back the session time zone (same instant) ...
    assert rec == EPOCH and rec.utcoffset() != timedelta(0)
    # ... while the typed column normalises to UTC regardless of the session setting.
    from fantasy_gm.persistence.tables import observations

    with ny.connect() as conn:
        typed = conn.execute(
            observations.select().with_only_columns(observations.c.recorded_at)
        ).scalar_one()
    assert typed == EPOCH and typed.utcoffset() == timedelta(0)
    got = store.known_as_of(KnowledgeCutoff(as_of=ts(days=30)), ObservationQuery())
    assert got[0].effective_at == ts(days=7)
    assert got[0].effective_at.utcoffset() == timedelta(0)
    ny.dispose()


@pytest.mark.postgres
def test_pg_jsonb_is_derived_and_text_payload_is_authoritative(
    pg_engine: Engine, clock: ManualClock
) -> None:
    store = SqlObservationStore(pg_engine, clock)
    u = wr_usage(make_player(), team_id(), 1, routes=20)
    store.add(u)
    with pg_engine.connect() as conn:
        payload, as_jsonb_text, routes = conn.execute(
            text(
                "SELECT payload, payload_json::text, (payload_json->'metrics'->>'routes')::float "
                "FROM observations"
            )
        ).one()
    assert payload == u.canonical_json()  # exact bytes preserved
    assert as_jsonb_text != payload  # JSONB normalises whitespace/key order: never hash it
    assert json.loads(as_jsonb_text) == json.loads(payload)
    assert routes == 20.0  # the JSONB copy is usable for ad-hoc queries


@pytest.mark.postgres
def test_pg_append_only_including_truncate(pg_engine: Engine, clock: ManualClock) -> None:
    store = SqlObservationStore(pg_engine, clock)
    store.add(wr_usage(make_player(), team_id(), 1, routes=20))
    for stmt in (
        "UPDATE observations SET kind = 'x'",
        "DELETE FROM observations",
        "TRUNCATE observations CASCADE",
    ):
        with pytest.raises(DatabaseError, match="append-only"), pg_engine.begin() as conn:
            conn.execute(text(stmt))
    with pg_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM observations")).scalar_one() == 1
