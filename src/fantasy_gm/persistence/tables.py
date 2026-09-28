"""Table definitions (SQLAlchemy Core).

Every durable record is stored as ``record_type`` + ``schema_version`` + ``payload`` (canonical
JSON *text*, the authoritative bytes) + ``payload_hash`` (SHA-256 of those bytes) + a
ledger/registry-stamped ``recorded_at``. Other columns are query indexes derived from the payload.

``observations.payload_json`` is a derived, NON-authoritative JSON/JSONB copy for ad-hoc
queries: PostgreSQL JSONB normalises key order and whitespace, so it can never be used for
integrity checks. All append-only tables are protected by UPDATE/DELETE triggers.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    Column,
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


def _payload_columns() -> list[Column[Any]]:
    return [
        Column("record_type", String(64), nullable=False),
        Column("schema_version", Integer, nullable=False),
        Column("payload", Text, nullable=False),
        Column("payload_hash", String(64), nullable=False),
        Column("recorded_at", UTCDateTime(), nullable=False),
    ]


def _decision_fk() -> Column[Any]:
    return Column(
        "decision_id", String(64), ForeignKey("decisions.decision_id"), nullable=False, index=True
    )


observations = Table(
    "observations",
    metadata,
    Column("observation_id", String(64), primary_key=True),
    Column("kind", String(64), nullable=False),
    Column("fact_key", String(512), nullable=False),
    Column("subject_player_id", String(64), nullable=True),
    Column("subject_team_id", String(64), nullable=True),
    Column("subject_league_id", String(64), nullable=True),
    Column("observed_at", UTCDateTime(), nullable=False),
    Column("effective_at", UTCDateTime(), nullable=False),
    Column("ingested_at", UTCDateTime(), nullable=False),
    Column("provider", String(64), nullable=False),
    Column("timestamp_quality", String(32), nullable=False),
    *_payload_columns(),
    Column("payload_json", json_type(), nullable=False),
    Index("ix_observations_player_observed", "subject_player_id", "observed_at"),
    Index("ix_observations_team_observed", "subject_team_id", "observed_at"),
    Index("ix_observations_league_observed", "subject_league_id", "observed_at"),
    Index("ix_observations_kind_fact", "kind", "fact_key"),
)

identity_mapping_events = Table(
    "identity_mapping_events",
    metadata,
    Column("event_id", String(64), primary_key=True),
    Column("seq", Integer, nullable=False),
    Column("provider", String(64), nullable=False),
    Column("entity_type", String(32), nullable=False),
    Column("external_id", String(256), nullable=False),
    Column("internal_id", String(64), nullable=True, index=True),
    Column("status", String(16), nullable=False),
    *_payload_columns(),
    UniqueConstraint("provider", "entity_type", "external_id", "seq"),
)

decisions = Table(
    "decisions",
    metadata,
    Column("decision_id", String(64), primary_key=True),
    Column("league_id", String(64), nullable=False, index=True),
    Column("run_id", String(64), nullable=False, index=True),
    Column("run_mode", String(16), nullable=False),
    Column("decision_type", String(32), nullable=False),
    Column("decision_time", UTCDateTime(), nullable=False),
    Column("information_cutoff", UTCDateTime(), nullable=False),
    *_payload_columns(),
)

decision_status_events = Table(
    "decision_status_events",
    metadata,
    Column("event_id", String(64), primary_key=True),
    _decision_fk(),
    Column("seq", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("occurred_at", UTCDateTime(), nullable=False),
    *_payload_columns(),
    UniqueConstraint("decision_id", "seq"),
)

execution_events = Table(
    "execution_events",
    metadata,
    Column("event_id", String(64), primary_key=True),
    _decision_fk(),
    Column("seq", Integer, nullable=False),
    Column("attempt_id", String(64), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("occurred_at", UTCDateTime(), nullable=False),
    *_payload_columns(),
    UniqueConstraint("decision_id", "seq"),
)

decision_outcomes = Table(
    "decision_outcomes",
    metadata,
    Column("outcome_id", String(64), primary_key=True),
    _decision_fk(),
    *_payload_columns(),
)

decision_grades = Table(
    "decision_grades",
    metadata,
    Column("grade_id", String(64), primary_key=True),
    _decision_fk(),
    Column("outcome_id", String(64), ForeignKey("decision_outcomes.outcome_id"), nullable=False),
    *_payload_columns(),
)

# Model-based counterfactual estimates and evaluations live in their own table so they can never
# be confused with observed outcomes/grades.
decision_counterfactuals = Table(
    "decision_counterfactuals",
    metadata,
    Column("record_id", String(64), primary_key=True),
    _decision_fk(),
    *_payload_columns(),
)

model_artifacts = Table(
    "model_artifacts",
    metadata,
    Column("artifact_hash", String(64), primary_key=True),
    Column("name", String(128), nullable=False),
    Column("version", String(64), nullable=False),
    *_payload_columns(),
)

model_validation_records = Table(
    "model_validation_records",
    metadata,
    Column("record_id", String(64), primary_key=True),
    Column(
        "artifact_hash",
        String(64),
        ForeignKey("model_artifacts.artifact_hash"),
        nullable=False,
        index=True,
    ),
    Column("decision_type", String(32), nullable=False),
    Column("regime", String(64), nullable=False),
    Column("state", String(16), nullable=False),
    Column("effective_at", UTCDateTime(), nullable=False),
    *_payload_columns(),
)

APPEND_ONLY_TABLES = (
    "observations",
    "identity_mapping_events",
    "decisions",
    "decision_status_events",
    "execution_events",
    "decision_outcomes",
    "decision_grades",
    "decision_counterfactuals",
    "model_artifacts",
    "model_validation_records",
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
            statements.append(
                f"CREATE TRIGGER trg_{table}_no_truncate BEFORE TRUNCATE ON {table} "
                "FOR EACH STATEMENT EXECUTE FUNCTION fantasy_gm_reject_mutation();"
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
            statements.append(f"DROP TRIGGER IF EXISTS trg_{table}_no_truncate ON {table}")
        statements.append("DROP FUNCTION IF EXISTS fantasy_gm_reject_mutation()")
    return statements
