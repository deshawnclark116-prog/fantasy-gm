from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DatabaseError

from fantasy_gm.api.app import create_app
from fantasy_gm.config import Settings
from fantasy_gm.decisions.execution import ExecutionService
from fantasy_gm.decisions.ledger import InMemoryDecisionLedger
from fantasy_gm.domain.autonomy import AutonomyMode, AutonomyPolicy
from fantasy_gm.domain.capabilities import ProviderCapability
from fantasy_gm.domain.decision import ActorKind, DecisionStatus
from fantasy_gm.domain.identity import EntityType, ProviderRef
from fantasy_gm.persistence.ledger import SqlDecisionLedger
from fantasy_gm.player_state.store import DuplicateObservationError, ObservationStore
from tests.factories import (
    lineup_decision,
    make_league,
    make_player,
    status_event,
    team_id,
    wr_usage,
)
from tests.fakes import FakeTransactionProvider, NullIdentity

ROOT = Path(__file__).resolve().parents[2]


def test_alembic_upgrade_creates_schema_and_triggers(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'm.sqlite3'}"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    tables = set(inspect(engine).get_table_names())
    assert {
        "observations",
        "decisions",
        "decision_status_events",
        "decision_outcomes",
        "decision_grades",
        "provider_id_mappings",
    } <= tables

    ledger = SqlDecisionLedger(engine)
    d = lineup_decision()
    ledger.record(d, status_event(d, DecisionStatus.RECOMMENDED))
    with pytest.raises(DatabaseError, match="append-only"), engine.begin() as conn:
        conn.execute(text("DELETE FROM decisions"))
    command.downgrade(cfg, "base")
    assert "decisions" not in set(inspect(create_engine(url)).get_table_names())


def test_observation_ids_are_write_once(store: ObservationStore) -> None:
    p = make_player()
    u = wr_usage(p, team_id(), 1, routes=20)
    store.add(u)
    store.add(u)  # idempotent
    with pytest.raises(DuplicateObservationError):
        store.add(u.model_copy(update={"week": 2}))


def test_api_health_and_decision_read() -> None:
    ledger = InMemoryDecisionLedger()
    d = lineup_decision()
    ledger.record(d, status_event(d, DecisionStatus.RECOMMENDED))
    client = TestClient(create_app(ledger, Settings(execution_enabled=False)))
    health = client.get("/health").json()
    assert health["status"] == "ok" and health["execution_enabled"] is False
    body = client.get(f"/v1/decisions/{d.decision_id}").json()
    assert body["current_status"] == "recommended"
    assert body["decision_hash"] == d.content_hash()
    assert client.get("/v1/decisions/dec_missing").status_code == 404


def test_provider_exception_recorded_as_failed_execution() -> None:
    league = make_league()
    policy = AutonomyPolicy(league_id=league.league_id, default_mode=AutonomyMode.AUTONOMOUS)
    d = lineup_decision(league_id=league.league_id, mode=AutonomyMode.AUTONOMOUS)
    ledger = InMemoryDecisionLedger()
    ledger.record(d, status_event(d, DecisionStatus.APPROVED, actor=ActorKind.POLICY))
    caps = frozenset({ProviderCapability.READ, ProviderCapability.LINEUP_WRITE})
    provider = FakeTransactionProvider(caps, d.created_at, fail_with=RuntimeError("HTTP 503"))
    service = ExecutionService(ledger, provider, NullIdentity(), execution_enabled=True)
    ref = ProviderRef(provider="fake_platform", entity_type=EntityType.LEAGUE, external_id="L")
    attempt = service.execute(d.decision_id, policy, league, ref, now=d.created_at)
    assert not attempt.executed and not attempt.refused
    entry = ledger.get(d.decision_id)
    assert entry.current_status is DecisionStatus.EXECUTION_FAILED
    result = entry.status_history[-1].execution_result
    assert result is not None and "HTTP 503" in result.message
