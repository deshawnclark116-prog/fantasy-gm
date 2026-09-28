"""Table definitions (SQLAlchemy Core).

Domain models are pydantic and persisted as validated JSON payloads alongside indexed columns
used for querying. The ledger tables are append-only and protected by database triggers
(see ``immutability_ddl``) in addition to application checks.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

from fantasy_gm.persistence.types import UTCDateTime, json_type

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

provider_id_mappings = Table(
    "provider_id_mappings",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("internal_id", String(64), nullable=False, index=True),
    Column("provider", String(64), nullable=False),
    Column("entity_type", String(32), nullable=False),
    Column("external_id", String(256), nullable=False),
    Column("method", String(32), nullable=False),
    Column("confidence", Float, nullable=False),
    Column("observed_at", UTCDateTime(), nullable=False),
    UniqueConstraint("provider", "entity_type", "external_id"),
    UniqueConstraint("internal_id", "provider", "entity_type"),
)

observations = Table(
    "observations",
    metadata,
    Column("observation_id", String(64), primary_key=True),
    Column("kind", String(64), nullable=False),
    Column("fact_key", String(512), nullable=False),
    Column("subject_player_id", String(64), nullable=True),
    Column("subject_team_id", String(64), nullable=True),
    Column("observed_at", UTCDateTime(), nullable=False),
    Column("effective_at", UTCDateTime(), nullable=False),
    Column("ingested_at", UTCDateTime(), nullable=False),
    Column("provider", String(64), nullable=False),
    Column("payload", json_type(), nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Index("ix_observations_player_observed", "subject_player_id", "observed_at"),
    Index("ix_observations_team_observed", "subject_team_id", "observed_at"),
    Index("ix_observations_kind_fact", "kind", "fact_key"),
)

decisions = Table(
    "decisions",
    metadata,
    Column("decision_id", String(64), primary_key=True),
    Column("league_id", String(64), nullable=False, index=True),
    Column("decision_type", String(32), nullable=False),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("information_cutoff", UTCDateTime(), nullable=False),
    Column("payload", Text, nullable=False),  # canonical JSON exactly as hashed
    Column("content_hash", String(64), nullable=False),
)

decision_status_events = Table(
    "decision_status_events",
    metadata,
    Column("event_id", String(64), primary_key=True),
    Column("seq", Integer, nullable=False),
    Column(
        "decision_id", String(64), ForeignKey("decisions.decision_id"), nullable=False, index=True
    ),
    Column("status", String(32), nullable=False),
    Column("occurred_at", UTCDateTime(), nullable=False),
    Column("payload", Text, nullable=False),
    UniqueConstraint("decision_id", "seq"),
)

decision_outcomes = Table(
    "decision_outcomes",
    metadata,
    Column("outcome_id", String(64), primary_key=True),
    Column(
        "decision_id", String(64), ForeignKey("decisions.decision_id"), nullable=False, index=True
    ),
    Column("recorded_at", UTCDateTime(), nullable=False),
    Column("payload", Text, nullable=False),
)

decision_grades = Table(
    "decision_grades",
    metadata,
    Column("grade_id", String(64), primary_key=True),
    Column(
        "decision_id", String(64), ForeignKey("decisions.decision_id"), nullable=False, index=True
    ),
    Column("outcome_id", String(64), ForeignKey("decision_outcomes.outcome_id"), nullable=False),
    Column("graded_at", UTCDateTime(), nullable=False),
    Column("payload", Text, nullable=False),
)

APPEND_ONLY_TABLES = (
    "observations",
    "decisions",
    "decision_status_events",
    "decision_outcomes",
    "decision_grades",
)


def immutability_ddl(dialect: str) -> list[str]:
    """Trigger DDL that rejects UPDATE/DELETE on append-only tables."""
    statements: list[str] = []
    if dialect == "sqlite":
        for table in APPEND_ONLY_TABLES:
            for op in ("UPDATE", "DELETE"):
                statements.append(
                    f"CREATE TRIGGER IF NOT EXISTS trg_{table}_no_{op.lower()} "
                    f"BEFORE {op} ON {table} "
                    f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;"
                )
    elif dialect == "postgresql":
        statements.append(
            "CREATE OR REPLACE FUNCTION fantasy_gm_reject_mutation() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME; END; $$ LANGUAGE plpgsql;"
        )
        for table in APPEND_ONLY_TABLES:
            statements.append(
                f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION fantasy_gm_reject_mutation();"
            )
    return statements


def drop_immutability_ddl(dialect: str) -> list[str]:
    statements: list[str] = []
    if dialect == "sqlite":
        for table in APPEND_ONLY_TABLES:
            for op in ("update", "delete"):
                statements.append(f"DROP TRIGGER IF EXISTS trg_{table}_no_{op}")
    elif dialect == "postgresql":
        for table in APPEND_ONLY_TABLES:
            statements.append(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
        statements.append("DROP FUNCTION IF EXISTS fantasy_gm_reject_mutation()")
    return statements
