"""Foundation v0.1.1 acceptance: auditability, temporal integrity and execution safety.

Each class maps to the numbered item in the v0.1.1 architecture review.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import threading
from concurrent.futures import ProcessPoolExecutor
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, text
from sqlalchemy.exc import DatabaseError

from fantasy_gm.decisions.autonomy import Routing, authorize_execution, route_decision
from fantasy_gm.decisions.execution import AttemptOutcome, ExecutionService
from fantasy_gm.decisions.ledger import (
    ConcurrentModificationError,
    DecisionLedger,
    InMemoryDecisionLedger,
    InvalidTransitionError,
    LedgerError,
    RecordTimeError,
)
from fantasy_gm.domain.actions import (
    DraftSelection,
    FreeAgentAddDrop,
    RespondToTrade,
    SetLineup,
    WaiverClaim,
)
from fantasy_gm.domain.artifacts import FitKind, ModelArtifactManifest
from fantasy_gm.domain.autonomy import (
    AutonomyMode,
    AutonomyPolicy,
    DecisionType,
    FreshnessContext,
    FreshnessPolicy,
)
from fantasy_gm.domain.base import sha256_hex
from fantasy_gm.domain.clock import ManualClock
from fantasy_gm.domain.decision import (
    ActorKind,
    CounterfactualEstimate,
    Decision,
    DecisionStatus,
    ObservedOutcome,
    OutcomeDistribution,
)
from fantasy_gm.domain.evidence import EvidenceEntry, EvidenceKind, EvidenceManifest
from fantasy_gm.domain.execution import (
    ExecutionEvent,
    ExecutionEventKind,
    action_fingerprint,
    idempotency_key,
)
from fantasy_gm.domain.identity import (
    EntityType,
    IdentityMappingEvent,
    MappingMethod,
    MappingStatus,
    ProviderRef,
)
from fantasy_gm.domain.ids import new_attempt_id, new_game_id, new_player_id
from fantasy_gm.domain.knowledge import LeakageError
from fantasy_gm.domain.league import LeagueScoringSettings, PlatformEligibility, StatKey
from fantasy_gm.domain.nfl import (
    ContextSourceKind,
    DraftCapital,
    InjuryDesignation,
    InjuryReportType,
    InjuryStatus,
    Position,
    PreseasonGameContext,
    SeasonPhase,
    UsageMetric,
    UsageSnapshot,
)
from fantasy_gm.domain.risk import ActionRiskClass, RiskPolicy, assess_action_risk
from fantasy_gm.domain.roles import RoleDimension, RoleEstimateStatus, RoleFamily
from fantasy_gm.domain.run_context import RunContext, RunMode
from fantasy_gm.domain.seeds import SeedSpec
from fantasy_gm.domain.time import (
    InsufficientTimestampAction,
    KnowledgeCutoff,
    KnowledgeMode,
    TimestampPolicy,
    TimestampQuality,
)
from fantasy_gm.domain.validation import ModelValidationRecord, ValidationState
from fantasy_gm.grading.grader import evaluate_counterfactual, grade_observed
from fantasy_gm.knowledge.session import SessionSealedError
from fantasy_gm.leagues.scoring import score_stat_line
from fantasy_gm.leagues.state import reconstruct_league_state
from fantasy_gm.models.registry import ModelRegistry
from fantasy_gm.organizational_intent import components as comp
from fantasy_gm.organizational_intent.config import IntentConfigV0
from fantasy_gm.organizational_intent.engine import compute_intent_snapshot
from fantasy_gm.organizational_intent.inputs import gather_intent_inputs
from fantasy_gm.organizational_intent.snapshot import IntentComponent
from fantasy_gm.persistence.ledger import SqlDecisionLedger
from fantasy_gm.persistence.observation_store import SqlObservationStore
from fantasy_gm.player_state.roles import observed_role_vector
from fantasy_gm.player_state.store import (
    InMemoryObservationStore,
    ObservationQuery,
    ObservationStore,
)
from fantasy_gm.providers import interfaces as provider_interfaces
from fantasy_gm.providers.identity import (
    IdentityConflictError,
    IdentityRegistry,
    UnresolvedIdentityError,
)
from fantasy_gm.providers.ingestion import map_injury
from fantasy_gm.providers.interfaces import FantasyTransactionProvider
from fantasy_gm.providers.records import ProviderInjuryRecord
from fantasy_gm.records.codec import (
    EncodedRecord,
    TamperDetectedError,
    UnknownSchemaVersionError,
    VersionedCodec,
)
from fantasy_gm.records.registry import DEFAULT_CODECS
from fantasy_gm.runtime import runtime_fingerprint
from fantasy_gm.simulation.distributions import PlayerWeekDistribution, TruncatedNormalDistribution
from fantasy_gm.simulation.interfaces import MatchupRequest
from fantasy_gm.simulation.offload import run_offloop
from fantasy_gm.simulation.reference import IndependentMatchupSimulator
from tests.acceptance.test_foundation_v01 import seed_prior_heavy_wr
from tests.factories import (
    NOW,
    T0,
    fitted_artifact,
    heuristic_artifact,
    league_settings,
    lineup_decision,
    live_run,
    make_league,
    make_player,
    memory_store,
    obs_times,
    open_session,
    replay_run,
    source,
    status_event,
    team_id,
    ts,
    wr_usage,
)
from tests.fakes import LEAGUE_REF, READ_ONLY, WRITE_CAPS, FakePlatform, NullIdentity

pytestmark = pytest.mark.acceptance
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "records"


def validate(
    models: ModelRegistry,
    artifact: ModelArtifactManifest,
    clock: ManualClock,
    state: ValidationState = ValidationState.VALIDATED,
    decision_type: DecisionType = DecisionType.LINEUP,
    regime: str = "regular_season",
) -> None:
    models.register_artifact(artifact)
    models.record_validation(
        ModelValidationRecord(
            artifact_hash=artifact.artifact_hash,
            decision_type=decision_type,
            regime=regime,
            state=state,
            effective_at=clock.now(),
            approved_by="test-validation",
        )
    )


def live_decision(store: ObservationStore, clock: ManualClock, **kwargs: Any) -> Decision:
    return lineup_decision(open_session(store, clock, clock.now(), run=live_run()), **kwargs)


# ============================================================================ 1
class TestAuthoritativeRecordedTime:
    def test_live_decision_cannot_be_backdated(
        self, ledger: DecisionLedger, store: ObservationStore, clock: ManualClock
    ) -> None:
        session = open_session(store, clock, clock.now() - timedelta(hours=1), run=live_run())
        backdated = lineup_decision(session, decision_time=clock.now() - timedelta(hours=1))
        with pytest.raises(RecordTimeError, match="cannot be backdated"):
            ledger.record(backdated, status_event(backdated, DecisionStatus.RECOMMENDED))

    def test_recorded_at_comes_only_from_the_ledger_clock(
        self, ledger: DecisionLedger, store: ObservationStore, clock: ManualClock
    ) -> None:
        assert "recorded_at" not in Decision.model_fields  # callers cannot even express it
        two_min_ago = clock.now() - timedelta(minutes=2)
        decision = lineup_decision(
            open_session(store, clock, two_min_ago, run=live_run()), decision_time=two_min_ago
        )
        clock.advance(timedelta(seconds=40))
        meta = ledger.record(decision, status_event(decision, DecisionStatus.RECOMMENDED))
        entry = ledger.get(decision.decision_id)
        assert meta.recorded_at == entry.recorded_at == NOW + timedelta(seconds=40)
        assert entry.decision.value.decision_time == NOW - timedelta(minutes=2)

    def test_live_status_event_cannot_be_backdated(
        self, ledger: DecisionLedger, store: ObservationStore, clock: ManualClock
    ) -> None:
        decision = live_decision(store, clock, mode=AutonomyMode.APPROVAL_REQUIRED)
        ledger.record(decision, status_event(decision, DecisionStatus.AWAITING_APPROVAL))
        clock.advance(timedelta(hours=2))
        stale_approval = status_event(
            decision, DecisionStatus.APPROVED, at=NOW + timedelta(minutes=1), actor=ActorKind.USER
        )
        with pytest.raises(RecordTimeError):
            ledger.append_status(stale_approval)

    def test_replay_keeps_historical_time_without_faking_the_write_time(
        self, ledger: DecisionLedger, store: ObservationStore, clock: ManualClock
    ) -> None:
        decision = lineup_decision(open_session(store, clock, ts(days=6)))  # 2025 replay
        ledger.record(decision, status_event(decision, DecisionStatus.RECORDED))
        entry = ledger.get(decision.decision_id)
        assert entry.decision.value.decision_time == ts(days=6)
        assert entry.decision.value.information_cutoff == ts(days=6)
        assert entry.recorded_at == NOW  # physically written "now", in 2026
        future = lineup_decision(
            open_session(store, clock, ts(days=6)), decision_time=NOW + timedelta(days=1)
        )
        with pytest.raises(RecordTimeError, match="future"):
            ledger.record(future, status_event(future, DecisionStatus.RECORDED))

    def test_run_mode_invariants(self, store: ObservationStore, clock: ManualClock) -> None:
        with pytest.raises(ValidationError, match="SYSTEM_KNOWLEDGE"):
            RunContext(mode=RunMode.LIVE, knowledge_mode=KnowledgeMode.PUBLIC_AVAILABILITY)
        with pytest.raises(ValueError, match="future"):
            open_session(store, clock, NOW + timedelta(hours=1), run=live_run())
        paper = lineup_decision(open_session(store, clock, NOW, run=RunContext(mode=RunMode.PAPER)))
        paper_ledger = InMemoryDecisionLedger(clock)
        recommend = status_event(paper, DecisionStatus.RECOMMENDED)
        with pytest.raises(InvalidTransitionError):  # paper decisions never notify/execute
            paper_ledger.record(paper, recommend)


# ============================================================================ 2
class TestVersionedPayloads:
    @pytest.mark.parametrize("name", sorted(p.stem for p in FIXTURES.glob("*.json")))
    def test_golden_v1_records_remain_readable(self, name: str) -> None:
        record = EncodedRecord.model_validate_json((FIXTURES / f"{name}.json").read_text())
        rt = record.record_type
        codec: VersionedCodec[Any]
        if rt.startswith("observation:"):
            codec = DEFAULT_CODECS.observation(rt.split(":", 1)[1])
        else:
            codec = next(
                getattr(DEFAULT_CODECS, f.name)
                for f in dataclasses.fields(DEFAULT_CODECS)
                if f.name != "observations" and getattr(DEFAULT_CODECS, f.name).record_type == rt
            )
        value = codec.decode_record(record)
        assert sha256_hex(record.payload) == record.payload_hash
        assert value is not None

    def test_v1_decision_readable_after_synthetic_v2_change(
        self, sql_engine: Engine, clock: ManualClock
    ) -> None:
        class DecisionV2(Decision):
            objective: str  # new REQUIRED field in the hypothetical v2 model

        v1 = EncodedRecord.model_validate_json((FIXTURES / "decision_v1.json").read_text())
        with pytest.raises(ValidationError):
            DecisionV2.model_validate_json(v1.payload)  # naive parse of history breaks

        codec_v2 = VersionedCodec(
            "decision",
            DecisionV2,
            current_version=2,
            legacy_readers={
                1: lambda d: DecisionV2.model_validate({**d, "objective": "unspecified_in_v1"})
            },
        )
        upgraded = codec_v2.decode_record(v1)
        assert isinstance(upgraded, DecisionV2) and upgraded.objective == "unspecified_in_v1"
        with pytest.raises(UnknownSchemaVersionError):
            codec_v2.decode(3, v1.payload, v1.payload_hash)

        # End to end: rows written by the v1 ledger are read by a v2 ledger; bytes untouched.
        store = SqlObservationStore(sql_engine, clock)
        decision = lineup_decision(open_session(store, clock, ts(days=6)))
        SqlDecisionLedger(sql_engine, clock).record(
            decision, status_event(decision, DecisionStatus.RECORDED)
        )
        v2_ledger = SqlDecisionLedger(
            sql_engine, clock, codecs=dataclasses.replace(DEFAULT_CODECS, decision=codec_v2)
        )
        entry = v2_ledger.get(decision.decision_id)
        assert isinstance(entry.decision.value, DecisionV2)
        assert entry.decision.meta.schema_version == 1
        assert entry.decision_hash == decision.content_hash()
        with sql_engine.connect() as conn:
            stored = conn.execute(text("SELECT payload FROM decisions")).scalar_one()
        assert stored == decision.canonical_json()

    def test_every_durable_table_carries_schema_version(self, sql_engine: Engine) -> None:
        from fantasy_gm.persistence.tables import APPEND_ONLY_TABLES, metadata

        for name in APPEND_ONLY_TABLES:
            cols = metadata.tables[name].c
            assert {"schema_version", "payload", "payload_hash", "recorded_at"} <= set(cols.keys())


# ============================================================================ 3
class _LeakyStore:
    """A faulty store that ignores the cutoff (simulates a buggy adapter)."""

    def __init__(self, inner: InMemoryObservationStore) -> None:
        self.inner = inner

    def add(self, o: Any) -> None:
        self.inner.add(o)

    def add_many(self, os: Any) -> None:
        self.inner.add_many(os)

    def known_as_of(self, cutoff: KnowledgeCutoff, query: ObservationQuery) -> list[Any]:
        return self.inner.known_as_of(KnowledgeCutoff(as_of=NOW), query)


class TestEvidenceSession:
    def test_reads_accumulate_a_verifiable_manifest(
        self, store: ObservationStore, clock: ManualClock
    ) -> None:
        player, _, _, directory = seed_prior_heavy_wr(store)
        session = open_session(store, clock, ts())
        gather_intent_inputs(session, directory, player.player_id, 2025)
        manifest = session.seal()
        observed = [e for e in manifest.entries if e.kind is EvidenceKind.OBSERVATION]
        assert len(observed) >= 5
        stored = {o.observation_id: o for o in session_free_read(store, ts())}
        for e in observed:
            assert e.known_at is not None and e.known_at <= ts()
            assert e.reference_hash == stored[e.reference_id].content_hash()
        with pytest.raises(SessionSealedError):
            session.read(ObservationQuery(player_id=player.player_id))

    def test_no_raw_store_on_the_engine_interface(self, clock: ManualClock) -> None:
        session = open_session(memory_store(), clock, ts())
        public = [n for n in dir(session) if not n.startswith("_")]
        assert "store" not in public
        assert not any("store" in n for n in public)

    def test_undeclared_evidence_is_rejected(
        self, store: ObservationStore, clock: ManualClock
    ) -> None:
        player = make_player()
        stray = wr_usage(player, team_id(), 1, routes=30)
        store.add(stray)
        session = open_session(store, clock, ts(days=30))
        with pytest.raises(LeakageError, match="not read through this session"):
            session.evidence_ids_for([stray])
        art = session.use_model(heuristic_artifact())
        with pytest.raises(LeakageError, match="not read in this session"):
            session.record_derived("x", stray, "derived:1", ["observation:" + stray.observation_id])
        assert art in session.seal().ids()

    def test_faulty_store_cannot_leak_through_a_session(self, clock: ManualClock) -> None:
        inner = memory_store()
        player = make_player()
        inner.add(wr_usage(player, team_id(), 20, routes=30))  # observed ~day 140
        session = open_session(_LeakyStore(inner), clock, ts(days=1))  # type: ignore[arg-type]
        with pytest.raises(LeakageError):
            session.read(ObservationQuery(player_id=player.player_id))

    def test_hand_built_manifest_with_late_evidence_is_invalid(self) -> None:
        run = replay_run()
        with pytest.raises(ValidationError, match="leakage"):
            EvidenceManifest(
                run_id=run.run_id,
                run_mode=run.mode,
                cutoff=KnowledgeCutoff(as_of=ts()),
                sealed_at=NOW,
                entries=(
                    EvidenceEntry(
                        evidence_id="observation:x",
                        kind=EvidenceKind.OBSERVATION,
                        reference_type="usage_snapshot",
                        reference_id="x",
                        reference_hash="h",
                        known_at=ts(days=1),
                        ingested_at=ts(days=1),
                        timestamp_quality=TimestampQuality.EXACT_PUBLICATION_TIME,
                    ),
                ),
            )


def session_free_read(store: ObservationStore, as_of: Any) -> list[Any]:
    return store.known_as_of(KnowledgeCutoff(as_of=as_of), ObservationQuery())


# ============================================================================ 4
class TestModelArtifactProvenance:
    def test_future_trained_artifact_cannot_be_used_in_earlier_backtest(
        self, store: ObservationStore, clock: ManualClock
    ) -> None:
        future_model = fitted_artifact(trained_through=ts(days=30))
        session = open_session(store, clock, ts(days=6))
        with pytest.raises(LeakageError, match="trained through"):
            session.use_model(future_model)
        with pytest.raises(LeakageError):
            lineup_decision(open_session(store, clock, ts(days=6)), artifact=future_model)
        ok = lineup_decision(open_session(store, clock, ts(days=40)), artifact=future_model)
        assert future_model.artifact_hash in ok.evidence.artifact_hashes()

    def test_manifest_forged_with_future_artifact_is_invalid(self) -> None:
        run = replay_run()
        with pytest.raises(ValidationError, match="trained through"):
            EvidenceManifest(
                run_id=run.run_id,
                run_mode=run.mode,
                cutoff=KnowledgeCutoff(as_of=ts()),
                sealed_at=NOW,
                entries=(
                    EvidenceEntry(
                        evidence_id="model_artifact:a",
                        kind=EvidenceKind.MODEL_ARTIFACT,
                        reference_type="model_artifact",
                        reference_id="a",
                        reference_hash="a",
                        fit_kind=FitKind.FITTED,
                        trained_through=ts(days=1),
                    ),
                ),
            )

    def test_manifest_contents_and_registry_roundtrip(
        self, models: ModelRegistry, clock: ManualClock
    ) -> None:
        art = fitted_artifact(trained_through=ts()).model_copy(
            update={"runtime": runtime_fingerprint(), "code_version": "abc123"}
        )
        assert models.register_artifact(art) == art.artifact_hash
        assert models.get_artifact(art.artifact_hash) == art
        for f in (
            "trained_through",
            "training_data_manifest_hash",
            "feature_schema_hash",
            "config_hash",
            "runtime",
        ):
            assert getattr(art, f) is not None
        with pytest.raises(ValidationError):
            ModelArtifactManifest(
                name="m", version="1", fit_kind=FitKind.FITTED, config_hash="c", created_at=NOW
            )  # no training window

    def test_hand_specified_artifacts_are_flagged(
        self, store: ObservationStore, clock: ManualClock
    ) -> None:
        decision = lineup_decision(open_session(store, clock, ts(days=6)))
        assert decision.evidence.hand_specified_artifacts == (decision.decision_artifact_hash,)


# ============================================================================ 5
class TestObservationIntegrity:
    def test_in_memory_tamper_detected(self, clock: ManualClock) -> None:
        store = memory_store()
        usage = wr_usage(make_player(), team_id(), 1, routes=30)
        store.add(usage)
        kind, rec, at = store._rows[usage.observation_id]
        store._rows[usage.observation_id] = (
            kind,
            rec.model_copy(update={"payload": rec.payload.replace("30.0", "45.0")}),
            at,
        )
        with pytest.raises(TamperDetectedError):
            store.known_as_of(KnowledgeCutoff(as_of=NOW), ObservationQuery())

    def test_sql_tampered_row_detected_and_updates_blocked(
        self, sql_engine: Engine, clock: ManualClock
    ) -> None:
        store = SqlObservationStore(sql_engine, clock)
        usage = wr_usage(make_player(), team_id(), 1, routes=30)
        store.add(usage)
        with pytest.raises(DatabaseError, match="append-only"), sql_engine.begin() as conn:
            conn.execute(text("UPDATE observations SET payload = 'x'"))
        # An attacker/bug inserts a forged row carrying another row's hash.
        forged = usage.model_copy(update={"observation_id": "obs_forged"})
        honest = DEFAULT_CODECS.observation(usage.kind).encode(forged)
        with sql_engine.begin() as conn:
            row = dict(conn.execute(text("SELECT * FROM observations")).mappings().one())
            row.update(
                observation_id="obs_forged",
                payload=honest.payload.replace("30.0", "45.0"),
                payload_hash=honest.payload_hash,
            )
            cols = ", ".join(row)
            conn.execute(
                text(
                    f"INSERT INTO observations ({cols}) VALUES ({', '.join(':' + c for c in row)})"
                ),
                {
                    **row,
                    "payload_json": json.dumps(row["payload_json"])
                    if not isinstance(row["payload_json"], str)
                    else row["payload_json"],
                },
            )
        with pytest.raises(TamperDetectedError):
            store.known_as_of(KnowledgeCutoff(as_of=NOW), ObservationQuery())


# ============================================================================ 6
class TestTimestampTrust:
    def test_no_fabricated_publication_times(self) -> None:
        with pytest.raises(ValidationError, match="fabricated"):
            InjuryStatus(
                player_id=new_player_id(),
                designation=InjuryDesignation.OUT,
                report_type=InjuryReportType.NEWS,
                effective_at=ts(),
                observed_at=ts(),  # claims publication a day before ingestion
                source=source(ts(days=1), quality=TimestampQuality.INGESTION_TIME_ONLY),
            )
        with pytest.raises(ValidationError, match="after ingested_at"):
            InjuryStatus(
                player_id=new_player_id(),
                designation=InjuryDesignation.OUT,
                report_type=InjuryReportType.NEWS,
                effective_at=ts(),
                observed_at=ts(days=2),
                source=source(ts(days=1)),
            )

    def test_raw_timestamp_and_quality_preserved(
        self, store: ObservationStore, clock: ManualClock
    ) -> None:
        obs = InjuryStatus(
            player_id=new_player_id(),
            designation=InjuryDesignation.QUESTIONABLE,
            report_type=InjuryReportType.OFFICIAL_REPORT,
            effective_at=ts(),
            observed_at=ts(),
            source=source(ts(hours=1), quality=TimestampQuality.LAST_UPDATED_ONLY).model_copy(
                update={
                    "raw_timestamp": "Mon, 01 Sep 2025 12:00:00 GMT",
                    "raw_timestamp_field": "Last-Modified",
                }
            ),
        )
        store.add(obs)
        back = open_session(store, clock, ts(days=1)).read(ObservationQuery())[0]
        assert back.source.raw_timestamp == "Mon, 01 Sep 2025 12:00:00 GMT"
        assert back.source.timestamp_quality is TimestampQuality.LAST_UPDATED_ONLY

    def test_backtests_exclude_or_flag_untrusted_timestamps(
        self, store: ObservationStore, clock: ManualClock
    ) -> None:
        pid = new_player_id()
        event_timed = InjuryStatus(  # "observed_at" is really the game time: unsafe
            player_id=pid,
            designation=InjuryDesignation.OUT,
            report_type=InjuryReportType.NEWS,
            **obs_times(ts(), lag_hours=24 * 30, quality=TimestampQuality.PROVIDER_EVENT_TIME),
        )
        exact = InjuryStatus(
            player_id=pid,
            designation=InjuryDesignation.QUESTIONABLE,
            report_type=InjuryReportType.OFFICIAL_REPORT,
            **obs_times(ts(hours=-1)),
        )
        store.add_many([event_timed, exact])
        q = ObservationQuery(player_id=pid)
        public = replay_run(KnowledgeMode.PUBLIC_AVAILABILITY)
        strict = open_session(store, clock, ts(days=1), run=public)
        assert [o.observation_id for o in strict.read(q)] == [exact.observation_id]
        flagging = open_session(
            store,
            clock,
            ts(days=1),
            run=public,
            cutoff=KnowledgeCutoff(
                as_of=ts(days=1),
                mode=KnowledgeMode.PUBLIC_AVAILABILITY,
                timestamp_policy=TimestampPolicy(on_insufficient=InsufficientTimestampAction.FLAG),
            ),
        )
        assert len(flagging.read(q)) == 2
        manifest = flagging.seal()
        assert manifest.timestamp_suspect_ids == (f"observation:{event_timed.observation_id}",)
        # Under SYSTEM_KNOWLEDGE our own ingestion time governs; it was ingested 30 days later.
        assert [o.observation_id for o in open_session(store, clock, ts(days=1)).read(q)] == [
            exact.observation_id
        ]


def _store_with_clock(backend: Any, clock: ManualClock) -> ObservationStore:
    """A store whose physical write clock is the present (no simulated live history)."""
    if backend.engine is None:
        return InMemoryObservationStore(clock)
    return SqlObservationStore(backend.engine, clock)


class TestPhysicalStorageBoundsKnowledge:
    def test_backdated_ingestion_cannot_leak_into_system_knowledge_replay(
        self, backend: Any, clock: ManualClock
    ) -> None:
        """A backfill job that stamps an old ``ingested_at`` must not make data visible to a
        SYSTEM_KNOWLEDGE replay: knowability is bounded by the store's own write time."""
        store = _store_with_clock(backend, clock)
        pid = new_player_id()
        forged = InjuryStatus(  # claims ingestion in 2025, physically written "now" (2026)
            player_id=pid,
            designation=InjuryDesignation.OUT,
            report_type=InjuryReportType.OFFICIAL_REPORT,
            **obs_times(ts()),
        )
        store.add(forged)
        q = ObservationQuery(player_id=pid)
        assert open_session(store, clock, ts(days=1)).read(q) == []
        public = open_session(
            store, clock, ts(days=1), run=replay_run(KnowledgeMode.PUBLIC_AVAILABILITY)
        )
        assert [o.observation_id for o in public.read(q)] == [forged.observation_id]

    def test_public_availability_replay_can_be_pinned_to_a_store_snapshot(
        self, backend: Any, clock: ManualClock
    ) -> None:
        store = _store_with_clock(backend, clock)
        pid = new_player_id()
        first = InjuryStatus(
            player_id=pid,
            designation=InjuryDesignation.QUESTIONABLE,
            report_type=InjuryReportType.OFFICIAL_REPORT,
            **obs_times(ts()),
        )
        store.add(first)
        snapshot = clock.now()
        clock.advance(timedelta(days=30))
        late_backfill = InjuryStatus(
            player_id=pid,
            designation=InjuryDesignation.OUT,
            report_type=InjuryReportType.NEWS,
            **obs_times(ts(hours=1)),
        )
        store.add(late_backfill)
        run = replay_run(KnowledgeMode.PUBLIC_AVAILABILITY)
        pinned = KnowledgeCutoff(
            as_of=ts(days=1), mode=KnowledgeMode.PUBLIC_AVAILABILITY, stored_by=snapshot
        )
        q = ObservationQuery(player_id=pid)
        ids = [
            o.observation_id
            for o in open_session(store, clock, ts(days=1), run=run, cutoff=pinned).read(q)
        ]
        assert ids == [first.observation_id]  # repeatable despite later backfill
        unpinned = open_session(store, clock, ts(days=1), run=run).read(q)
        assert len(unpinned) == 2


