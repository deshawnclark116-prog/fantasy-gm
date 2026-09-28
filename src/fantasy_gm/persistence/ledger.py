"""SQL decision ledger. Same validation rules as the in-memory reference implementation."""

from __future__ import annotations

from sqlalchemy import Engine, select
from sqlalchemy.engine import Connection

from fantasy_gm.decisions.ledger import (
    DecisionNotFoundError,
    ImmutableRecordError,
    LedgerEntry,
    load_decision,
    validate_grade,
    validate_initial,
    validate_outcome,
    validate_transition,
)
from fantasy_gm.domain.decision import (
    Decision,
    DecisionGrade,
    DecisionOutcome,
    DecisionStatusEvent,
)
from fantasy_gm.domain.ids import DecisionId, LeagueId
from fantasy_gm.persistence.tables import (
    decision_grades,
    decision_outcomes,
    decision_status_events,
    decisions,
)


class SqlDecisionLedger:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(self, decision: Decision, initial_status: DecisionStatusEvent) -> str:
        validate_initial(decision, initial_status)
        digest = decision.content_hash()
        with self._engine.begin() as conn:
            exists = conn.execute(
                select(decisions.c.decision_id).where(
                    decisions.c.decision_id == decision.decision_id
                )
            ).first()
            if exists is not None:
                raise ImmutableRecordError(f"decision {decision.decision_id} already recorded")
            conn.execute(
                decisions.insert().values(
                    decision_id=decision.decision_id,
                    league_id=decision.league_id,
                    decision_type=decision.decision_type.value,
                    created_at=decision.created_at,
                    information_cutoff=decision.information_cutoff,
                    payload=decision.canonical_json(),
                    content_hash=digest,
                )
            )
            self._insert_event(conn, initial_status, seq=0)
        return digest

    def append_status(self, event: DecisionStatusEvent) -> None:
        with self._engine.begin() as conn:
            entry = self._load(conn, event.decision_id)
            validate_transition(entry, event)
            self._insert_event(conn, event, seq=len(entry.status_history))

    def attach_outcome(self, outcome: DecisionOutcome) -> None:
        with self._engine.begin() as conn:
            entry = self._load(conn, outcome.decision_id)
            validate_outcome(entry, outcome)
            conn.execute(
                decision_outcomes.insert().values(
                    outcome_id=outcome.outcome_id,
                    decision_id=outcome.decision_id,
                    recorded_at=outcome.recorded_at,
                    payload=outcome.canonical_json(),
                )
            )

    def attach_grade(self, grade: DecisionGrade) -> None:
        with self._engine.begin() as conn:
            entry = self._load(conn, grade.decision_id)
            validate_grade(entry, grade)
            conn.execute(
                decision_grades.insert().values(
                    grade_id=grade.grade_id,
                    decision_id=grade.decision_id,
                    outcome_id=grade.outcome_id,
                    graded_at=grade.graded_at,
                    payload=grade.canonical_json(),
                )
            )

    def get(self, decision_id: DecisionId) -> LedgerEntry:
        with self._engine.connect() as conn:
            return self._load(conn, decision_id)

    def list_ids(self, league_id: LeagueId) -> list[DecisionId]:
        with self._engine.connect() as conn:
            rows: list[str] = list(
                conn.execute(
                    select(decisions.c.decision_id)
                    .where(decisions.c.league_id == league_id)
                    .order_by(decisions.c.created_at)
                ).scalars()
            )
            return [DecisionId(r) for r in rows]

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _insert_event(conn: Connection, event: DecisionStatusEvent, seq: int) -> None:
        conn.execute(
            decision_status_events.insert().values(
                event_id=event.event_id,
                seq=seq,
                decision_id=event.decision_id,
                status=event.status.value,
                occurred_at=event.occurred_at,
                payload=event.canonical_json(),
            )
        )

    @staticmethod
    def _load(conn: Connection, decision_id: DecisionId) -> LedgerEntry:
        row = conn.execute(
            select(decisions.c.payload, decisions.c.content_hash).where(
                decisions.c.decision_id == decision_id
            )
        ).first()
        if row is None:
            raise DecisionNotFoundError(decision_id)
        events: list[str] = list(
            conn.execute(
                select(decision_status_events.c.payload)
                .where(decision_status_events.c.decision_id == decision_id)
                .order_by(decision_status_events.c.seq)
            ).scalars()
        )
        outcomes: list[str] = list(
            conn.execute(
                select(decision_outcomes.c.payload)
                .where(decision_outcomes.c.decision_id == decision_id)
                .order_by(decision_outcomes.c.recorded_at)
            ).scalars()
        )
        grades: list[str] = list(
            conn.execute(
                select(decision_grades.c.payload)
                .where(decision_grades.c.decision_id == decision_id)
                .order_by(decision_grades.c.graded_at)
            ).scalars()
        )
        return LedgerEntry(
            decision=load_decision(row.payload, row.content_hash),
            decision_hash=row.content_hash,
            status_history=tuple(DecisionStatusEvent.model_validate_json(e) for e in events),
            outcomes=tuple(DecisionOutcome.model_validate_json(o) for o in outcomes),
            grades=tuple(DecisionGrade.model_validate_json(g) for g in grades),
        )
