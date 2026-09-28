"""Foundation v0.1 acceptance criteria. Each test class maps to one numbered criterion."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from itertools import pairwise

import numpy as np
import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DatabaseError

from fantasy_gm.decisions.autonomy import Routing, route_decision
from fantasy_gm.decisions.execution import ExecutionService
from fantasy_gm.decisions.ledger import DecisionLedger, ImmutableRecordError, LedgerError
from fantasy_gm.domain.autonomy import AutonomyMode, AutonomyPolicy
from fantasy_gm.domain.capabilities import ProviderCapability
from fantasy_gm.domain.decision import (
    ActorKind,
    DecisionOutcome,
    DecisionStatus,
)
from fantasy_gm.domain.identity import (
    EntityType,
    MappingMethod,
    ProviderIdMapping,
    ProviderRef,
)
from fantasy_gm.domain.ids import new_fantasy_team_id, new_game_id, new_player_id
from fantasy_gm.domain.league import (
    AllowedTier,
    DraftSettings,
    DraftType,
    League,
    LineupLock,
    PlayoffSettings,
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
from fantasy_gm.domain.time import KnowledgeCutoff, KnowledgeMode
from fantasy_gm.grading.grader import grade_decision
from fantasy_gm.leagues.roster_validation import validate_roster
from fantasy_gm.leagues.scoring import score_stat_line
from fantasy_gm.organizational_intent.engine import compute_intent_snapshot
from fantasy_gm.organizational_intent.inputs import gather_intent_inputs
from fantasy_gm.organizational_intent.snapshot import (
    ComponentCategory,
    ConflictKind,
    IntentComponent,
)
from fantasy_gm.persistence.identity import SqlIdentityRegistry
from fantasy_gm.player_state.builder import build_player_state
from fantasy_gm.player_state.store import ObservationQuery, ObservationStore
from fantasy_gm.providers.identity import (
    IdentityConflictError,
    IdentityRegistry,
    InMemoryIdentityRegistry,
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
    assignment,
    lineup_decision,
    make_league,
    make_player,
    obs_times,
    status_event,
    team_id,
    ts,
    wr_usage,
)
from tests.fakes import FakeTransactionProvider, NullIdentity

pytestmark = pytest.mark.acceptance


# ============================================================================ 1
class TestArbitraryLeagueConfiguration:
    def test_te_premium_superflex_faab_league_scores_and_validates(self) -> None:
        league = make_league()
        te_line = {StatKey.REC: 6, StatKey.REC_YD: 70, StatKey.REC_TD: 1}
        te = score_stat_line(te_line, Position.TE, league.scoring)
        wr = score_stat_line(te_line, Position.WR, league.scoring)
        # TE premium: 1.5/rec vs 0.5/rec for the same stat line.
        assert te.total == Decimal("6") * Decimal("1.5") + Decimal("7.0") + 6
        assert wr.total == Decimal("3.0") + Decimal("7.0") + 6
        assert {ln.source for ln in te.lines} == {"rec", "rec_yd", "rec_td"}

        qb = make_player("Test QB", Position.QB)
        wr_p = make_player("Test WR", Position.WR)
        elig = {qb.player_id: qb.positions, wr_p.player_id: wr_p.positions}
        ok = (
            RosterEntry(player_id=qb.player_id, slot_label="SUPERFLEX"),
            RosterEntry(player_id=wr_p.player_id, slot_label="FLEX"),
        )
        assert validate_roster(ok, league.roster_rules, elig) == []
        bad = (RosterEntry(player_id=qb.player_id, slot_label="FLEX"),)
        assert [v.code for v in validate_roster(bad, league.roster_rules, elig)] == ["ineligible"]

    def test_completely_different_configuration_is_representable(self) -> None:
        scoring = ScoringRules(
            name="non-PPR with bonuses and DST tiers",
            per_stat={
                StatKey.PASS_YD: Decimal("0.05"),
                StatKey.PASS_TD: Decimal(4),
                StatKey.RUSH_YD: Decimal("0.1"),
                StatKey.REC_YD: Decimal("0.1"),
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
                RosterSlot(
                    label="D",
                    kind=SlotKind.STARTER,
                    count=1,
                    eligible_positions=frozenset({Position.DST}),
                ),
                RosterSlot(label="BENCH", kind=SlotKind.BENCH, count=10),
            )
        )
        league = League(
            league_id=make_league().league_id,
            name="Two-QB rolling-waiver linear league",
            season=2025,
            num_teams=10,
            scoring=scoring,
            roster_rules=rules,
            draft=DraftSettings(draft_type=DraftType.LINEAR, rounds=17),
            waivers=WaiverSettings(waiver_type=WaiverType.ROLLING),
            playoffs=PlayoffSettings(playoff_teams=4, first_week=15, last_week=16),
            lineup_lock=LineupLock.WEEKLY_FIRST_GAME,
        )
        assert league.roster_rules.starter_count == 7
        rb = score_stat_line(
            {StatKey.RUSH_YD: 104, StatKey.RUSH_FIRST_DOWN: 6, StatKey.REC: 4},
            Position.RB,
            league.scoring,
        )
        assert rb.total == Decimal("10.4") + Decimal("3.0") + Decimal(3)  # no PPR
        dst = score_stat_line(
            {StatKey.DST_POINTS_ALLOWED: 13, StatKey.DST_SACK: 3}, Position.DST, league.scoring
        )
        assert dst.total == Decimal(4) + Decimal(3)

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
def _mapping(internal: str, provider: str, external: str) -> ProviderIdMapping:
    return ProviderIdMapping(
        internal_id=internal,
        ref=ProviderRef(provider=provider, entity_type=EntityType.PLAYER, external_id=external),
        method=MappingMethod.CURATED_CROSSWALK,
        confidence=1.0,
        observed_at=ts(),
    )


@pytest.fixture(params=["memory", "sql"])
def registry(request: pytest.FixtureRequest, engine: Engine) -> IdentityRegistry:
    return InMemoryIdentityRegistry() if request.param == "memory" else SqlIdentityRegistry(engine)


class TestProviderIdentityMapping:
    def test_one_player_many_providers(self, registry: IdentityRegistry) -> None:
        player = make_player("Test Tight End", Position.TE)
        for provider, ext in (("sleeper", "S-0001"), ("espn", "E-0001"), ("yahoo", "Y-0001")):
            registry.link(_mapping(player.player_id, provider, ext))
            registry.link(_mapping(player.player_id, provider, ext))  # idempotent
        for provider, ext in (("sleeper", "S-0001"), ("espn", "E-0001"), ("yahoo", "Y-0001")):
            ref = ProviderRef(provider=provider, entity_type=EntityType.PLAYER, external_id=ext)
            assert registry.resolve(ref) == player.player_id
        assert {m.ref.provider for m in registry.mappings_for(player.player_id)} == {
            "sleeper",
            "espn",
            "yahoo",
        }
        # Internal IDs are never provider IDs.
        assert player.player_id.startswith("plr_")

    def test_conflicts_are_refused(self, registry: IdentityRegistry) -> None:
        a, b = new_player_id(), new_player_id()
        registry.link(_mapping(a, "sleeper", "S-1"))
        with pytest.raises(IdentityConflictError):
            registry.link(_mapping(b, "sleeper", "S-1"))  # same ref, different player
        with pytest.raises(IdentityConflictError):
            registry.link(_mapping(a, "sleeper", "S-2"))  # second sleeper id for same player

    def test_records_from_different_providers_land_on_same_player(self) -> None:
        player = make_player("Test Running Back", Position.RB)
        team, game = team_id(), new_game_id()
        reg = InMemoryIdentityRegistry(
            [
                _mapping(player.player_id, "sleeper", "S-77"),
                _mapping(player.player_id, "espn", "E-77"),
            ]
        )
        for provider, entity, internal, ext in (
            ("sleeper", EntityType.NFL_TEAM, team, "KC"),
            ("sleeper", EntityType.GAME, game, "G1"),
        ):
            reg.link(
                ProviderIdMapping(
                    internal_id=internal,
                    ref=ProviderRef(provider=provider, entity_type=entity, external_id=ext),
                    method=MappingMethod.PROVIDER_CROSSWALK,
                    confidence=1.0,
                    observed_at=ts(),
                )
            )

        def ref(p: str, e: EntityType, x: str) -> ProviderRef:
            return ProviderRef(provider=p, entity_type=e, external_id=x)

        usage = map_usage(
            ProviderUsageRecord(
                player_ref=ref("sleeper", EntityType.PLAYER, "S-77"),
                team_ref=ref("sleeper", EntityType.NFL_TEAM, "KC"),
                game_ref=ref("sleeper", EntityType.GAME, "G1"),
                season=2025,
                week=1,
                phase=SeasonPhase.REGULAR,
                metrics={UsageMetric.CARRIES: 14},
                observed_at=ts(1),
                effective_at=ts(),
            ),
            reg,
            ingested_at=ts(1),
        )
        injury = map_injury(
            ProviderInjuryRecord(
                player_ref=ref("espn", EntityType.PLAYER, "E-77"),
                designation=InjuryDesignation.QUESTIONABLE,
                report_type=InjuryReportType.OFFICIAL_REPORT,
                observed_at=ts(3),
                effective_at=ts(3),
            ),
            reg,
            ingested_at=ts(3),
        )
        assert usage.player_id == injury.player_id == player.player_id
        with pytest.raises(UnresolvedIdentityError):
            map_injury(
                injury_record := ProviderInjuryRecord(
                    player_ref=ref("yahoo", EntityType.PLAYER, "unknown"),
                    designation=InjuryDesignation.OUT,
                    report_type=InjuryReportType.NEWS,
                    observed_at=ts(3),
                    effective_at=ts(3),
                ),
                reg,
                ingested_at=ts(3),
            )
        assert injury_record.player_ref.provider == "yahoo"


# ============================================================================ 3
class TestHistoricalReconstruction:
    def test_stat_corrections_and_publication_times_are_respected(
        self, store: ObservationStore
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
            source=obs_times(kickoff + timedelta(days=3))["source"],
        )
        store.add_many([corrected, original])  # insertion order must not matter
        q = ObservationQuery(player_id=player.player_id)

        def routes_at(as_of_days: float) -> list[float]:
            known = store.known_as_of(KnowledgeCutoff(as_of=ts(days=as_of_days)), q)
            return [o.metrics[UsageMetric.ROUTES] for o in known if isinstance(o, UsageSnapshot)]

        assert routes_at(7) == []  # game kicked off but stats not yet published
        assert routes_at(8) == [30]  # original line
        assert routes_at(9.9) == [30]  # correction not yet public
        assert routes_at(10.1) == [34]  # correction supersedes

    def test_system_knowledge_vs_public_availability(self, store: ObservationStore) -> None:
        player = make_player()
        late_ingest = InjuryStatus(
            player_id=player.player_id,
            designation=InjuryDesignation.OUT,
            report_type=InjuryReportType.OFFICIAL_REPORT,
            **obs_times(ts(days=5), lag_hours=48),  # published day 5, ingested day 7
        )
        store.add(late_ingest)
        q = ObservationQuery(player_id=player.player_id)
        strict = KnowledgeCutoff(as_of=ts(days=6))
        backfill = KnowledgeCutoff(as_of=ts(days=6), mode=KnowledgeMode.PUBLIC_AVAILABILITY)
        assert store.known_as_of(strict, q) == []
        assert [o.observation_id for o in store.known_as_of(backfill, q)] == [
            late_ingest.observation_id
        ]

    def test_player_state_rebuilt_as_of_cutoff(self, store: ObservationStore) -> None:
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
        early = build_player_state(store, player, KnowledgeCutoff(as_of=ts(days=3)))
        late = build_player_state(store, player, KnowledgeCutoff(as_of=ts(days=5)))
        assert (
            early.injury is not None and early.injury.designation is InjuryDesignation.QUESTIONABLE
        )
        assert late.injury is not None and late.injury.designation is InjuryDesignation.OUT
        before = build_player_state(store, player, KnowledgeCutoff(as_of=ts(days=1)))
        assert before.injury is None
        assert any("injury" in gap for gap in before.data_gaps)


# ============================================================================ 4 & 5
def _seed_prior_heavy_wr(store: ObservationStore) -> tuple:
    """A first-round WR listed as WR1, with a same-position veteran competitor."""
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
    directory = {player.player_id: player, vet.player_id: vet}
    return player, vet, team, directory


class TestOrganizationalIntentSnapshot:
    def test_transparent_component_level_snapshot(self, store: ObservationStore) -> None:
        player, _, team, directory = _seed_prior_heavy_wr(store)
        cutoff = KnowledgeCutoff(as_of=ts(days=0))
        inputs = gather_intent_inputs(store, directory, player.player_id, 2025, cutoff)
        snap = compute_intent_snapshot(inputs, computed_at=ts(days=0, hours=1))

        assert {c.component for c in snap.components} == set(IntentComponent)
        assert snap.team_id == team
        draft = snap.component(IntentComponent.DRAFT_INVESTMENT)
        assert draft.score is not None and draft.score > 0.5
        assert draft.measurements["overall_pick"] == 12
        assert draft.provenance and draft.provenance[0].observation_kind == "draft_capital"
        assert draft.method == "log_pick_rank_v0"
        depth = snap.component(IntentComponent.DEPTH_CHART)
        assert depth.score == 1.0 and depth.provenance
        comp = snap.component(IntentComponent.ROSTER_COMPETITION)
        assert comp.score == 1.0 and comp.measurements["room_size"] == 2
        pre = snap.component(IntentComponent.PRESEASON_DEPLOYMENT)
        assert pre.score == pytest.approx(0.9)

        # Missing evidence is explicit, never silently zero.
        contract = snap.component(IntentComponent.CONTRACT_INVESTMENT)
        assert contract.score is None and contract.confidence == 0 and contract.notes
        assert IntentComponent.CONTRACT_INVESTMENT in snap.missing_components
        # No regular-season usage yet -> priors carry all influence.
        assert snap.evidence_balance.usage_share == 0
        assert sum(c.influence for c in snap.components) == pytest.approx(1.0)
        # There is no black-box aggregate intent score.
        assert not any("intent_score" in f for f in type(snap).model_fields)
        # Every provenance record points at a real, knowable observation.
        known_ids = {o.observation_id for o in inputs.all_observations()}
        for c in snap.components:
            for p in c.provenance:
                assert p.observation_id in known_ids
                assert p.observed_at <= cutoff.as_of


class TestUsageOverridesPriors:
    def test_accumulating_usage_reduces_prior_influence(self, store: ObservationStore) -> None:
        player, _, team, directory = _seed_prior_heavy_wr(store)
        # Real deployment contradicts the priors: ~35% route participation every week.
        store.add_many(wr_usage(player, team, week=w, routes=14) for w in range(1, 11))

        snaps = []
        for weeks_played in (0, 1, 3, 6, 10):
            as_of = ts(days=7 * weeks_played + 1)
            inputs = gather_intent_inputs(
                store, directory, player.player_id, 2025, KnowledgeCutoff(as_of=as_of)
            )
            snaps.append(compute_intent_snapshot(inputs, computed_at=as_of))

        usage_share = [s.evidence_balance.usage_share for s in snaps]
        draft_infl = [s.component(IntentComponent.DRAFT_INVESTMENT).influence for s in snaps]
        assert usage_share == sorted(usage_share) and usage_share[0] == 0
        assert usage_share[-1] > 0.7
        assert all(a > b for a, b in pairwise(draft_infl))
        prior_infl_final = sum(
            c.influence for c in snaps[-1].components if c.category is ComponentCategory.PRIOR
        )
        assert snaps[-1].component(IntentComponent.ACTUAL_USAGE).influence > prior_infl_final

        final = snaps[-1]
        usage = final.component(IntentComponent.ACTUAL_USAGE)
        assert usage.measurements["weighted_share"] == pytest.approx(0.35)
        assert [c.kind for c in final.conflicts] == [ConflictKind.USAGE_BELOW_PRIORS]
        assert snaps[0].conflicts == ()

    def test_future_usage_never_reaches_an_earlier_snapshot(self, store: ObservationStore) -> None:
        player, _, team, directory = _seed_prior_heavy_wr(store)
        cutoff = KnowledgeCutoff(as_of=ts(days=0))
        before = compute_intent_snapshot(
            gather_intent_inputs(store, directory, player.player_id, 2025, cutoff), ts(days=0)
        )
        store.add_many(wr_usage(player, team, week=w, routes=40) for w in range(1, 6))
        after = compute_intent_snapshot(
            gather_intent_inputs(store, directory, player.player_id, 2025, cutoff), ts(days=0)
        )
        assert before.components == after.components
        assert before.evidence_balance == after.evidence_balance


# ============================================================================ 6 & 7
class TestDecisionLedger:
    def test_recorded_decision_is_not_contaminated_by_outcomes(
        self, ledger: DecisionLedger
    ) -> None:
        decision = lineup_decision()
        digest = ledger.record(decision, status_event(decision, DecisionStatus.RECOMMENDED))
        before = ledger.get(decision.decision_id)

        outcome = DecisionOutcome(
            decision_id=decision.decision_id,
            recorded_at=decision.information_cutoff + timedelta(days=2),
            realized={"lineup_points": 97.5},
            candidate_realized={decision.candidates[1].candidate_id: 121.0},
        )
        ledger.attach_outcome(outcome)
        after = ledger.get(decision.decision_id)

        assert after.decision == before.decision == decision
        assert after.decision_hash == digest == decision.content_hash()
        assert after.decision.canonical_json() == decision.canonical_json()
        assert len(after.outcomes) == 1 and before.outcomes == ()

        with pytest.raises(ImmutableRecordError):
            ledger.record(decision, status_event(decision, DecisionStatus.RECOMMENDED))

    def test_leaky_evidence_cannot_be_recorded(self) -> None:
        with pytest.raises(ValueError, match="leakage"):
            lineup_decision(cutoff=ts(days=6), evidence_observed=ts(days=6, hours=1))

    def test_outcome_cannot_predate_information_cutoff(self, ledger: DecisionLedger) -> None:
        decision = lineup_decision()
        ledger.record(decision, status_event(decision, DecisionStatus.RECOMMENDED))
        with pytest.raises(LedgerError):
            ledger.attach_outcome(
                DecisionOutcome(
                    decision_id=decision.decision_id,
                    recorded_at=decision.information_cutoff,
                    realized={"lineup_points": 90.0},
                )
            )

    def test_database_rejects_in_place_mutation(self, engine: Engine) -> None:
        from fantasy_gm.persistence.ledger import SqlDecisionLedger

        ledger = SqlDecisionLedger(engine)
        decision = lineup_decision()
        ledger.record(decision, status_event(decision, DecisionStatus.RECOMMENDED))
        with pytest.raises(DatabaseError, match="append-only"), engine.begin() as conn:
            conn.execute(text("UPDATE decisions SET payload = '{}'"))
        with pytest.raises(DatabaseError, match="append-only"), engine.begin() as conn:
            conn.execute(text("DELETE FROM decision_status_events"))
        assert ledger.get(decision.decision_id).decision == decision

    def test_outcome_graded_separately(self, ledger: DecisionLedger) -> None:
        decision = lineup_decision()
        ledger.record(decision, status_event(decision, DecisionStatus.RECOMMENDED))
        alt = decision.candidates[1].candidate_id
        outcome = DecisionOutcome(
            decision_id=decision.decision_id,
            recorded_at=decision.information_cutoff + timedelta(days=2),
            realized={"lineup_points": 110.0},
            candidate_realized={alt: 118.0},
        )
        ledger.attach_outcome(outcome)
        entry = ledger.get(decision.decision_id)
        grade = grade_decision(entry, outcome, graded_at=outcome.recorded_at + timedelta(hours=1))
        ledger.attach_grade(grade)

        graded = ledger.get(decision.decision_id)
        assert graded.decision == decision  # untouched
        assert graded.grades == (grade,)
        assert grade.decision_hash == decision.content_hash()
        assert grade.predicted_mean == 110.0 and grade.error == 0.0
        assert grade.pit == pytest.approx(0.5)
        assert grade.regret == pytest.approx(8.0) and grade.selected_rank == 2


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
        dists = (_dist("plr_a", 14.0), _dist("plr_b", 9.0))
        req = WeeklyOutcomeRequest(distributions=dists, n_sims=5000, seed=SeedSpec(root_seed=42))
        r1, r2 = sim.simulate(req), sim.simulate(req)
        for pid in ("plr_a", "plr_b"):
            np.testing.assert_array_equal(r1.samples[pid], r2.samples[pid])
        assert r1.run == r2.run and r1.run.seed == SeedSpec(root_seed=42)
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
        sim = IndependentMatchupSimulator()
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
        a, b = sim.simulate(req), sim.simulate(req)
        assert a == b
        assert a.p_home_win + a.p_away_win + a.p_tie == pytest.approx(1.0)


# ============================================================================ 9
class TestAutonomyRefusesWithoutCapability:
    def _setup(self, ledger: DecisionLedger, caps: frozenset[ProviderCapability]):
        league = make_league()
        policy = AutonomyPolicy(league_id=league.league_id, default_mode=AutonomyMode.AUTONOMOUS)
        decision = lineup_decision(
            league_id=league.league_id,
            team=league.managed_team_id,
            mode=AutonomyMode.AUTONOMOUS,
        )
        return league, policy, decision

    def test_read_only_provider_routes_to_manual_notification(self, ledger: DecisionLedger) -> None:
        read_only = frozenset({ProviderCapability.READ})
        league, policy, decision = self._setup(ledger, read_only)
        routing = route_decision(decision, policy, read_only, league)
        assert routing.routing is Routing.NOTIFY
        assert routing.manual_action_required
        assert any("lineup_write" in r for r in routing.reasons)

    def test_execution_refused_when_capability_missing(self, ledger: DecisionLedger) -> None:
        caps_at_decision = frozenset({ProviderCapability.READ, ProviderCapability.LINEUP_WRITE})
        league, policy, decision = self._setup(ledger, caps_at_decision)
        routing = route_decision(decision, policy, caps_at_decision, league)
        assert routing.routing is Routing.EXECUTE
        ledger.record(
            decision, status_event(decision, routing.initial_status, actor=ActorKind.POLICY)
        )

        # The platform's write access is later revoked (e.g. token scope reduced).
        provider = FakeTransactionProvider(frozenset({ProviderCapability.READ}), ts(days=6))
        service = ExecutionService(ledger, provider, NullIdentity(), execution_enabled=True)
        league_ref = ProviderRef(
            provider="fake_platform", entity_type=EntityType.LEAGUE, external_id="L1"
        )
        attempt = service.execute(
            decision.decision_id, policy, league, league_ref, now=decision.created_at
        )

        assert attempt.refused and not attempt.executed
        assert any("lacks required capability lineup_write" in r for r in attempt.reasons)
        assert provider.calls == []  # provider write was never invoked
        entry = ledger.get(decision.decision_id)
        assert entry.current_status is DecisionStatus.EXECUTION_BLOCKED

    def test_execution_proceeds_with_capability(self, ledger: DecisionLedger) -> None:
        caps = frozenset({ProviderCapability.READ, ProviderCapability.LINEUP_WRITE})
        league, policy, decision = self._setup(ledger, caps)
        routing = route_decision(decision, policy, caps, league)
        ledger.record(
            decision, status_event(decision, routing.initial_status, actor=ActorKind.POLICY)
        )
        provider = FakeTransactionProvider(caps, decision.created_at)
        service = ExecutionService(ledger, provider, NullIdentity(), execution_enabled=True)
        league_ref = ProviderRef(
            provider="fake_platform", entity_type=EntityType.LEAGUE, external_id="L1"
        )
        attempt = service.execute(
            decision.decision_id, policy, league, league_ref, now=decision.created_at
        )
        assert attempt.executed and len(provider.calls) == 1
        assert ledger.get(decision.decision_id).current_status is DecisionStatus.EXECUTED