# ============================================================================ 7
class TestTemporalLeagueState:
    def test_midseason_rule_change_does_not_rewrite_history(
        self, store: ObservationStore, clock: ManualClock
    ) -> None:
        league = make_league()
        store.add_many(league_settings(league, ts(days=-10)))
        old_rules = league_settings(league, ts(days=-10))[0]
        assert isinstance(old_rules, LeagueScoringSettings)
        full_ppr = old_rules.scoring.model_copy(
            update={"per_stat": {**old_rules.scoring.per_stat, StatKey.REC: Decimal(1)}}
        )
        # Commissioner announces in week 5 a change effective week 6.
        store.add(
            LeagueScoringSettings(
                league_id=league.league_id,
                scoring=full_ppr,
                **obs_times(ts(days=42), observed=ts(days=35)),
            )
        )
        store.add(
            PlatformEligibility(
                league_id=league.league_id,
                player_id=new_player_id(),
                positions=frozenset({Position.TE}),
                **obs_times(ts(days=1)),
            )
        )
        wr_line = {StatKey.REC: 10}

        def rec_points(day: float) -> Decimal:
            state = reconstruct_league_state(open_session(store, clock, ts(days=day)), league)
            return score_stat_line(wr_line, Position.WR, state.rules().scoring).total

        assert rec_points(21) == Decimal(5)  # week 3 decision, replayed after the change
        assert rec_points(38) == Decimal(5)  # announced but not yet effective
        assert rec_points(43) == Decimal(10)

        session = open_session(store, clock, ts(days=21))
        state = reconstruct_league_state(session, league)
        assert state.faab_budget == 100 and len(state.eligibility) == 1
        manifest_ids = session.seal().ids()
        assert all(f"observation:{oid}" in manifest_ids for oid in state.source_observation_ids)

    def test_league_identity_is_separate_from_configuration(self) -> None:
        from fantasy_gm.domain.league import League

        assert not {"scoring", "roster_rules", "waivers"} & set(League.model_fields)


