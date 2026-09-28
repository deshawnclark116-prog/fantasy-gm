"""foundation v0.1.1: versioned append-only records, identity history, execution attempts

Revision ID: 0001_foundation
Revises:
Create Date: 2026-09-28

The pre-release v0.1 schema (commit 0755909) was never deployed and never held data, so this
initial migration was rewritten in place for v0.1.1 rather than stacked (see ADR 0009).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from fantasy_gm.persistence.tables import drop_immutability_ddl, immutability_ddl

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)
JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _payload() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("record_type", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.Integer, nullable=False),
        sa.Column("payload", sa.Text, nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("recorded_at", TS, nullable=False),
    ]


def _decision_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["decision_id"], ["decisions.decision_id"], name=f"fk_{table}_decision_id_decisions"
    )


def upgrade() -> None:
    op.create_table(
        "observations",
        sa.Column("observation_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("fact_key", sa.String(512), nullable=False),
        sa.Column("subject_player_id", sa.String(64), nullable=True),
        sa.Column("subject_team_id", sa.String(64), nullable=True),
        sa.Column("subject_league_id", sa.String(64), nullable=True),
        sa.Column("observed_at", TS, nullable=False),
        sa.Column("effective_at", TS, nullable=False),
        sa.Column("ingested_at", TS, nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("timestamp_quality", sa.String(32), nullable=False),
        *_payload(),
        sa.Column("payload_json", JSON, nullable=False),
        sa.PrimaryKeyConstraint("observation_id", name="pk_observations"),
    )
    for name, cols in (
        ("ix_observations_player_observed", ["subject_player_id", "observed_at"]),
        ("ix_observations_team_observed", ["subject_team_id", "observed_at"]),
        ("ix_observations_league_observed", ["subject_league_id", "observed_at"]),
        ("ix_observations_kind_fact", ["kind", "fact_key"]),
    ):
        op.create_index(name, "observations", cols)

    op.create_table(
        "identity_mapping_events",
        sa.Column("event_id", sa.String(64), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(256), nullable=False),
        sa.Column("internal_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        *_payload(),
        sa.PrimaryKeyConstraint("event_id", name="pk_identity_mapping_events"),
        sa.UniqueConstraint(
            "provider",
            "entity_type",
            "external_id",
            "seq",
            name="uq_identity_mapping_events_provider_entity_type_external_id_seq",
        ),
    )
    op.create_index(
        "ix_identity_mapping_events_internal_id", "identity_mapping_events", ["internal_id"]
    )

    op.create_table(
        "decisions",
        sa.Column("decision_id", sa.String(64), nullable=False),
        sa.Column("league_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("run_mode", sa.String(16), nullable=False),
        sa.Column("decision_type", sa.String(32), nullable=False),
        sa.Column("decision_time", TS, nullable=False),
        sa.Column("information_cutoff", TS, nullable=False),
        *_payload(),
        sa.PrimaryKeyConstraint("decision_id", name="pk_decisions"),
    )
    op.create_index("ix_decisions_league_id", "decisions", ["league_id"])
    op.create_index("ix_decisions_run_id", "decisions", ["run_id"])

    op.create_table(
        "decision_status_events",
        sa.Column("event_id", sa.String(64), nullable=False),
        sa.Column("decision_id", sa.String(64), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("occurred_at", TS, nullable=False),
        *_payload(),
        sa.PrimaryKeyConstraint("event_id", name="pk_decision_status_events"),
        _decision_fk("decision_status_events"),
        sa.UniqueConstraint("decision_id", "seq", name="uq_decision_status_events_decision_id_seq"),
    )
    op.create_table(
        "execution_events",
        sa.Column("event_id", sa.String(64), nullable=False),
        sa.Column("decision_id", sa.String(64), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("attempt_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("occurred_at", TS, nullable=False),
        *_payload(),
        sa.PrimaryKeyConstraint("event_id", name="pk_execution_events"),
        _decision_fk("execution_events"),
        sa.UniqueConstraint("decision_id", "seq", name="uq_execution_events_decision_id_seq"),
    )
    op.create_table(
        "decision_outcomes",
        sa.Column("outcome_id", sa.String(64), nullable=False),
        sa.Column("decision_id", sa.String(64), nullable=False),
        *_payload(),
        sa.PrimaryKeyConstraint("outcome_id", name="pk_decision_outcomes"),
        _decision_fk("decision_outcomes"),
    )
    op.create_table(
        "decision_grades",
        sa.Column("grade_id", sa.String(64), nullable=False),
        sa.Column("decision_id", sa.String(64), nullable=False),
        sa.Column("outcome_id", sa.String(64), nullable=False),
        *_payload(),
        sa.PrimaryKeyConstraint("grade_id", name="pk_decision_grades"),
        _decision_fk("decision_grades"),
        sa.ForeignKeyConstraint(
            ["outcome_id"],
            ["decision_outcomes.outcome_id"],
            name="fk_decision_grades_outcome_id_decision_outcomes",
        ),
    )
    op.create_table(
        "decision_counterfactuals",
        sa.Column("record_id", sa.String(64), nullable=False),
        sa.Column("decision_id", sa.String(64), nullable=False),
        *_payload(),
        sa.PrimaryKeyConstraint("record_id", name="pk_decision_counterfactuals"),
        _decision_fk("decision_counterfactuals"),
    )
    for table in (
        "decision_status_events",
        "execution_events",
        "decision_outcomes",
        "decision_grades",
        "decision_counterfactuals",
    ):
        op.create_index(f"ix_{table}_decision_id", table, ["decision_id"])

    op.create_table(
        "model_artifacts",
        sa.Column("artifact_hash", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        *_payload(),
        sa.PrimaryKeyConstraint("artifact_hash", name="pk_model_artifacts"),
    )
    op.create_table(
        "model_validation_records",
        sa.Column("record_id", sa.String(64), nullable=False),
        sa.Column("artifact_hash", sa.String(64), nullable=False),
        sa.Column("decision_type", sa.String(32), nullable=False),
        sa.Column("regime", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("effective_at", TS, nullable=False),
        *_payload(),
        sa.PrimaryKeyConstraint("record_id", name="pk_model_validation_records"),
        sa.ForeignKeyConstraint(
            ["artifact_hash"],
            ["model_artifacts.artifact_hash"],
            name="fk_model_validation_records_artifact_hash_model_artifacts",
        ),
    )
    op.create_index(
        "ix_model_validation_records_artifact_hash", "model_validation_records", ["artifact_hash"]
    )

    for stmt in immutability_ddl(op.get_bind().dialect.name):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in drop_immutability_ddl(op.get_bind().dialect.name):
        op.execute(stmt)
    for table in (
        "model_validation_records",
        "model_artifacts",
        "decision_counterfactuals",
        "decision_grades",
        "decision_outcomes",
        "execution_events",
        "decision_status_events",
        "decisions",
        "identity_mapping_events",
        "observations",
    ):
        op.drop_table(table)
