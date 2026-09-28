"""foundation v0.1: identity, observations, decision ledger

Revision ID: 0001_foundation
Revises:
Create Date: 2026-09-28
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


def upgrade() -> None:
    op.create_table(
        "provider_id_mappings",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("internal_id", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(256), nullable=False),
        sa.Column("method", sa.String(32), nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("observed_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_provider_id_mappings"),
        sa.UniqueConstraint(
            "provider",
            "entity_type",
            "external_id",
            name="uq_provider_id_mappings_provider_entity_type_external_id",
        ),
        sa.UniqueConstraint(
            "internal_id",
            "provider",
            "entity_type",
            name="uq_provider_id_mappings_internal_id_provider_entity_type",
        ),
    )
    op.create_index("ix_provider_id_mappings_internal_id", "provider_id_mappings", ["internal_id"])

    op.create_table(
        "observations",
        sa.Column("observation_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("fact_key", sa.String(512), nullable=False),
        sa.Column("subject_player_id", sa.String(64), nullable=True),
        sa.Column("subject_team_id", sa.String(64), nullable=True),
        sa.Column("observed_at", TS, nullable=False),
        sa.Column("effective_at", TS, nullable=False),
        sa.Column("ingested_at", TS, nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("payload", JSON, nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("observation_id", name="pk_observations"),
    )
    op.create_index(
        "ix_observations_player_observed", "observations", ["subject_player_id", "observed_at"]
    )
    op.create_index(
        "ix_observations_team_observed", "observations", ["subject_team_id", "observed_at"]
    )
    op.create_index("ix_observations_kind_fact", "observations", ["kind", "fact_key"])

    op.create_table(
        "decisions",
        sa.Column("decision_id", sa.String(64), nullable=False),
        sa.Column("league_id", sa.String(64), nullable=False),
        sa.Column("decision_type", sa.String(32), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("information_cutoff", TS, nullable=False),
        sa.Column("payload", sa.Text, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("decision_id", name="pk_decisions"),
    )
    op.create_index("ix_decisions_league_id", "decisions", ["league_id"])

    op.create_table(
        "decision_status_events",
        sa.Column("event_id", sa.String(64), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("decision_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("occurred_at", TS, nullable=False),
        sa.Column("payload", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("event_id", name="pk_decision_status_events"),
        sa.ForeignKeyConstraint(
            ["decision_id"],
            ["decisions.decision_id"],
            name="fk_decision_status_events_decision_id_decisions",
        ),
        sa.UniqueConstraint("decision_id", "seq", name="uq_decision_status_events_decision_id_seq"),
    )
    op.create_index(
        "ix_decision_status_events_decision_id", "decision_status_events", ["decision_id"]
    )

    op.create_table(
        "decision_outcomes",
        sa.Column("outcome_id", sa.String(64), nullable=False),
        sa.Column("decision_id", sa.String(64), nullable=False),
        sa.Column("recorded_at", TS, nullable=False),
        sa.Column("payload", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("outcome_id", name="pk_decision_outcomes"),
        sa.ForeignKeyConstraint(
            ["decision_id"],
            ["decisions.decision_id"],
            name="fk_decision_outcomes_decision_id_decisions",
        ),
    )
    op.create_index("ix_decision_outcomes_decision_id", "decision_outcomes", ["decision_id"])

    op.create_table(
        "decision_grades",
        sa.Column("grade_id", sa.String(64), nullable=False),
        sa.Column("decision_id", sa.String(64), nullable=False),
        sa.Column("outcome_id", sa.String(64), nullable=False),
        sa.Column("graded_at", TS, nullable=False),
        sa.Column("payload", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("grade_id", name="pk_decision_grades"),
        sa.ForeignKeyConstraint(
            ["decision_id"],
            ["decisions.decision_id"],
            name="fk_decision_grades_decision_id_decisions",
        ),
        sa.ForeignKeyConstraint(
            ["outcome_id"],
            ["decision_outcomes.outcome_id"],
            name="fk_decision_grades_outcome_id_decision_outcomes",
        ),
    )
    op.create_index("ix_decision_grades_decision_id", "decision_grades", ["decision_id"])

    for stmt in immutability_ddl(op.get_bind().dialect.name):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in drop_immutability_ddl(op.get_bind().dialect.name):
        op.execute(stmt)
    for table in (
        "decision_grades",
        "decision_outcomes",
        "decision_status_events",
        "decisions",
        "observations",
        "provider_id_mappings",
    ):
        op.drop_table(table)