# ============================================================================ 8
class TestLatentRoleVector:
    def test_deployment_and_target_earning_are_separate_dimensions(self) -> None:
        a, b, team = (
            make_player("Deployed", Position.WR),
            make_player("Earner", Position.WR),
            team_id(),
        )
        a_games = [
            wr_usage(a, team, w, routes=36, extra={UsageMetric.TARGETS: 3.6}) for w in range(1, 5)
        ]
        b_games = [
            wr_usage(b, team, w, routes=24, extra={UsageMetric.TARGETS: 7.2}) for w in range(1, 5)
        ]
        va = observed_role_vector(a.player_id, Position.WR, a_games, ts(days=40))
        vb = observed_role_vector(b.player_id, Position.WR, b_games, ts(days=40))
        rp, tprr = RoleDimension.REC_ROUTE_PARTICIPATION, RoleDimension.REC_TARGETS_PER_ROUTE
        assert va.value(rp) == pytest.approx(0.9) and va.value(tprr) == pytest.approx(0.10)
        assert vb.value(rp) == pytest.approx(0.6) and vb.value(tprr) == pytest.approx(0.30)
        assert va.estimates[rp].family is RoleFamily.DEPLOYMENT
        assert va.estimates[tprr].family is RoleFamily.TARGET_EARNING
        # Unobserved is not zero; reserved dimensions are explicit.
        assert (
            va.estimates[RoleDimension.REC_AIR_YARD_SHARE].status is RoleEstimateStatus.UNOBSERVED
        )
        assert va.estimates[RoleDimension.REC_AIR_YARD_SHARE].value is None
        assert (
            va.estimates[RoleDimension.REC_SLOT_RATE].status is RoleEstimateStatus.NOT_YET_SUPPORTED
        )

    def test_rb_dimensions_are_independent(self) -> None:
        rb, team = make_player("RB", Position.RB), team_id()
        u = UsageSnapshot(
            player_id=rb.player_id,
            team_id=team,
            game_id=new_game_id(),
            season=2025,
            week=1,
            phase=SeasonPhase.REGULAR,
            metrics={
                UsageMetric.OFFENSE_SNAPS: 40,
                UsageMetric.TEAM_OFFENSE_SNAPS: 64,
                UsageMetric.CARRIES: 18,
                UsageMetric.TEAM_CARRIES: 25,
                UsageMetric.GOAL_LINE_CARRIES: 0,
                UsageMetric.TEAM_GOAL_LINE_CARRIES: 3,
                UsageMetric.ROUTES: 8,
                UsageMetric.TEAM_DROPBACKS: 35,
                UsageMetric.PASS_BLOCK_SNAPS: 2,
            },
            **obs_times(ts()),
        )
        v = observed_role_vector(rb.player_id, Position.RB, [u], ts(days=1))
        assert v.value(RoleDimension.RB_CARRY_SHARE) == pytest.approx(0.72)
        assert v.value(RoleDimension.RB_GOAL_LINE_SHARE) == 0.0  # observed zero is real zero
        assert v.value(RoleDimension.RB_PASS_BLOCK_RATE) == pytest.approx(0.2)
        assert v.value(RoleDimension.RB_THIRD_DOWN_SHARE) is None

    def test_intent_components_are_priors_on_specific_dimensions(
        self, store: ObservationStore, clock: ManualClock
    ) -> None:
        player, _, _, directory = seed_prior_heavy_wr(store)
        inputs = gather_intent_inputs(
            open_session(store, clock, ts()), directory, player.player_id, 2025
        )
        snap = compute_intent_snapshot(inputs, ts())
        usage = snap.component(IntentComponent.ACTUAL_USAGE)
        assert RoleDimension.REC_ROUTE_PARTICIPATION in usage.informs
        assert RoleDimension.REC_TARGETS_PER_ROUTE not in usage.informs
        draft = snap.component(IntentComponent.DRAFT_INVESTMENT)
        assert RoleDimension.REC_TARGETS_PER_ROUTE in draft.informs


