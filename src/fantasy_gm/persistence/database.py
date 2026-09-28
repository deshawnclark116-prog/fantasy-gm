"""Engine construction and schema bootstrap.

SQLite (dev/tests): pysqlite's implicit transaction handling is disabled and every transaction
starts with ``BEGIN IMMEDIATE``, so writers are serialised by the database itself (the SQLite
analogue of the PostgreSQL row locks used by the ledger).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import Connection
from sqlalchemy.pool import StaticPool

from fantasy_gm.persistence.tables import immutability_ddl, metadata


def make_engine(url: str, **kwargs: Any) -> Engine:
    if not url.startswith("sqlite"):
        return create_engine(url, **kwargs)
    if ":memory:" in url or url == "sqlite://":
        kwargs.setdefault("poolclass", StaticPool)
    kwargs.setdefault("connect_args", {"check_same_thread": False, "timeout": 30})
    engine = create_engine(url, **kwargs)

    @event.listens_for(engine, "connect")
    def _sqlite_connect(dbapi_conn: Any, _record: Any) -> None:
        dbapi_conn.isolation_level = None  # let SQLAlchemy emit BEGIN itself
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _sqlite_begin(conn: Connection) -> None:
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def create_schema(engine: Engine) -> None:
    """Create tables + append-only triggers directly (tests/dev). Production uses Alembic."""
    metadata.create_all(engine)
    with engine.begin() as conn:
        for stmt in immutability_ddl(engine.dialect.name):
            conn.execute(text(stmt))
