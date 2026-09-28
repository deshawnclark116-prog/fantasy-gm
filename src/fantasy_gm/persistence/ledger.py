"""SQL decision ledger. Same validation rules as the in-memory reference implementation.

Concurrency: every append to a decision first takes ``SELECT ... FOR UPDATE`` on the decision
row (PostgreSQL; SQLite serialises writers with ``BEGIN IMMEDIATE``), then re-reads the history,
validates the transition and inserts with the next sequence number. A unique constraint on
(decision_id, seq) remains as a backstop; if it ever fires it is surfaced as
``ConcurrentModificationError``, never as a raw integrity error.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, Row, Table, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from fantasy_gm.decisions.ledger import (
    ConcurrentModificationError,
    DecisionNotFoundError,
    ImmutableRecordError,
    LedgerEntry,
    LedgerTimePolicy,
    RecordMeta,
    meta_for,
    stamp,
    validate_counterfactual_estimate,
    validate_counterfactual_evaluation,
    validate_execution_append,
    validate_grade,
    validate_new_decision,
    validate_outcome,
    validate_transition,
)
from fantasy_gm.domain.clock import Clock
from fantasy_gm.domain.decision import (
    CounterfactualEstimate,
    CounterfactualEvaluation,
    Decision,
    DecisionStatusEvent,
    ObservedGrade,
    ObservedOutcome,
)
from fantasy_gm.domain.execution import ExecutionEvent
from fantasy_gm.domain.ids import DecisionId, LeagueId
from fantasy_gm.persistence.tables import (
    decision_counterfactuals,
    decision_grades,
    decision_outcomes,
    decision_status_events,
    decisions,
    execution_events,
)
from fantasy_gm.records.codec import EncodedRecord
from fantasy_gm.records.registry import DEFAULT_CODECS, CodecSet

_PAYLOAD_COLS = ("record_type", "schema_version", "payload", "payload_hash", "recorded_at")


def payload_values(enc: EncodedRecord, recorded_at: datetime) -> dict[str, Any]:
    return {
        "record_type": enc.record_type,
        "schema_version": enc.schema_version,
        "payload": enc.payload,
        "payload_hash": enc.payload_hash,
        "recorded_at": recorded_at,
    }


def encoded_from_row(row: Row[Any]) -> EncodedRecord:
    return EncodedRecord(
        record_type=row.record_type,
        schema_version=row.schema_version,
        payload=row.payload,
        payload_hash=row.payload_hash,
    )


class SqlDecisionLedger:
    def __init__(
        self,
        engine: Engine,
        clock: Clock,
        codecs: CodecSet = DEFAULT_CODECS,
        time_policy: LedgerTimePolicy | None = None,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._codecs = codecs
        self._policy = time_policy or LedgerTimePolicy()

    # ------------------------------------------------------------------ writes

    def record(self, decision: Decision, initial_status: DecisionStatusEvent) -> RecordMeta:
        with self._engine.begin() as conn:
            exists = conn.execute(
                select(decisions.c.decision_id).where(
                    decisions.c.decision_id == decision.decision_id
                )
            ).first()
            if exists is not None:
                raise ImmutableRecordError(f"decision {decision.decision_id} already recorded")
            now = self._clock.now()
            validate_new_decision(decision, initial_status, now, self._policy)
            enc = self._codecs.decision.encode(decision)
            try:
                conn.execute(
                    decisions.insert().values(
                        decision_id=decision.decision_id,
                        league_id=decision.league_id,
                        run_id=decision.run.run_id,
                        run_mode=decision.run.mode.value,
                        decision_type=decision.decision_type.value,
                        decision_time=decision.decision_time,
                        information_cutoff=decision.information_cutoff,
                        **payload_values(enc, now),
                    )
                )
            except IntegrityError as exc:
                raise ImmutableRecordError(str(exc.orig)) from exc
            self._insert_status(conn, initial_status, 0, now)
            return meta_for(enc, now)

    def append_status(
        self, event: DecisionStatusEvent, expected_seq: int | None = None
    ) -> RecordMeta:
        with self._engine.begin() as conn:
            entry = self._lock_and_load(conn, event.decision_id)
            seq = len(entry.status_history)
            if expected_seq is not None and expected_seq != seq:
                raise ConcurrentModificationError(f"expected status seq {expected_seq}, at {seq}")
            now = self._clock.now()
            validate_transition(entry, event, now, self._policy)
            return self._insert_status(conn, event, seq, now)

    def append_execution_event(self, event: ExecutionEvent, expected_seq: int) -> RecordMeta:
        with self._engine.begin() as conn:
            entry = self._lock_and_load(conn, event.decision_id)
            seq = len(entry.execution_events)
            if expected_seq != seq:
                raise ConcurrentModificationError(
                    f"expected execution seq {expected_seq}, at {seq}"
                )
            now = self._clock.now()
            validate_execution_append(entry, event, now, self._policy)
            enc = self._codecs.execution_event.encode(event)
            self._insert(
                conn,
                execution_events,
                event_id=event.event_id,
                decision_id=event.decision_id,
                seq=seq,
                attempt_id=event.attempt_id,
                kind=event.kind.value,
                occurred_at=event.occurred_at,
                **payload_values(enc, now),
            )
            return meta_for(enc, now, seq)

    def attach_outcome(self, outcome: ObservedOutcome) -> RecordMeta:
        with self._engine.begin() as conn:
            entry = self._lock_and_load(conn, outcome.decision_id)
            now = self._clock.now()
            validate_outcome(entry, outcome, now, self._policy)
            enc = self._codecs.observed_outcome.encode(outcome)
            self._insert(
                conn,
                decision_outcomes,
                outcome_id=outcome.outcome_id,
                decision_id=outcome.decision_id,
                **payload_values(enc, now),
            )
            return meta_for(enc, now)

    def attach_grade(self, grade: ObservedGrade) -> RecordMeta:
        with self._engine.begin() as conn:
            entry = self._lock_and_load(conn, grade.decision_id)
            validate_grade(entry, grade)
            now = self._clock.now()
            enc = self._codecs.observed_grade.encode(grade)
            self._insert(
                conn,
                decision_grades,
                grade_id=grade.grade_id,
                decision_id=grade.decision_id,
                outcome_id=grade.outcome_id,
                **payload_values(enc, now),
            )
            return meta_for(enc, now)

    def attach_counterfactual_estimate(self, estimate: CounterfactualEstimate) -> RecordMeta:
        with self._engine.begin() as conn:
            entry = self._lock_and_load(conn, estimate.decision_id)
            now = self._clock.now()
            validate_counterfactual_estimate(entry, estimate, now, self._policy)
            enc = self._codecs.counterfactual_estimate.encode(estimate)
            self._insert(
                conn,
                decision_counterfactuals,
                record_id=estimate.estimate_id,
                decision_id=estimate.decision_id,
                **payload_values(enc, now),
            )
            return meta_for(enc, now)

    def attach_counterfactual_evaluation(self, evaluation: CounterfactualEvaluation) -> RecordMeta:
        with self._engine.begin() as conn:
            entry = self._lock_and_load(conn, evaluation.decision_id)
            validate_counterfactual_evaluation(entry, evaluation)
            now = self._clock.now()
            enc = self._codecs.counterfactual_evaluation.encode(evaluation)
            self._insert(
                conn,
                decision_counterfactuals,
                record_id=evaluation.evaluation_id,
                decision_id=evaluation.decision_id,
                **payload_values(enc, now),
            )
            return meta_for(enc, now)

    # ------------------------------------------------------------------ reads

    def get(self, decision_id: DecisionId) -> LedgerEntry:
        with self._engine.connect() as conn:
            return self._load(conn, decision_id)

    def list_ids(self, league_id: LeagueId) -> list[DecisionId]:
        with self._engine.connect() as conn:
            rows: list[str] = list(
                conn.execute(
                    select(decisions.c.decision_id)
                    .where(decisions.c.league_id == league_id)
                    .order_by(decisions.c.recorded_at)
                ).scalars()
            )
            return [DecisionId(r) for r in rows]

    # ------------------------------------------------------------------ internals

    def _insert_status(
        self, conn: Connection, event: DecisionStatusEvent, seq: int, now: datetime
    ) -> RecordMeta:
        enc = self._codecs.status_event.encode(event)
        self._insert(
            conn,
            decision_status_events,
            event_id=event.event_id,
            decision_id=event.decision_id,
            seq=seq,
            status=event.status.value,
            occurred_at=event.occurred_at,
            **payload_values(enc, now),
        )
        return meta_for(enc, now, seq)

    @staticmethod
    def _insert(conn: Connection, table: Table, **values: Any) -> None:
        try:
            conn.execute(table.insert().values(**values))
        except IntegrityError as exc:
            raise ConcurrentModificationError(
                f"concurrent append to {table.name}: {exc.orig}"
            ) from exc

    def _lock_and_load(self, conn: Connection, decision_id: DecisionId) -> LedgerEntry:
        locked = conn.execute(
            select(decisions.c.decision_id)
            .where(decisions.c.decision_id == decision_id)
            .with_for_update()
        ).first()
        if locked is None:
            raise DecisionNotFoundError(decision_id)
        return self._load(conn, decision_id)

    @staticmethod
    def _rows(
        conn: Connection, table: Table, decision_id: DecisionId, order: str
    ) -> Sequence[Row[Any]]:
        cols = [table.c[c] for c in _PAYLOAD_COLS]
        return conn.execute(
            select(*cols).where(table.c.decision_id == decision_id).order_by(table.c[order])
        ).all()

    def _load(self, conn: Connection, decision_id: DecisionId) -> LedgerEntry:
        c = self._codecs
        row = conn.execute(
            select(*[decisions.c[x] for x in _PAYLOAD_COLS]).where(
                decisions.c.decision_id == decision_id
            )
        ).first()
        if row is None:
            raise DecisionNotFoundError(decision_id)
        statuses = self._rows(conn, decision_status_events, decision_id, "seq")
        execs = self._rows(conn, execution_events, decision_id, "seq")
        outcomes = self._rows(conn, decision_outcomes, decision_id, "recorded_at")
        grades = self._rows(conn, decision_grades, decision_id, "recorded_at")
        cfs = self._rows(conn, decision_counterfactuals, decision_id, "recorded_at")
        cf_est = [r for r in cfs if r.record_type == c.counterfactual_estimate.record_type]
        cf_eval = [r for r in cfs if r.record_type == c.counterfactual_evaluation.record_type]
        return LedgerEntry(
            decision=stamp(c.decision, encoded_from_row(row), row.recorded_at),
            status_history=tuple(
                stamp(c.status_event, encoded_from_row(r), r.recorded_at, i)
                for i, r in enumerate(statuses)
            ),
            execution_events=tuple(
                stamp(c.execution_event, encoded_from_row(r), r.recorded_at, i)
                for i, r in enumerate(execs)
            ),
            outcomes=tuple(
                stamp(c.observed_outcome, encoded_from_row(r), r.recorded_at) for r in outcomes
            ),
            grades=tuple(
                stamp(c.observed_grade, encoded_from_row(r), r.recorded_at) for r in grades
            ),
            counterfactual_estimates=tuple(
                stamp(c.counterfactual_estimate, encoded_from_row(r), r.recorded_at) for r in cf_est
            ),
            counterfactual_evaluations=tuple(
                stamp(c.counterfactual_evaluation, encoded_from_row(r), r.recorded_at)
                for r in cf_eval
            ),
        )