# ============================================================================ 9
class TestConditionalPreseason:
    def _vet(self) -> tuple[Any, Any, DraftCapital]:
        p, team = make_player("Vet", Position.WR), team_id()
        draft = DraftCapital(
            player_id=p.player_id,
            draft_year=2019,
            round=1,
            overall_pick=5,
            drafting_team_id=team,
            **obs_times(ts(days=-2000)),
        )
        return p, team, draft

    def _pre(self, p: Any, team: Any, first: float, game: Any) -> UsageSnapshot:
        return wr_usage(
            p,
            team,
            1,
            routes=0,
            phase=SeasonPhase.PRESEASON,
            game=game,
            effective=ts(days=-20),
            extra={UsageMetric.FIRST_TEAM_SNAPS: first, UsageMetric.TEAM_FIRST_TEAM_SNAPS: 20},
        )

    def _ctx(self, team: Any, game: Any, rested: bool | None) -> PreseasonGameContext:
        return PreseasonGameContext(
            team_id=team,
            game_id=game,
            season=2025,
            starters_rested=rested,
            source_kind=ContextSourceKind.OFFICIAL_PARTICIPATION,
            **obs_times(ts(days=-19)),
        )

    def test_rested_veteran_non_usage_is_not_negative_evidence(self) -> None:
        p, team, draft = self._vet()
        game = new_game_id()
        res = comp.preseason_deployment(
            [self._pre(p, team, 0, game)],
            [self._ctx(team, game, True)],
            draft,
            2025,
            team,
            IntentConfigV0(),
        )
        assert res.score is None and res.confidence == 0
        assert any("not negative evidence" in n for n in res.notes)

    def test_missing_context_lowers_confidence(self) -> None:
        p, team, draft = self._vet()
        game = new_game_id()
        cfg = IntentConfigV0()
        known = comp.preseason_deployment(
            [self._pre(p, team, 4, game)], [self._ctx(team, game, False)], draft, 2025, team, cfg
        )
        unknown = comp.preseason_deployment(
            [self._pre(p, team, 4, game)], [], draft, 2025, team, cfg
        )
        no_draft = comp.preseason_deployment(
            [self._pre(p, team, 4, game)], [], None, 2025, team, cfg
        )
        assert known.score == unknown.score == pytest.approx(0.2)
        assert known.confidence > unknown.confidence > no_draft.confidence > 0

    def test_rookie_first_team_usage_counts(self) -> None:
        p, team = make_player("Rookie", Position.WR), team_id()
        draft = DraftCapital(
            player_id=p.player_id,
            draft_year=2025,
            round=2,
            overall_pick=50,
            drafting_team_id=team,
            **obs_times(ts(days=-130)),
        )
        game = new_game_id()
        res = comp.preseason_deployment(
            [self._pre(p, team, 17, game)],
            [self._ctx(team, game, True)],
            draft,
            2025,
            team,
            IntentConfigV0(),
        )
        assert res.score == pytest.approx(0.85) and "rookie" in res.notes[0]


