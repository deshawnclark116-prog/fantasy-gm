from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine

from fantasy_gm.decisions.ledger import DecisionLedger, InMemoryDecisionLedger
from fantasy_gm.persistence.database import create_schema, make_engine
from fantasy_gm.persistence.ledger import SqlDecisionLedger
from fantasy_gm.persistence.observation_store import SqlObservationStore
from fantasy_gm.player_state.store import InMemoryObservationStore, ObservationStore


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = make_engine("sqlite:///:memory:")
    create_schema(eng)
    yield eng
    eng.dispose()


@pytest.fixture(params=["memory", "sql"])
def ledger(request: pytest.FixtureRequest, engine: Engine) -> DecisionLedger:
    if request.param == "memory":
        return InMemoryDecisionLedger()
    return SqlDecisionLedger(engine)


@pytest.fixture(params=["memory", "sql"])
def store(request: pytest.FixtureRequest, engine: Engine) -> ObservationStore:
    if request.param == "memory":
        return InMemoryObservationStore()
    return SqlObservationStore(engine)
