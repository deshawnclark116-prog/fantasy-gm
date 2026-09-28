"""Foundation v0.1 acceptance criteria (ported to v0.1.1 APIs). One class per criterion."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from itertools import pairwise

import numpy as np
import pytest

from fantasy_gm.decisions.autonomy import Routing, route_decision
from fantasy_gm.decisions.execution import AttemptOutcome, ExecutionService
from fantasy_gm.decisions.ledger import DecisionLedger, ImmutableRecordError, LedgerError
from fantasy_gm.domain.autonomy import AutonomyMode, AutonomyPolicy
from fantasy_gm.domain.clock import ManualClock
from fantasy_gm.domain.decision import ActorKind, DecisionStatus, ObservedOutcome
from fantasy_gm.domain.identity import (
    EntityType,
    IdentityMappingEvent,
    MappingMethod,
    MappingStatus,
    ProviderRef,
)
from fantasy_gm.domain.ids import new_fantasy_team_id, new_game_id, new_player_id
from fantasy_gm.domain.league import (
    AllowedTier,
    RosterEntry,
    RosterRules,
    RosterSlot,
    ScoringRules,
    SlotKind,
    StatKey,
    ThresholdBonus,
    WaiverSettings,
    WaiverType,
)
from fantasy_gm.domain.nfl import (
    DepthChartSignal,
    DepthChartSource,
    DraftCapital,
    InjuryDesignation,
    InjuryReportType,
    InjuryStatus,
    Position,
    SeasonPhase,
    UsageMetric,
    UsageSnapshot,
)
from fantasy_gm.domain.seeds import SeedSpec
from fantasy_gm.domain.time import KnowledgeCutoff, KnowledgeMode, TimestampQuality
from fantasy_gm.domain.validation import ModelValidationRecord, ValidationState
from fantasy_gm.grading.grader import grade_observed
from fantasy_gm.leagues.roster_validation import validate_roster
from fantasy_gm.leagues.scoring import score_stat_line
from fantasy_gm.leagues.state import reconstruct_league_state
from fantasy_gm.models.registry import ModelRegistry
from fantasy_gm.organizational_intent.engine import compute_intent_snapshot
from fantasy_gm.organizational_intent.inputs import gather_intent_inputs
from fantasy_gm.organizational_intent.snapshot import (
    ComponentCategory,
    ConflictKind,
    IntentComponent,
)
from fantasy_gm.player_state.builder import build_player_state
from fantasy_gm.player_state.store import ObservationQuery, ObservationStore
from fantasy_gm.providers.identity import (
    IdentityConflictError,
    IdentityRegistry,
    UnresolvedIdentityError,
)
from fantasy_gm.providers.ingestion import map_injury, map_usage
from fantasy_gm.providers.records import ProviderInjuryRecord, ProviderUsageRecord
from fantasy_gm.simulation.distributions import (
    EmpiricalDistribution,
    PlayerWeekDistribution,
    TruncatedNormalDistribution,
)
from fantasy_gm.simulation.interfaces import MatchupRequest, WeeklyOutcomeRequest
from fantasy_gm.simulation.reference import (
    IndependentMatchupSimulator,
    IndependentWeeklyOutcomeSimulator,
)
from tests.factories import (
    EXACT,
    NOW,
    assignment,
    heuristic_artifact,
    league_settings,
    lineup_decision,
    live_run,
    make_league,
    make_player,
    obs_times,
    open_session,
    replay_run,
    source,
    status_event,
    te_premium_rules,
    team_id,
    ts,
    wr_usage,
)
from tests.fakes import LEAGUE_REF, READ_ONLY, WRITE_CAPS, FakePlatform, NullIdentity

pytestmark = pytest.mark.acceptance


# ============================================================================ 1
class TestArbitraryLeagueConfiguration:
    def test_te_premium_superflex_faab_league_scores_and_validates(
        self, store: ObservationStore, clock: ManualClock
    ) -> None:
        league = make_league()
        store.add_many(league_settings(league, ts(-30)))
        rules = reconstruct_league_state(open_session(store, clock, ts()), league).rules()
        te_line = {StatKey.REC: 6, StatKey.REC_YD: 70, StatKey.REC_TD: 1}
        te = score_stat_line(te_line, Position.TE, rules.scoring)
        wr = score_stat_line(te_line, Position.WR, rules.scoring)
        assert te.total == Decimal("6") * Decimal("1.5") + Decimal("7.0") + 6
        assert wr.total == Decimal("3.0") + Decimal("7.0") + 6

        qb, wr_p = make_player("Test QB", Position.QB), make_player("Test WR", Position.WR)
        elig = {qb.player_id: qb.positions, wr_p.player_id: wr_p.positions}
        ok = (
            RosterEntry(player_id=qb.player_id, slot_label="SUPERFLEX"),
            RosterEntry(player_id=wr_p.player_id, slot_label="FLEX"),
        )
        assert validate_roster(ok, rules.roster_rules, elig) == []
        bad = (RosterEntry(player_id=qb.player_id, slot_label="FLEX"),)
        assert [v.code for v in validate_roster(bad, rules.roster_rules, elig)] == ["ineligible"]

    def test_completely_different_configuration_is_representable(self) -> None:
        scoring = ScoringRules(
            name="non-PPR with bonuses and DST tiers",
            per_stat={
                StatKey.PASS_YD: Decimal("0.05"),
                StatKey.RUSH_YD: Decimal("0.1"),
                StatKey.RUSH_FIRST_DOWN: Decimal("0.5"),
                StatKey.DST_SACK: Decimal(1),
            },
            bonuses=(
                ThresholdBonus(
                    stat=StatKey.RUSH_YD,
                    threshold=Decimal(100),
                    points=Decimal(3),
                    positions=frozenset({Position.RB}),
                ),
            ),
            dst_points_allowed_tiers=(
                AllowedTier(min_allowed=0, max_allowed=0, points=Decimal(10)),
                AllowedTier(min_allowed=1, max_allowed=13, points=Decimal(4)),
                AllowedTier(min_allowed=14, max_allowed=None, points=Decimal(-1)),
            ),
        )
        rules = RosterRules(
            slots=(
                RosterSlot(
                    label="QB",
                    kind=SlotKind.STARTER,
                    count=2,
                    eligible_positions=frozenset({Position.QB}),
                ),
                RosterSlot(
                    label="W/R",
                    kind=SlotKind.STARTER,
                    count=4,
                    eligible_positions=frozenset({Position.WR, Position.RB}),
                ),
                RosterSlot(label="BENCH", kind=SlotKind.BENCH, count=10),
            )
        )
        assert rules.starter_count == 6
        rb = score_stat_line(
            {StatKey.RUSH_YD: 104, StatKey.RUSH_FIRST_DOWN: 6, StatKey.REC: 4},
            Position.RB,
            scoring,
        )
        assert rb.total == Decimal("10.4") + Decimal("3.0") + Decimal(3)
        dst = score_stat_line(
            {StatKey.DST_POINTS_ALLOWED: 13, StatKey.DST_SACK: 3}, Position.DST, scoring
        )
        assert dst.total == Decimal(7)

    def test_inconsistent_configuration_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="faab_budget"):
            WaiverSettings(waiver_type=WaiverType.FAAB)
        with pytest.raises(ValueError, match="contiguous"):
            ScoringRules(
                name="gap",
                per_stat={},
                dst_points_allowed_tiers=(
                    AllowedTier(min_allowed=0, max_allowed=6, points=Decimal(7)),
                    AllowedTier(min_allowed=10, max_allowed=None, points=Decimal(0)),
                ),
            )


# ============================================================================ 2
def _assert(
    internal: str, provider: str, external: str, entity: EntityType = EntityType.PLAYER
) -> IdentityMappingEvent:
    return IdentityMappingEvent(
        ref=ProviderRef(provider=provider, entity_type=entity, external_id=external),
        internal_id=internal,
        status=MappingStatus.VERIFIED,
        method=MappingMethod.CURATED_CROSSWALK,
        confidence=1.0,
        asserted_by="crosswalk:test@1",
    )


class TestProviderIdentityMapping:
    def test_one_player_many_providers(self, identity: IdentityRegistry) -> None:
        player = make_player("Test Tight End", Position.TE)
        pairs = (("sleeper", "S-0001"), ("espn", "E-0001"), ("yahoo", "Y-0001"))
        for provider, ext in pairs:
            identity.assert_mapping(_assert(player.player_id, provider, ext))
            identity.assert_mapping(_assert(player.player_id, provider, ext))  # idempotent
        for provider, ext in pairs:
            ref = ProviderRef(provider=provider, entity_type=EntityType.PLAYER, external_id=ext)
            res = identity.resolve(ref)
            assert res is not None and res.internal_id == player.player_id
            assert len(identity.history(ref)) == 1
        assert {r.ref.provider for r in identity.refs_for(player.player_id)} == {
            "sleeper",
            "espn",
            "yahoo",
        }
        assert player.player_id.startswith("plr_")

    def test_conflicts_are_refused(self, identity: IdentityRegistry) -> None:
        a, b = new_player_id(), new_player_id()
        identity.assert_mapping(_assert(a, "sleeper", "S-1"))
        with pytest.raises(IdentityConflictError, match="silent reassignment"):
            identity.assert_mapping(_assert(b, "sleeper", "S-1"))
        with pytest.raises(IdentityConflictError, match="VERIFIED"):
            identity.assert_mapping(_assert(a, "sleeper", "S-2"))

    def test_records_from_different_providers_land_on_same_player(
        self, identity: IdentityRegistry, clock: ManualClock
    ) -> None:
        player = make_player("Test Running Back", Position.RB)
        team, game = team_id(), new_game_id()
        identity.assert_mapping(_assert(player.player_id, "sleeper", "S-77"))
        identity.assert_mapping(_assert(player.player_id, "espn", "E-77"))
        identity.assert_mapping(_assert(team, "sleeper", "KC", EntityType.NFL_TEAM))
        identity.assert_mapping(_assert(game, "sleeper", "G1", EntityType.GAME))

        def ref(p: str, e: EntityType, x: str) -> ProviderRef:
            return ProviderRef(provider=p, entity_type=e, external_id=x)

        now = clock.now()
        usage = map_usage(
            ProviderUsageRecord(
                player_ref=ref("sleeper", EntityType.PLAYER, "S-77"),
                team_ref=ref("sleeper", EntityType.NFL_TEAM, "KC"),
                game_ref=ref("sleeper", EntityType.GAME, "G1"),
                season=2025,
                week=1,
                phase=SeasonPhase.REGULAR,
                metrics={UsageMetric.CARRIES: 14},
                observed_at=now,
                effective_at=now - timedelta(days=1),
                timestamp_quality=EXACT,
                raw_timestamp="2026-09-28T12:00:00Z",
                raw_timestamp_field="published",
            ),
            identity,
            ingested_at=now,
        )
        injury = map_injury(
            ProviderInjuryRecord(
                player_ref=ref("espn", EntityType.PLAYER, "E-77"),
                designation=InjuryDesignation.QUESTIONABLE,
                report_type=InjuryReportType.OFFICIAL_REPORT,
                observed_at=now,
                effective_at=now,
                timestamp_quality=EXACT,
            ),
            identity,
            ingested_at=now,
        )
        assert usage.player_id == injury.player_id == player.player_id
        assert len(usage.source.identity_basis) == 3  # player, team, game mappings recorded
        assert usage.source.raw_timestamp == "2026-09-28T12:00:00Z"
        with pytest.raises(UnresolvedIdentityError):
            map_injury(
                ProviderInjuryRecord(
                    player_ref=ref("yahoo", EntityType.PLAYER, "unknown"),
                    designation=InjuryDesignation.OUT,
                    report_type=InjuryReportType.NEWS,
                    observed_at=now,
                    effective_at=now,
                    timestamp_quality=EXACT,
                ),
                identity,
                ingested_at=now,
            )


# ============================================================================ 3
class TestHistoricalReconstruction:
    def test_stat_corrections_and_publication_times_are_respected(
        self, store: ObservationStore, clock: ManualClock
    ) -> None:
        player, team, game = make_player(), team_id(), new_game_id()
        kickoff = ts(days=7)
        original = wr_usage(player, team, 1, routes=30, game=game, effective=kickoff)
        corrected = UsageSnapshot(
            **{
                **original.model_dump(exclude={"observation_id", "observed_at", "source"}),
                "metrics": {UsageMetric.ROUTES: 34, UsageMetric.TEAM_DROPBACKS: 40},
            },
            observed_at=kickoff + timedelta(days=3),
            source=source(kickoff + timedelta(days=3)),
        )
        store.add_many([corrected, original])
        q = ObservationQuery(player_id=player.player_id)

        def routes_at(as_of_days: float) -> list[float]:
            known = open_session(store, clock, ts(days=as_of_days)).read(q)
            return [o.metrics[UsageMetric.ROUTES] for o in known if isinstance(o, UsageSnapshot)]

        assert routes_at(7) == []
        assert routes_at(8) == [30]
        assert routes_at(9.9) == [30]
        assert routes_at(10.1) == [34]

    def test_system_knowledge_vs_public_availability(
        self, store: ObservationStore, clock: ManualClock
    ) -> None:
        player = make_player()
        late = InjuryStatus(
            player_id=player.player_id,
            designation=InjuryDesignation.OUT,
            report_type=InjuryReportType.OFFICIAL_REPORT,
            **obs_times(ts(days=5), lag_hours=48),
        )
        store.add(late)
        q = ObservationQuery(player_id=player.player_id)
        strict = open_session(store, clock, ts(days=6))
        backfill = open_session(
            store, clock, ts(days=6), run=replay_run(KnowledgeMode.PUBLIC_AVAILABILITY)
        )
        assert strict.read(q) == []
        assert [o.observation_id for o in backfill.read(q)] == [late.observation_id]

    def test_player_state_rebuilt_as_of_cutoff(
        self, store: ObservationStore, clock: ManualClock
    ) -> None:
        player, team = make_player(), team_id()
        store.add(assignment(player, team, ts(days=-30)))
        for day, designation in ((2, InjuryDesignation.QUESTIONABLE), (4, InjuryDesignation.OUT)):
            store.add(
                InjuryStatus(
                    player_id=player.player_id,
                    designation=designation,
                    report_type=InjuryReportType.OFFICIAL_REPORT,
                    **obs_times(ts(days=day)),
                )
            )
        early = build_player_state(open_session(store, clock, ts(days=3)), player)
        late = build_player_state(open_session(store, clock, ts(days=5)), player)
        assert early.injury is not None
        assert early.injury.designation is InjuryDesignation.QUESTIONABLE
        assert late.injury is not None and late.injury.designation is InjuryDesignation.OUT
        before = build_player_state(open_session(store, clock, ts(days=1)), player)
        assert before.injury is None and any("injury" in g for g in before.data_gaps)


# ============================================================================ 4 & 5
def seed_prior_heavy_wr(store: ObservationStore) -> tuple:  # type: ignore[type-arg]
    """A first-round rookie WR listed as WR1, with a same-position veteran competitor."""
    player = make_player("Test Rookie WR", Position.WR)
    vet = make_player("Test Veteran WR", Position.WR)
    team = team_id()
    draft_day = ts(days=-130)
    store.add_many(
        [
            assignment(player, team, draft_day),
            assignment(vet, team, ts(days=-900)),
            DraftCapital(
                player_id=player.player_id,
                draft_year=2025,
                round=1,
                overall_pick=12,
                drafting_team_id=team,
                **obs_times(draft_day),
            ),
            DraftCapital(
                player_id=vet.player_id,
                draft_year=2021,
                round=4,
                overall_pick=120,
                drafting_team_id=team,
                **obs_times(ts(days=-1600)),
            ),
            DepthChartSignal(
                player_id=player.player_id,
                team_id=team,
                position=Position.WR,
                depth_rank=1,
                source_type=DepthChartSource.PROVIDER_CURATED,
                **obs_times(ts(days=-3)),
            ),
            wr_usage(
                player,
                team,
                week=2,
                routes=0,
                phase=SeasonPhase.PRESEASON,
                effective=ts(days=-14),
                extra={UsageMetric.FIRST_TEAM_SNAPS: 18, UsageMetric.TEAM_FIRST_TEAM_SNAPS: 20},
            ),
        ]
    )
    return player, vet, team, {player.player_id: player, vet.player_id: vet}


class TestOrganizationalIntentSnapshot:
    def test_transparent_component_level_snapshot(
        self, store: ObservationStore, clock: ManualClock
    ) -> None:
        player, _, team, directory = seed_prior_heavy_wr(store)
        session = open_session(store, clock, ts())
        inputs = gather_intent_inputs(session, directory, player.player_id, 2025)
        snap = compute_intent_snapshot(inputs, computed_at=ts(hours=1))

        assert {c.component for c in snap.components} == set(IntentComponent)
        assert snap.team_id == team
        draft = snap.component(IntentComponent.DRAFT_INVESTMENT)
        assert draft.score is not None and draft.score > 0.5
        assert draft.measurements["overall_pick"] == 12
        assert draft.provenance[0].observation_kind == "draft_capital"
        depth = snap.component(IntentComponent.DEPTH_CHART)
        assert depth.score == 1.0 and depth.provenance
        assert snap.component(IntentComponent.ROSTER_COMPETITION).score == 1.0
        pre = snap.component(IntentComponent.PRESEASON_DEPLOYMENT)
        assert pre.score == pytest.approx(0.9)
        contract = snap.component(IntentComponent.CONTRACT_INVESTMENT)
        assert contract.score is None and contract.confidence == 0 and contract.notes
        assert snap.evidence_balance.usage_share == 0
        assert sum(c.influence for c in snap.components) == pytest.approx(1.0)
        assert not any("intent_score" in f for f in type(snap).model_fields)
        # Every provenance record points at an observation the session recorded as evidence.
        manifest = session.seal()
        for c in snap.components:
            for p in c.provenance:
                assert f"observation:{p.observation_id}" in manifest.ids()
                assert p.observed_at <= ts()


class TestUsageOverridesPriors:
    def test_accumulating_usage_reduces_prior_influence(
        self, store: ObservationStore, clock: ManualClock
    ) -> None:
        player, _, team, directory = seed_prior_heavy_wr(store)
        # Real deployment contradicts the priors: 30% route participation every week.
        store.add_many(wr_usage(player, team, week=w, routes=12) for w in range(1, 11))
        snaps = []
        for weeks_played in (0, 1, 3, 6, 10):
            as_of = ts(days=7 * weeks_played + 1)
            inputs = gather_intent_inputs(
                open_session(store, clock, as_of), directory, player.player_id, 2025
            )
            snaps.append(compute_intent_snapshot(inputs, computed_at=as_of))

        usage_share = [s.evidence_balance.usage_share for s in snaps]
        draft_infl = [s.component(IntentComponent.DRAFT_INVESTMENT).influence for s in snaps]
        assert usage_share == sorted(usage_share) and usage_share[0] == 0
        assert usage_share[-1] > 0.7
        assert all(a > b for a, b in pairwise(draft_infl))
        prior_final = sum(
            c.influence for c in snaps[-1].components if c.category is ComponentCategory.PRIOR
        )
        assert snaps[-1].component(IntentComponent.ACTUAL_USAGE).influence > prior_final
        usage = snaps[-1].component(IntentComponent.ACTUAL_USAGE)
        assert usage.measurements["rec_route_participation"] == pytest.approx(0.30)
        assert [c.kind for c in snaps[-1].conflicts] == [ConflictKind.USAGE_BELOW_PRIORS]
        assert snaps[0].conflicts == ()

    def test_future_usage_never_reaches_an_earlier_snapshot(
        self, store: ObservationStore, clock: ManualClock
    ) -> None:
        player, _, team, directory = seed_prior_heavy_wr(store)

        def snap():  # type: ignore[no-untyped-def]
            inputs = gather_intent_inputs(
                open_session(store, clock, ts()), directory, player.player_id, 2025
            )
            return compute_intent_snapshot(inputs, ts())

        before = snap()
        store.add_many(wr_usage(player, team, week=w, routes=40) for w in range(1, 6))
        after = snap()
        assert before.components == after.components
        assert before.evidence_balance == after.evidence_balance


# ============================================================================ 6 & 7
class TestDecisionLedger:
    def test_recorded_decision_is_not_contaminated_by_outcomes(
        self, ledger: DecisionLedger, store: ObservationStore, clock: ManualClock
    ) -> None:
        decision = lineup_decision(open_session(store, clock, ts(days=6)))
        meta = ledger.record(decision, status_event(decision, DecisionStatus.RECORDED))
        before = ledger.get(decision.decision_id)
        clock.advance(timedelta(days=1))
        ledger.attach_outcome(
            ObservedOutcome(
                decision_id=decision.decision_id,
                known_at=decision.information_cutoff + timedelta(days=2),
                realized={"lineup_points": 97.5},
                observed_alternatives={decision.candidates[1].candidate_id: 121.0},
            )
        )
        after = ledger.get(decision.decision_id)
        assert after.decision.value == before.decision.value == decision
        assert after.decision_hash == meta.payload_hash == decision.content_hash()
        assert after.recorded_at == before.recorded_at == NOW
        assert len(after.outcomes) == 1 and before.outcomes == ()
        with pytest.raises(ImmutableRecordError):
            ledger.record(decision, status_event(decision, DecisionStatus.RECORDED))

    def test_outcome_cannot_predate_information_cutoff(
        self, ledger: DecisionLedger, store: ObservationStore, clock: ManualClock
    ) -> None:
        decision = lineup_decision(open_session(store, clock, ts(days=6)))
        ledger.record(decision, status_event(decision, DecisionStatus.RECORDED))
        with pytest.raises(LedgerError):
            ledger.attach_outcome(
                ObservedOutcome(
                    decision_id=decision.decision_id,
                    known_at=decision.information_cutoff,
                    realized={"lineup_points": 90.0},
                )
            )

    def test_outcome_graded_separately(
        self, ledger: DecisionLedger, store: ObservationStore, clock: ManualClock
    ) -> None:
        decision = lineup_decision(open_session(store, clock, ts(days=6)))
        ledger.record(decision, status_event(decision, DecisionStatus.RECORDED))
        outcome = ObservedOutcome(
            decision_id=decision.decision_id,
            known_at=decision.information_cutoff + timedelta(days=2),
            realized={"lineup_points": 110.0},
            observed_alternatives={decision.candidates[1].candidate_id: 118.0},
        )
        ledger.attach_outcome(outcome)
        grade = grade_observed(ledger.get(decision.decision_id), outcome, graded_at=NOW)
        ledger.attach_grade(grade)
        graded = ledger.get(decision.decision_id)
        assert graded.decision.value == decision
        assert [g.value for g in graded.grades] == [grade]
        assert grade.decision_hash == decision.content_hash()
        assert grade.pit == pytest.approx(0.5)
        assert grade.observed_regret == pytest.approx(8.0) and grade.observed_rank == 2


# ============================================================================ 8
def _dist(pid: str, mean: float) -> PlayerWeekDistribution:
    return PlayerWeekDistribution(
        player_id=pid,
        season=2025,
        week=3,
        p_active=0.9,
        conditional=TruncatedNormalDistribution(mean=mean, stdev=6.0, lower=-2.0),
        source_model="test_fixture",
    )


class TestDeterministicSimulation:
    def test_weekly_outcomes_reproducible(self) -> None:
        sim = IndependentWeeklyOutcomeSimulator()
        req = WeeklyOutcomeRequest(
            distributions=(_dist("plr_a", 14.0), _dist("plr_b", 9.0)),
            n_sims=5000,
            seed=SeedSpec(root_seed=42),
        )
        r1, r2 = sim.simulate(req), sim.simulate(req)
        for pid in ("plr_a", "plr_b"):
            np.testing.assert_array_equal(r1.samples[pid], r2.samples[pid])
        assert r1.run == r2.run
        other = sim.simulate(req.model_copy(update={"seed": SeedSpec(root_seed=43)}))
        assert not np.array_equal(other.samples["plr_a"], r1.samples["plr_a"])

    def test_streams_keyed_by_label_not_call_order(self) -> None:
        sim = IndependentWeeklyOutcomeSimulator()
        seed = SeedSpec(root_seed=7)
        solo = sim.simulate(
            WeeklyOutcomeRequest(distributions=(_dist("plr_a", 14.0),), n_sims=1000, seed=seed)
        )
        crowd = sim.simulate(
            WeeklyOutcomeRequest(
                distributions=(_dist("plr_z", 3.0), _dist("plr_a", 14.0)), n_sims=1000, seed=seed
            )
        )
        np.testing.assert_array_equal(solo.samples["plr_a"], crowd.samples["plr_a"])

    def test_matchup_reproducible(self) -> None:
        req = MatchupRequest(
            home_team_id=new_fantasy_team_id(),
            away_team_id=new_fantasy_team_id(),
            home_lineup=(_dist("plr_a", 14.0), _dist("plr_b", 9.0)),
            away_lineup=(
                PlayerWeekDistribution(
                    player_id="plr_c",
                    season=2025,
                    week=3,
                    p_active=1.0,
                    conditional=EmpiricalDistribution(samples=(4.0, 12.0, 25.0)),
                    source_model="test_fixture",
                ),
            ),
            n_sims=4000,
            seed=SeedSpec(root_seed=2025, path=("week3",)),
        )
        sim = IndependentMatchupSimulator()
        a, b = sim.simulate(req), sim.simulate(req)
        assert a == b
        assert a.p_home_win + a.p_away_win + a.p_tie == pytest.approx(1.0)


# ============================================================================ 9
def _validated_live_decision(
    store: ObservationStore,
    clock: ManualClock,
    models: ModelRegistry,
):  # type: ignore[no-untyped-def]
    league = make_league()
    artifact = heuristic_artifact("validated_lineup_engine")
    models.register_artifact(artifact)
    models.record_validation(
        ModelValidationRecord(
            artifact_hash=artifact.artifact_hash,
            decision_type="lineup",  # type: ignore[arg-type]
            regime="regular_season",
            state=ValidationState.VALIDATED,
            effective_at=clock.now(),
            approved_by="test",
        )
    )
    policy = AutonomyPolicy(league_id=league.league_id, default_mode=AutonomyMode.AUTONOMOUS)
    session = open_session(store, clock, clock.now(), run=live_run())
    decision = lineup_decision(
        session,
        league_id=league.league_id,
        team=league.managed_team_id,
        mode=AutonomyMode.AUTONOMOUS,
        artifact=artifact,
    )
    return league, policy, decision


class TestAutonomyRefusesWithoutCapability:
    def test_read_only_provider_routes_to_manual_notification(
        self, store: ObservationStore, clock: ManualClock, models: ModelRegistry
    ) -> None:
        _, policy, decision = _validated_live_decision(store, clock, models)
        routing = route_decision(decision, policy, READ_ONLY, models, clock.now())
        assert routing.routing is Routing.NOTIFY and routing.manual_action_required
        assert any("lineup_write" in r for r in routing.reasons)

    async def test_execution_refused_when_capability_missing(
        self,
        ledger: DecisionLedger,
        store: ObservationStore,
        clock: ManualClock,
        models: ModelRegistry,
    ) -> None:
        _, policy, decision = _validated_live_decision(store, clock, models)
        routing = route_decision(decision, policy, WRITE_CAPS, models, clock.now())
        assert routing.routing is Routing.EXECUTE
        ledger.record(
            decision, status_event(decision, routing.initial_status, actor=ActorKind.POLICY)
        )
        platform = FakePlatform(clock, capabilities=READ_ONLY)  # write access revoked
        service = ExecutionService(
            ledger, platform, NullIdentity(), models, clock, execution_enabled=True, worker_id="w1"
        )
        attempt = await service.execute(decision.decision_id, policy, LEAGUE_REF)
        assert attempt.outcome is AttemptOutcome.REFUSED
        assert any("lacks required capability lineup_write" in r for r in attempt.reasons)
        assert platform.execute_calls == 0
        assert ledger.get(decision.decision_id).current_status is DecisionStatus.EXECUTION_BLOCKED

    async def test_execution_proceeds_with_capability(
        self,
        ledger: DecisionLedger,
        store: ObservationStore,
        clock: ManualClock,
        models: ModelRegistry,
    ) -> None:
        _, policy, decision = _validated_live_decision(store, clock, models)
        routing = route_decision(decision, policy, WRITE_CAPS, models, clock.now())
        ledger.record(
            decision, status_event(decision, routing.initial_status, actor=ActorKind.POLICY)
        )
        platform = FakePlatform(clock)
        service = ExecutionService(
            ledger, platform, NullIdentity(), models, clock, execution_enabled=True, worker_id="w1"
        )
        attempt = await service.execute(decision.decision_id, policy, LEAGUE_REF)
        assert attempt.outcome is AttemptOutcome.EXECUTED and platform.executed == 1
        assert ledger.get(decision.decision_id).current_status is DecisionStatus.EXECUTED


__all__ = ["KnowledgeCutoff", "TimestampQuality", "te_premium_rules"]