# ============================================================================ 10
class TestAsyncProviderContracts:
    def test_external_io_contracts_are_async(self) -> None:
        for proto in (
            provider_interfaces.NFLPlayerProvider,
            provider_interfaces.UsageProvider,
            provider_interfaces.InjuryProvider,
            provider_interfaces.ScheduleProvider,
            provider_interfaces.MarketADPProvider,
            provider_interfaces.FantasyLeagueProvider,
            provider_interfaces.FantasyTransactionProvider,
        ):
            methods = [
                m for n, m in vars(proto).items() if inspect.isfunction(m) and not n.startswith("_")
            ]
            assert methods and all(inspect.iscoroutinefunction(m) for m in methods), proto

    def test_fake_satisfies_protocol(self, clock: ManualClock) -> None:
        assert isinstance(FakePlatform(clock), FantasyTransactionProvider)

    async def test_simulation_runs_off_the_event_loop(self) -> None:
        req = MatchupRequest(
            home_team_id="ft_a",
            away_team_id="ft_b",  # type: ignore[arg-type]
            home_lineup=tuple(_dist(f"plr_h{i}", 12.0) for i in range(9)),
            away_lineup=tuple(_dist(f"plr_a{i}", 11.0) for i in range(9)),
            n_sims=200_000,
            seed=SeedSpec(root_seed=11),
        )
        sim = IndependentMatchupSimulator()
        ticks = 0

        async def heartbeat() -> None:
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.005)

        hb = asyncio.create_task(heartbeat())
        with ProcessPoolExecutor(max_workers=1) as pool:
            remote = await run_offloop(sim.simulate, req, pool)
        hb.cancel()
        assert ticks > 3  # the loop kept serving other work while the sim ran
        assert remote == sim.simulate(req)  # identical across the process boundary


def _dist(pid: str, mean: float) -> PlayerWeekDistribution:
    return PlayerWeekDistribution(
        player_id=pid,
        season=2025,
        week=3,
        p_active=0.95,
        conditional=TruncatedNormalDistribution(mean=mean, stdev=6.0, lower=-2.0),
        source_model="test",
        source_artifact_hash=sha256_hex(f"model:{pid[:5]}"),
    )


# ============================================================================ 11
class TestValidationStateGate:
    def test_unvalidated_model_never_executes_autonomously(
        self, store: ObservationStore, clock: ManualClock, models: ModelRegistry
    ) -> None:
        league = make_league()
        policy = AutonomyPolicy(league_id=league.league_id, default_mode=AutonomyMode.AUTONOMOUS)
        assert "min_autonomous_confidence" not in AutonomyPolicy.model_fields
        d = live_decision(
            store, clock, league_id=league.league_id, mode=AutonomyMode.AUTONOMOUS, confidence=1.0
        )
        models.register_artifact(heuristic_artifact())
        r = route_decision(d, policy, WRITE_CAPS, models, clock.now())
        assert r.routing is Routing.REQUEST_APPROVAL
        assert r.validation_state is ValidationState.UNVALIDATED

    def test_validation_is_per_decision_type_and_regime(
        self, store: ObservationStore, clock: ManualClock, models: ModelRegistry
    ) -> None:
        league = make_league()
        policy = AutonomyPolicy(league_id=league.league_id, default_mode=AutonomyMode.AUTONOMOUS)
        art = heuristic_artifact("engine")
        validate(models, art, clock, regime="playoffs")
        d = live_decision(
            store, clock, league_id=league.league_id, mode=AutonomyMode.AUTONOMOUS, artifact=art
        )
        assert route_decision(d, policy, WRITE_CAPS, models, clock.now()).routing is (
            Routing.REQUEST_APPROVAL
        )
        validate(models, art, clock, regime="regular_season")
        assert route_decision(d, policy, WRITE_CAPS, models, clock.now()).routing is (
            Routing.EXECUTE
        )

    def test_degradation_after_approval_blocks_execution(
        self,
        ledger: DecisionLedger,
        store: ObservationStore,
        clock: ManualClock,
        models: ModelRegistry,
    ) -> None:
        league = make_league()
        policy = AutonomyPolicy(league_id=league.league_id, default_mode=AutonomyMode.AUTONOMOUS)
        art = heuristic_artifact("engine")
        validate(models, art, clock)
        d = live_decision(
            store, clock, league_id=league.league_id, mode=AutonomyMode.AUTONOMOUS, artifact=art
        )
        r = route_decision(d, policy, WRITE_CAPS, models, clock.now())
        ledger.record(d, status_event(d, r.initial_status, actor=ActorKind.POLICY))
        clock.advance(timedelta(seconds=10))
        validate(models, art, clock, state=ValidationState.DEGRADED)
        auth = authorize_execution(
            ledger.get(d.decision_id),
            policy,
            WRITE_CAPS,
            models,
            clock.now(),
            execution_enabled=True,
        )
        assert not auth.allowed and any("degraded" in x for x in auth.reasons)

    def test_unvalidated_calibrator_blocks(
        self, store: ObservationStore, clock: ManualClock, models: ModelRegistry
    ) -> None:
        league = make_league()
        policy = AutonomyPolicy(league_id=league.league_id, default_mode=AutonomyMode.AUTONOMOUS)
        cal = heuristic_artifact("calibrator")
        models.register_artifact(cal)
        art = heuristic_artifact("engine").model_copy(
            update={"calibration_artifact_hash": cal.artifact_hash, "calibration_version": "1"}
        )
        validate(models, art, clock)
        d = live_decision(
            store, clock, league_id=league.league_id, mode=AutonomyMode.AUTONOMOUS, artifact=art
        )
        r = route_decision(d, policy, WRITE_CAPS, models, clock.now())
        assert r.routing is Routing.REQUEST_APPROVAL
        assert any("calibrator" in x for x in r.reasons)


