"""Engine construction and schema bootstrap."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.pool import StaticPool

from fantasy_gm.persistence.tables import immutability_ddl, metadata


def make_engine(url: str, **kwargs: Any) -> Engine:
    if url.startswith("sqlite"):
        if ":memory:" in url or url == "sqlite://":
            kwargs.setdefault("poolclass", StaticPool)
            kwargs.setdefault("connect_args", {"check_same_thread": False})
        engine = create_engine(url, **kwargs)

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine
    return create_engine(url, **kwargs)


def create_schema(engine: Engine) -> None:
    """Create tables + append-only triggers directly (tests/dev). Production uses Alembic."""
    metadata.create_all(engine)
    with engine.begin() as conn:
        for stmt in immutability_ddl(engine.dialect.name):
            conn.execute(text(stmt))