# ============================================================================ 12
class TestActionRiskPolicy:
    def test_default_classification(self) -> None:
        p = RiskPolicy(protected_player_ids=frozenset({"plr_star"}))  # type: ignore[arg-type]
        pid = new_player_id()
        cases = {
            ActionRiskClass.LOW: SetLineup(
                week=1,
                entries=(
                    __import__("fantasy_gm.domain.league", fromlist=["RosterEntry"]).RosterEntry(
                        player_id=pid, slot_label="FLEX"
                    ),
                ),
            ),
            ActionRiskClass.MEDIUM: WaiverClaim(add_player_id=pid, faab_bid=10),
            ActionRiskClass.CRITICAL: WaiverClaim(add_player_id=pid, faab_bid=60),
        }
        for expected, action in cases.items():
            assert assess_action_risk(action, p, 100).risk_class is expected
        drop_star = FreeAgentAddDrop(add_player_id=pid, drop_player_id="plr_star")  # type: ignore[arg-type]
        assert assess_action_risk(drop_star, p, 100).risk_class is ActionRiskClass.HIGH
        assert (
            assess_action_risk(
                RespondToTrade(provider_trade_ref="t", accept=True), p, 100
            ).risk_class
            is ActionRiskClass.CRITICAL
        )
        assert (
            assess_action_risk(DraftSelection(player_id=pid, overall_pick=1), p, None).risk_class
            is ActionRiskClass.CRITICAL
        )

    def test_policy_is_configurable_not_hardcoded(self) -> None:
        pid = new_player_id()
        relaxed = RiskPolicy(
            draft_selection_class=ActionRiskClass.MEDIUM, max_autonomous_risk=ActionRiskClass.HIGH
        )
        assert (
            assess_action_risk(
                DraftSelection(player_id=pid, overall_pick=40), relaxed, None
            ).risk_class
            is ActionRiskClass.MEDIUM
        )

    def test_high_risk_requires_approval_even_when_validated_and_autonomous(
        self, store: ObservationStore, clock: ManualClock, models: ModelRegistry
    ) -> None:
        league = make_league()
        art = heuristic_artifact("engine")
        validate(models, art, clock)
        d = live_decision(
            store, clock, league_id=league.league_id, mode=AutonomyMode.AUTONOMOUS, artifact=art
        )
        tight = AutonomyPolicy(
            league_id=league.league_id,
            default_mode=AutonomyMode.AUTONOMOUS,
            risk=RiskPolicy(lineup_class=ActionRiskClass.HIGH),
        )
        r = route_decision(d, tight, WRITE_CAPS, models, clock.now())
        assert r.routing is Routing.REQUEST_APPROVAL and r.risk.risk_class is ActionRiskClass.HIGH
        loose = tight.model_copy(
            update={
                "risk": RiskPolicy(
                    lineup_class=ActionRiskClass.HIGH, max_autonomous_risk=ActionRiskClass.HIGH
                )
            }
        )
        assert route_decision(d, loose, WRITE_CAPS, models, clock.now()).routing is (
            Routing.EXECUTE
        )


# ============================================================================ 13
class _Harness:
    def __init__(
        self,
        ledger: DecisionLedger,
        store: ObservationStore,
        clock: ManualClock,
        models: ModelRegistry,
        platform: FakePlatform,
    ) -> None:
        self.ledger, self.clock, self.models, self.platform = ledger, clock, models, platform
        league = make_league()
        art = heuristic_artifact("engine")
        validate(models, art, clock)
        self.policy = AutonomyPolicy(
            league_id=league.league_id, default_mode=AutonomyMode.AUTONOMOUS
        )
        self.decision = live_decision(
            store, clock, league_id=league.league_id, mode=AutonomyMode.AUTONOMOUS, artifact=art
        )
        r = route_decision(self.decision, self.policy, WRITE_CAPS, models, clock.now())
        assert r.routing is Routing.EXECUTE
        ledger.record(
            self.decision, status_event(self.decision, r.initial_status, actor=ActorKind.POLICY)
        )

    def service(self, worker: str = "w1", timeout: float = 5.0) -> ExecutionService:
        return ExecutionService(
            self.ledger,
            self.platform,
            NullIdentity(),
            self.models,
            self.clock,
            execution_enabled=True,
            worker_id=worker,
            provider_timeout=timedelta(seconds=timeout),
        )

    async def run(self, worker: str = "w1", timeout: float = 5.0) -> Any:
        return await self.service(worker, timeout).execute(
            self.decision.decision_id, self.policy, LEAGUE_REF
        )

    @property
    def status(self) -> DecisionStatus:
        return self.ledger.get(self.decision.decision_id).current_status


class TestExecutionIdempotency:
    async def test_two_concurrent_requests_execute_once(
        self,
        ledger: DecisionLedger,
        store: ObservationStore,
        clock: ManualClock,
        models: ModelRegistry,
    ) -> None:
        h = _Harness(ledger, store, clock, models, FakePlatform(clock, delay=0.2))
        results = await asyncio.gather(h.run("w1"), h.run("w2"), h.run("w3"))
        outcomes = sorted(r.outcome.value for r in results)
        assert outcomes.count(AttemptOutcome.EXECUTED.value) == 1
        assert h.platform.execute_calls == 1 and h.platform.executed == 1
        assert set(outcomes) <= {
            AttemptOutcome.EXECUTED.value,
            AttemptOutcome.IN_PROGRESS_ELSEWHERE.value,
        }
        assert (await h.run("w4")).outcome is AttemptOutcome.ALREADY_EXECUTED
        assert h.platform.execute_calls == 1

    async def test_success_with_lost_response_is_reconciled_not_repeated(
        self,
        ledger: DecisionLedger,
        store: ObservationStore,
        clock: ManualClock,
        models: ModelRegistry,
    ) -> None:
        h = _Harness(ledger, store, clock, models, FakePlatform(clock, script=["lose_response"]))
        first = await h.run()
        assert first.outcome is AttemptOutcome.UNCERTAIN
        assert h.status is DecisionStatus.EXECUTION_UNCERTAIN
        second = await h.run()
        assert second.outcome is AttemptOutcome.RECONCILED_EXECUTED
        assert second.provider_transaction_ref == "txn-1"
        assert h.platform.execute_calls == 1 and h.platform.executed == 1
        assert h.status is DecisionStatus.EXECUTED

    async def test_retry_after_timeout_without_execution(
        self,
        ledger: DecisionLedger,
        store: ObservationStore,
        clock: ManualClock,
        models: ModelRegistry,
    ) -> None:
        h = _Harness(ledger, store, clock, models, FakePlatform(clock, script=["fail_before"]))
        assert (await h.run()).outcome is AttemptOutcome.UNCERTAIN
        # Immediately retrying is refused: the platform might still commit the first request.
        early = await h.run()
        assert early.outcome is AttemptOutcome.UNCERTAIN
        assert any("settle window" in r for r in early.reasons)
        assert h.platform.execute_calls == 1
        clock.advance(timedelta(minutes=3))
        retry = await h.run()
        assert retry.outcome is AttemptOutcome.EXECUTED
        assert h.platform.execute_calls == 2 and h.platform.executed == 1
        events = ledger.get(h.decision.decision_id).executions
        kinds = [e.kind for e in events]
        assert kinds == [
            ExecutionEventKind.ATTEMPT_STARTED,
            ExecutionEventKind.OUTCOME_UNKNOWN,
            ExecutionEventKind.RECONCILED_NOT_EXECUTED,
            ExecutionEventKind.ATTEMPT_STARTED,
            ExecutionEventKind.PROVIDER_CONFIRMED,
        ]
        assert len({e.idempotency_key for e in events}) == 1
        assert events[0].idempotency_key == idempotency_key(
            h.decision.decision_id, h.decision.selected.action
        )
        assert events[0].action_fingerprint == action_fingerprint(h.decision.selected.action)

    async def test_hung_provider_times_out_to_uncertain(
        self,
        ledger: DecisionLedger,
        store: ObservationStore,
        clock: ManualClock,
        models: ModelRegistry,
    ) -> None:
        h = _Harness(ledger, store, clock, models, FakePlatform(clock, script=["hang"]))
        assert (await h.run(timeout=0.2)).outcome is AttemptOutcome.UNCERTAIN
        assert h.status is DecisionStatus.EXECUTION_UNCERTAIN

    async def test_capability_revoked_between_decision_and_retry(
        self,
        ledger: DecisionLedger,
        store: ObservationStore,
        clock: ManualClock,
        models: ModelRegistry,
    ) -> None:
        h = _Harness(ledger, store, clock, models, FakePlatform(clock, script=["fail_before"]))
        assert (await h.run()).outcome is AttemptOutcome.UNCERTAIN
        h.platform.capabilities = READ_ONLY
        clock.advance(timedelta(minutes=3))
        retry = await h.run()
        assert retry.outcome is AttemptOutcome.REFUSED
        assert any("lacks required capability" in r for r in retry.reasons)
        assert h.platform.reconcile_calls == 1 and h.platform.execute_calls == 1
        assert h.status is DecisionStatus.EXECUTION_BLOCKED

    async def test_decision_cannot_expire_while_execution_in_flight(
        self,
        ledger: DecisionLedger,
        store: ObservationStore,
        clock: ManualClock,
        models: ModelRegistry,
    ) -> None:
        h = _Harness(ledger, store, clock, models, FakePlatform(clock, delay=0.2))
        task = asyncio.create_task(h.run())
        await asyncio.sleep(0.05)  # claim is in flight
        with pytest.raises(InvalidTransitionError, match="in_flight"):
            ledger.append_status(status_event(h.decision, DecisionStatus.EXPIRED, at=clock.now()))
        assert (await task).outcome is AttemptOutcome.EXECUTED
        assert h.status is DecisionStatus.EXECUTED

    async def test_native_idempotency_retries_with_same_key(
        self,
        ledger: DecisionLedger,
        store: ObservationStore,
        clock: ManualClock,
        models: ModelRegistry,
    ) -> None:
        platform = FakePlatform(clock, native_idempotency=True, script=["lose_response"])
        h = _Harness(ledger, store, clock, models, platform)
        assert (await h.run()).outcome is AttemptOutcome.UNCERTAIN
        retry = await h.run()
        assert retry.outcome is AttemptOutcome.EXECUTED
        assert platform.executed == 1 and platform.reconcile_calls == 0
        assert retry.provider_transaction_ref == "txn-1"

    async def test_crashed_worker_lease_expiry_forces_reconciliation(
        self,
        ledger: DecisionLedger,
        store: ObservationStore,
        clock: ManualClock,
        models: ModelRegistry,
    ) -> None:
        h = _Harness(ledger, store, clock, models, FakePlatform(clock))
        d = h.decision
        ledger.append_execution_event(
            ExecutionEvent(
                decision_id=d.decision_id,
                attempt_id=new_attempt_id(),
                kind=ExecutionEventKind.ATTEMPT_STARTED,
                occurred_at=clock.now(),
                provider="fake_platform",
                idempotency_key=idempotency_key(d.decision_id, d.selected.action),
                action_fingerprint=action_fingerprint(d.selected.action),
                worker_id="crashed",
                lease_expires_at=clock.now() + timedelta(minutes=2),
            ),
            expected_seq=0,
        )
        assert (await h.run()).outcome is AttemptOutcome.IN_PROGRESS_ELSEWHERE
        clock.advance(timedelta(minutes=3))
        result = await h.run()
        assert result.outcome is AttemptOutcome.EXECUTED
        assert h.platform.reconcile_calls == 1 and h.platform.executed == 1

    async def test_undeterminable_reconciliation_never_retries(
        self,
        ledger: DecisionLedger,
        store: ObservationStore,
        clock: ManualClock,
        models: ModelRegistry,
    ) -> None:
        from fantasy_gm.providers.interfaces import ReconciliationStatus

        platform = FakePlatform(
            clock, script=["fail_before"], reconcile_status=ReconciliationStatus.UNDETERMINABLE
        )
        h = _Harness(ledger, store, clock, models, platform)
        await h.run()
        assert (await h.run()).outcome is AttemptOutcome.UNCERTAIN
        assert platform.execute_calls == 1
        with pytest.raises(InvalidTransitionError):  # an uncertain execution cannot expire
            ledger.append_status(status_event(h.decision, DecisionStatus.EXPIRED, at=clock.now()))


# ============================================================================ 14
class TestFreshness:
    def test_limits_are_decision_type_and_context_specific(self) -> None:
        f = FreshnessPolicy()
        ctx = FreshnessContext()
        assert f.limit(DecisionType.DRAFT_PICK, ctx) == timedelta(seconds=90)
        assert f.limit(
            DecisionType.DRAFT_PICK, FreshnessContext(live_draft_on_clock=True)
        ) == timedelta(seconds=30)
        assert f.limit(DecisionType.LINEUP, ctx) == timedelta(minutes=30)
        assert f.limit(
            DecisionType.LINEUP, FreshnessContext(minutes_to_lineup_lock=20)
        ) == timedelta(minutes=5)
        assert f.limit(DecisionType.TRADE_PROPOSAL, ctx) == timedelta(hours=48)

    def test_near_lock_lineup_goes_stale_quickly(
        self,
        ledger: DecisionLedger,
        store: ObservationStore,
        clock: ManualClock,
        models: ModelRegistry,
    ) -> None:
        h = _Harness(ledger, store, clock, models, FakePlatform(clock))
        clock.advance(timedelta(minutes=10))
        entry = ledger.get(h.decision.decision_id)

        def auth(ctx: FreshnessContext) -> Any:
            return authorize_execution(
                entry,
                h.policy,
                WRITE_CAPS,
                models,
                clock.now(),
                execution_enabled=True,
                freshness=ctx,
            )

        assert auth(FreshnessContext()).allowed
        near = auth(FreshnessContext(minutes_to_lineup_lock=15))
        assert not near.allowed and any("stale" in r for r in near.reasons)


# ============================================================================ 15
class TestConcurrentLedgerAppends:
    def test_racing_status_appends_serialise_cleanly(
        self, ledger: DecisionLedger, store: ObservationStore, clock: ManualClock
    ) -> None:
        d = live_decision(store, clock, mode=AutonomyMode.APPROVAL_REQUIRED)
        ledger.record(d, status_event(d, DecisionStatus.AWAITING_APPROVAL))
        barrier = threading.Barrier(8)
        results: list[str] = []

        def worker(i: int) -> None:
            status = DecisionStatus.APPROVED if i % 2 else DecisionStatus.REJECTED
            barrier.wait()
            try:
                ledger.append_status(status_event(d, status, at=clock.now(), actor=ActorKind.USER))
                results.append("ok")
            except (InvalidTransitionError, ConcurrentModificationError) as exc:
                results.append(type(exc).__name__)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results.count("ok") == 1
        assert set(results) - {"ok"} <= {"InvalidTransitionError", "ConcurrentModificationError"}
        assert len(ledger.get(d.decision_id).status_history) == 2

    def test_optimistic_expected_sequence(
        self, ledger: DecisionLedger, store: ObservationStore, clock: ManualClock
    ) -> None:
        d = live_decision(store, clock, mode=AutonomyMode.APPROVAL_REQUIRED)
        ledger.record(d, status_event(d, DecisionStatus.AWAITING_APPROVAL))
        with pytest.raises(ConcurrentModificationError):
            ledger.append_status(
                status_event(d, DecisionStatus.REJECTED, at=clock.now()), expected_seq=5
            )
        ledger.append_status(
            status_event(d, DecisionStatus.REJECTED, at=clock.now()), expected_seq=1
        )


# ============================================================================ 17
def _event(
    ref: ProviderRef,
    internal: str | None,
    status: MappingStatus = MappingStatus.VERIFIED,
    **kw: Any,
) -> IdentityMappingEvent:
    return IdentityMappingEvent(
        ref=ref,
        internal_id=internal,
        status=status,
        method=kw.pop("method", MappingMethod.CURATED_CROSSWALK),
        confidence=kw.pop("confidence", 1.0),
        asserted_by=kw.pop("asserted_by", "crosswalk:test@1"),
        **kw,
    )


class TestIdentityCorrection:
    def test_correction_preserves_history_and_point_in_time_belief(
        self, identity: IdentityRegistry, clock: ManualClock
    ) -> None:
        ref = ProviderRef(provider="sleeper", entity_type=EntityType.PLAYER, external_id="S-9")
        wrong, right = new_player_id(), new_player_id()
        first = _event(ref, wrong)
        t1 = identity.assert_mapping(first)
        clock.advance(timedelta(days=3))
        with pytest.raises(IdentityConflictError, match="no silent reassignment"):
            identity.assert_mapping(_event(ref, right))
        with pytest.raises(ValidationError, match="reason"):
            _event(ref, right, supersedes_event_id=first.event_id)
        t2 = identity.assert_mapping(
            _event(
                ref,
                right,
                supersedes_event_id=first.event_id,
                reason="crosswalk v2 fixed a swapped id",
                asserted_by="manual:reviewer",
            )
        )
        res_then = identity.resolve(ref, as_of=t1 + timedelta(days=1))
        res_now = identity.resolve(ref)
        assert res_then is not None and res_then.internal_id == wrong
        assert res_now is not None and res_now.internal_id == right and res_now.recorded_at == t2
        assert [e.internal_id for e, _ in identity.history(ref)] == [wrong, right]

    def test_ingestion_refuses_non_verified_mappings(
        self, identity: IdentityRegistry, clock: ManualClock
    ) -> None:
        with pytest.raises(ValidationError, match="name matches"):
            _event(
                ProviderRef(provider="espn", entity_type=EntityType.PLAYER, external_id="1"),
                new_player_id(),
                method=MappingMethod.NAME_MATCH,
            )
        ref = ProviderRef(provider="espn", entity_type=EntityType.PLAYER, external_id="E-5")
        identity.assert_mapping(
            _event(
                ref,
                new_player_id(),
                MappingStatus.UNVERIFIED,
                method=MappingMethod.NAME_MATCH,
                confidence=0.7,
            )
        )
        record = ProviderInjuryRecord(
            player_ref=ref,
            designation=InjuryDesignation.OUT,
            report_type=InjuryReportType.NEWS,
            observed_at=clock.now(),
            effective_at=clock.now(),
            timestamp_quality=TimestampQuality.EXACT_PUBLICATION_TIME,
        )
        with pytest.raises(UnresolvedIdentityError, match="unverified"):
            map_injury(record, identity, clock.now())

    def test_observations_record_the_mapping_they_were_attributed_by(
        self, identity: IdentityRegistry, clock: ManualClock
    ) -> None:
        ref = ProviderRef(provider="espn", entity_type=EntityType.PLAYER, external_id="E-6")
        first = _event(ref, new_player_id())
        identity.assert_mapping(first)
        record = ProviderInjuryRecord(
            player_ref=ref,
            designation=InjuryDesignation.OUT,
            report_type=InjuryReportType.NEWS,
            observed_at=clock.now(),
            effective_at=clock.now(),
            timestamp_quality=TimestampQuality.EXACT_PUBLICATION_TIME,
        )
        obs = map_injury(record, identity, clock.now())
        assert obs.source.identity_basis == (first.event_id,)


# ============================================================================ 18
class TestCounterfactualSeparation:
    def test_simulated_counterfactuals_are_never_observed_outcomes(
        self, ledger: DecisionLedger, store: ObservationStore, clock: ManualClock
    ) -> None:
        d = lineup_decision(open_session(store, clock, ts(days=6)))
        ledger.record(d, status_event(d, DecisionStatus.RECORDED))
        outcome = ObservedOutcome(
            decision_id=d.decision_id, known_at=ts(days=8), realized={"lineup_points": 100.0}
        )
        ledger.attach_outcome(outcome)
        estimate = CounterfactualEstimate(
            decision_id=d.decision_id,
            candidate_id=d.candidates[1].candidate_id,
            metric="lineup_points",
            distribution=OutcomeDistribution(
                metric="lineup_points", unit="pts", mean=112.0, method="sim"
            ),
            method="simulation",
            computed_at=NOW,
            model_artifact_hashes=("m",),
            uses_post_decision_information=True,
        )
        ledger.attach_counterfactual_estimate(estimate)
        with pytest.raises(ValidationError):
            ObservedOutcome.model_validate(estimate.model_dump())
        entry = ledger.get(d.decision_id)
        assert [o.value for o in entry.outcomes] == [outcome]
        observed = grade_observed(entry, outcome, NOW)
        assert observed.basis == "observed" and observed.observed_regret is None
        evaluation = evaluate_counterfactual(entry, outcome, [estimate], NOW)
        assert evaluation.basis == "model_based" and evaluation.estimated_regret == 12.0
        ledger.attach_grade(observed)
        ledger.attach_counterfactual_evaluation(evaluation)
        final = ledger.get(d.decision_id)
        assert len(final.grades) == 1 and len(final.counterfactual_evaluations) == 1
        with pytest.raises(TypeError):
            DEFAULT_CODECS.observed_outcome.encode(estimate)  # type: ignore[arg-type]


# ============================================================================ 19
class TestReproducibilityEnvelope:
    def test_simulation_envelope_identifies_the_experiment(self) -> None:
        req = MatchupRequest(
            home_team_id="ft_a",
            away_team_id="ft_b",  # type: ignore[arg-type]
            home_lineup=(_dist("plr_h1", 10.0),),
            away_lineup=(_dist("plr_a1", 10.0),),
            n_sims=100,
            seed=SeedSpec(root_seed=5, path=("exp",)),
        )
        env = IndependentMatchupSimulator().simulate(req).run
        rt = env.runtime
        assert rt.python_version and rt.numpy_version and rt.package_version
        assert rt.code_commit is not None
        lock = Path(__file__).resolve().parents[2] / "uv.lock"
        assert rt.lockfile_hash == sha256_hex(lock.read_bytes().decode())
        assert env.seed == req.seed and env.request_hash == req.content_hash()
        assert env.config_hash and env.simulator_version
        assert set(env.model_artifact_hashes) == {
            _dist("plr_h1", 1).source_artifact_hash,
            _dist("plr_a1", 1).source_artifact_hash,
        }


__all__ = ["T0", "LedgerError", "Routing"]
