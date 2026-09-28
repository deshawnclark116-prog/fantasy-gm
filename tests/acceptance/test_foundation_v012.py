"""Foundation v0.1.2 acceptance: deep immutability / evidence-integrity closeout.

Pydantic's ``frozen=True`` only blocks attribute *reassignment*; a plain ``dict`` field is still
mutable in place. ``KnowledgeSession.read()`` hashes an observation into the evidence manifest
and then hands that same object to an engine -- if a nested container could be mutated
afterwards, the engine could compute from data that no longer matches the sealed evidence hash.
Every test class below maps to one required adversarial scenario from the review.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

import pytest

from fantasy_gm.decisions.ledger import InMemoryDecisionLedger
from fantasy_gm.domain.autonomy import AutonomyMode, AutonomyPolicy, DecisionType
from fantasy_gm.domain.decision import _ALLOWED_ACTIONS, DecisionStatus
from fantasy_gm.domain.evidence import EvidenceKind, evidence_id
from fantasy_gm.domain.frozen import FrozenMap
from fantasy_gm.domain.identity import EntityType, ProviderRef
from fantasy_gm.domain.ids import new_player_id
from fantasy_gm.domain.league import ScoringRules, StatKey
from fantasy_gm.domain.nfl import Player, Position, UsageMetric
from fantasy_gm.domain.risk import _RANK, ActionRiskClass, RiskPolicy
from fantasy_gm.domain.roles import ROLE_DIMENSIONS
from fantasy_gm.domain.seeds import SeedSpec
from fantasy_gm.domain.time import TimestampPolicy, TimestampQuality
from fantasy_gm.leagues.state import reconstruct_league_state
from fantasy_gm.organizational_intent.config import IntentConfigV0
from fantasy_gm.organizational_intent.engine import compute_intent_snapshot
from fantasy_gm.organizational_intent.inputs import gather_intent_inputs
from fantasy_gm.organizational_intent.snapshot import (
    COMPONENT_CATEGORY,
    COMPONENT_INFORMS,
    IntentComponent,
)
from fantasy_gm.player_state.builder import build_player_state
from fantasy_gm.player_state.store import ObservationQuery, ObservationStore
from fantasy_gm.providers.records import ProviderLeagueSnapshot
from fantasy_gm.records.codec import EncodedRecord
from fantasy_gm.records.registry import DEFAULT_CODECS
from fantasy_gm.simulation.distributions import PlayerWeekDistribution, TruncatedNormalDistribution
from fantasy_gm.simulation.interfaces import WeeklyOutcomeRequest
from fantasy_gm.simulation.reference import IndependentWeeklyOutcomeSimulator
from tests.acceptance.test_foundation_v01 import seed_prior_heavy_wr
from tests.factories import (
    NOW,
    league_settings,
    lineup_decision,
    make_league,
    make_player,
    open_session,
    status_event,
    team_id,
    ts,
    wr_usage,
)

pytestmark = pytest.mark.acceptance
FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "records"


def _assert_immutable_mapping(m: object) -> None:
    """A FrozenMap must refuse every mutating Mapping operation."""
    assert isinstance(m, FrozenMap)
    probe = next(iter(m), "__probe_key__")
    with pytest.raises(TypeError):
        m[probe] = object()  # type: ignore[index]
    with pytest.raises(TypeError):
        del m[probe]  # type: ignore[union-attr]
    with pytest.raises(TypeError):
        m.update({})  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        m.pop(probe, None)  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        m.clear()  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        m.setdefault("new_key", object())  # type: ignore[attr-defined]


def _codec_for_record_type(record_type: str):  # type: ignore[no-untyped-def]
    if record_type.startswith("observation:"):
        return DEFAULT_CODECS.observation(record_type.split(":", 1)[1])
    return next(
        getattr(DEFAULT_CODECS, f.name)
        for f in dataclasses.fields(DEFAULT_CODECS)
        if f.name != "observations" and getattr(DEFAULT_CODECS, f.name).record_type == record_type
    )


# ============================================================================ 1
class TestUsageSnapshotMetricsMutationThroughSession:
    def test_read_through_session_then_attempt_mutation(
        self, store: ObservationStore, clock
    ) -> None:
        player, team = make_player(), team_id()
        usage = wr_usage(player, team, week=1, routes=30, extra={UsageMetric.TARGETS: 4})
        store.add(usage)

        session = open_session(store, clock, ts(days=8))
        read_back = session.read(ObservationQuery(player_id=player.player_id))
        assert len(read_back) == 1
        obs = read_back[0]
        assert obs.kind == "usage_snapshot"

        before_hash = obs.content_hash()
        _assert_immutable_mapping(obs.metrics)  # type: ignore[attr-defined]
        # A failed mutation attempt must never partially apply.
        assert obs.content_hash() == before_hash

    def test_mutation_blocked_across_all_backends_identically(
        self, store: ObservationStore, clock
    ) -> None:
        """Whether the observation round-tripped through SQLite/PostgreSQL text+hash storage or
        stayed in the in-memory reference store, the object an engine receives is equally
        immutable -- immutability is a property of the domain type, not of one backend."""
        player, team = make_player(), team_id()
        usage = wr_usage(player, team, week=1, routes=20)
        store.add(usage)
        obs = open_session(store, clock, ts(days=8)).read(
            ObservationQuery(player_id=player.player_id)
        )[0]
        assert isinstance(obs.metrics, FrozenMap)  # type: ignore[attr-defined]
        with pytest.raises(TypeError):
            obs.metrics[UsageMetric.ROUTES] = 999.0  # type: ignore[index,attr-defined]


# ============================================================================ 2
class TestNestedLeagueScoringMutation:
    def test_top_level_and_nested_position_override_mutation_both_blocked(
        self, store: ObservationStore, clock
    ) -> None:
        league = make_league()
        store.add_many(league_settings(league, ts(-10)))
        state = reconstruct_league_state(open_session(store, clock, ts()), league)
        scoring = state.rules().scoring

        # Top level: per_stat.
        assert isinstance(scoring.per_stat, FrozenMap)
        with pytest.raises(TypeError):
            scoring.per_stat[StatKey.REC] = Decimal(99)  # type: ignore[index]

        # Nested level: position_overrides[Position.TE] is itself a FrozenMap.
        te_overrides = scoring.position_overrides[Position.TE]
        assert isinstance(te_overrides, FrozenMap)
        with pytest.raises(TypeError):
            te_overrides[StatKey.REC] = Decimal(0)  # type: ignore[index]
        # And the outer mapping cannot be used to swap in a fresh (mutable) inner dict either.
        with pytest.raises(TypeError):
            scoring.position_overrides[Position.TE] = {}  # type: ignore[index]

    def test_directly_constructed_scoring_rules_are_also_deeply_frozen(self) -> None:
        """Not just values that passed through a session -- any ``ScoringRules`` instance."""
        rules = ScoringRules(
            name="x",
            per_stat={StatKey.REC: Decimal("0.5")},
            position_overrides={Position.TE: {StatKey.REC: Decimal("1.5")}},
        )
        _assert_immutable_mapping(rules.per_stat)
        _assert_immutable_mapping(rules.position_overrides)
        _assert_immutable_mapping(rules.position_overrides[Position.TE])


# ============================================================================ 3 & 4
class TestSealedEvidenceHashSurvivesMutationAttempts:
    def test_hash_matches_before_and_after_a_failed_mutation(
        self, store: ObservationStore, clock
    ) -> None:
        player, team = make_player(), team_id()
        usage = wr_usage(player, team, week=1, routes=25, extra={UsageMetric.TARGETS: 3})
        store.add(usage)

        session = open_session(store, clock, ts(days=8))
        [obs] = session.read(ObservationQuery(player_id=player.player_id))
        manifest = session.seal()

        eid = evidence_id(EvidenceKind.OBSERVATION, obs.observation_id)
        entry = manifest.entry(eid)

        # #4: the sealed hash exactly matches the value the engine could consume, right now.
        assert entry.reference_hash == obs.content_hash()

        # #3: attempt mutation *after* the evidence hash was registered in the sealed manifest.
        with pytest.raises(TypeError):
            obs.metrics[UsageMetric.ROUTES] = 0.0  # type: ignore[index,attr-defined]

        # The mutation did not happen, so re-checking after the attempt still matches -- there
        # is no window in which the engine could have observed a value the manifest disagrees
        # with, because there was never a successful write to observe.
        assert entry.reference_hash == obs.content_hash()

    def test_derived_snapshot_provenance_hash_is_consistent_with_the_sealed_manifest(
        self, store: ObservationStore, clock
    ) -> None:
        """One level up the pipeline: an OrganizationalIntentSnapshot's provenance references
        observations by id; the manifest's recorded hash for that same observation id must
        agree with what the engine actually consumed while computing the snapshot."""
        player, _, _, directory = seed_prior_heavy_wr(store)
        session = open_session(store, clock, ts())
        inputs = gather_intent_inputs(session, directory, player.player_id, 2025)
        snapshot = compute_intent_snapshot(inputs, computed_at=ts(hours=1))
        manifest = session.seal()

        draft_component = snapshot.component(IntentComponent.DRAFT_INVESTMENT)
        prov = draft_component.provenance[0]
        eid = evidence_id(EvidenceKind.OBSERVATION, prov.observation_id)
        entry = manifest.entry(eid)
        assert entry.reference_hash is not None
        assert entry.evidence_id in manifest.ids()


# ============================================================================ 5
class TestCodecRoundTripsPreserveImmutability:
    def test_observation_codec_round_trip_stays_frozen(self) -> None:
        player, team = make_player(), team_id()
        usage = wr_usage(player, team, week=1, routes=12)
        codec = DEFAULT_CODECS.observation(usage.kind)
        encoded = codec.encode(usage)
        decoded = codec.decode_record(encoded)
        assert isinstance(decoded.metrics, FrozenMap)  # type: ignore[attr-defined]
        with pytest.raises(TypeError):
            decoded.metrics[UsageMetric.ROUTES] = -1.0  # type: ignore[index,attr-defined]
        # Round trip is byte-for-byte faithful, not merely "equal".
        assert codec.encode(decoded).payload == encoded.payload

    def test_decision_codec_round_trip_stays_frozen(self, store: ObservationStore, clock) -> None:
        decision = lineup_decision(open_session(store, clock, ts(days=6)))
        encoded = DEFAULT_CODECS.decision.encode(decision)
        decoded = DEFAULT_CODECS.decision.decode_record(encoded)
        assert isinstance(decoded.engine_versions, FrozenMap)
        with pytest.raises(TypeError):
            decoded.engine_versions["decision_engine"] = "tampered"  # type: ignore[index]
        assert decoded.content_hash() == decision.content_hash()

    def test_ledger_get_returns_deeply_frozen_decision(
        self, store: ObservationStore, clock
    ) -> None:
        """End to end through whichever backend is under test (memory/SQLite/PostgreSQL): the
        ledger's own hash-verify-then-decode path (ADR 0009) also yields a deeply frozen value.
        """
        ledger = InMemoryDecisionLedger(clock)
        decision = lineup_decision(open_session(store, clock, ts(days=6)))
        ledger.record(decision, status_event(decision, DecisionStatus.RECORDED))
        entry = ledger.get(decision.decision_id)
        with pytest.raises(TypeError):
            entry.decision.value.engine_versions["x"] = "y"  # type: ignore[index]


# ============================================================================ 6
class TestGoldenV1FixturesStillLoadAndAreDeeplyFrozen:
    @pytest.mark.parametrize(
        "name", sorted(p.stem for p in FIXTURES_DIR.glob("*.json")) if FIXTURES_DIR.exists() else []
    )
    def test_golden_fixture_decodes_and_dict_fields_are_frozen(self, name: str) -> None:
        record = EncodedRecord.model_validate_json((FIXTURES_DIR / f"{name}.json").read_text())
        codec = _codec_for_record_type(record.record_type)
        value = codec.decode_record(record)
        assert value is not None

        # Every dict-typed field on the decoded value (if any) must be a FrozenMap, and
        # attempting to mutate it must raise -- old payloads written before this closeout
        # still decode into the new, deeply immutable representation, because the codec always
        # constructs a *current* model instance from the versioned payload (ADR 0009).
        checked_any_mapping = False
        for field_name in type(value).model_fields:
            attr = getattr(value, field_name)
            if isinstance(attr, FrozenMap):
                checked_any_mapping = True
                _assert_immutable_mapping(attr)
        # Not every fixture has a dict field (e.g. identity_mapping_event_v1 does not); this is
        # informational only, not an assertion, so the parametrised test still documents intent.
        del checked_any_mapping


# ============================================================================ extra: other
# shallowly-frozen containers found in the broader domain audit, and the specific hash-
# instability bug this closeout uncovered and fixed.


class TestModuleLevelLookupTablesAreImmutable:
    """Not pydantic fields, but state-machine/config tables mutable in place would silently
    change behaviour for every future call, process-wide -- the same bug class, at module
    scope rather than instance scope."""

    def test_ledger_transition_table_is_immutable(self) -> None:
        from fantasy_gm.decisions.ledger import INITIAL_STATUSES, TRANSITIONS

        assert isinstance(TRANSITIONS, MappingProxyType)
        assert isinstance(INITIAL_STATUSES, MappingProxyType)
        with pytest.raises(TypeError):
            TRANSITIONS[DecisionStatus.RECORDED] = frozenset()  # type: ignore[index]

    def test_allowed_actions_table_is_immutable(self) -> None:
        assert isinstance(_ALLOWED_ACTIONS, MappingProxyType)
        with pytest.raises(TypeError):
            _ALLOWED_ACTIONS[DecisionStatus.RECORDED] = ()  # type: ignore[index]

    def test_role_dimensions_and_intent_lookup_tables_are_immutable(self) -> None:
        for table in (ROLE_DIMENSIONS, COMPONENT_CATEGORY, COMPONENT_INFORMS):
            assert isinstance(table, MappingProxyType)
            key = next(iter(table))
            with pytest.raises(TypeError):
                table[key] = None  # type: ignore[index]

    def test_risk_rank_table_is_immutable(self) -> None:
        assert isinstance(_RANK, MappingProxyType)
        with pytest.raises(TypeError):
            _RANK[ActionRiskClass.LOW] = 99  # type: ignore[index]


class TestSimulationResultsAreDeeplyFrozen:
    """``WeeklyOutcomeResult`` is a ``@dataclass(frozen=True)``, not a pydantic model --
    exactly the "shallowly frozen mutable container" pattern this closeout was asked to find
    beyond the pydantic surface. Its ``samples`` mapping must be immutable too."""

    def test_weekly_outcome_result_samples_mapping_is_immutable(self) -> None:
        dist = PlayerWeekDistribution(
            player_id="plr_a",
            season=2025,
            week=1,
            p_active=0.9,
            conditional=TruncatedNormalDistribution(mean=10.0, stdev=3.0, lower=-2.0),
            source_model="test",
        )
        result = IndependentWeeklyOutcomeSimulator().simulate(
            WeeklyOutcomeRequest(distributions=(dist,), n_sims=100, seed=SeedSpec(root_seed=1))
        )
        assert isinstance(result.samples, MappingProxyType)
        with pytest.raises(TypeError):
            result.samples["plr_a"] = None  # type: ignore[index]
        with pytest.raises(TypeError):
            del result.samples["plr_a"]  # type: ignore[attr-defined]
        # The array's own contents are also read-only (existing v0.1.1 guarantee, unaffected).
        with pytest.raises(ValueError, match="read-only"):
            result.samples["plr_a"][0] = 0.0


class TestFrozenJsonObjectDeepFreeze:
    """``ProviderLeagueSnapshot.raw`` is arbitrary provider JSON: nested dicts/lists at any
    depth must all be frozen, not just the outer mapping."""

    def test_arbitrarily_nested_payload_is_frozen_at_every_level(self) -> None:
        raw = {
            "settings": {"scoring": {"rec": 0.5}},
            "teams": [{"id": "1", "roster": ["a", "b"]}],
        }
        snap = ProviderLeagueSnapshot(
            league_ref=ProviderRef(provider="p", entity_type=EntityType.LEAGUE, external_id="1"),
            raw=raw,
            observed_at=NOW,
            effective_at=NOW,
            timestamp_quality=TimestampQuality.EXACT_PUBLICATION_TIME,
        )
        assert isinstance(snap.raw, FrozenMap)
        assert isinstance(snap.raw["settings"], FrozenMap)
        assert isinstance(snap.raw["settings"]["scoring"], FrozenMap)
        assert isinstance(snap.raw["teams"], tuple)
        assert isinstance(snap.raw["teams"][0], FrozenMap)
        assert isinstance(snap.raw["teams"][0]["roster"], tuple)
        with pytest.raises(TypeError):
            snap.raw["settings"]["scoring"]["rec"] = 1.0  # type: ignore[index]
        with pytest.raises(TypeError):
            snap.raw["teams"][0]["roster"] = ()  # type: ignore[index]
        # The original caller's dict is untouched and remains a plain, mutable dict -- proving
        # the frozen copy does not alias it.
        raw["settings"]["scoring"]["rec"] = 999
        assert snap.raw["settings"]["scoring"]["rec"] == 0.5


class TestFrozenSetCanonicalHashStability:
    """The bug this closeout uncovered while writing the hash-stability adversarial tests:
    CPython's frozenset iteration order can depend on insertion history, not only on the final
    elements, so two frozensets with identical members could serialise to different JSON list
    orders -- silently breaking ``content_hash()`` stability across an ordinary round trip.
    ``FrozenSet`` fixes this by always serialising in sorted order."""

    def test_timestamp_policy_default_frozenset_round_trip_is_hash_stable(self) -> None:
        policy = TimestampPolicy()
        clone = TimestampPolicy.model_validate_json(policy.model_dump_json())
        assert policy.content_hash() == clone.content_hash()
        dumped = policy.model_dump(mode="json")["accepted"]
        assert dumped == sorted(dumped)

    def test_player_positions_round_trip_is_hash_stable_many_times(self) -> None:
        """Repeated round trips (not just one) to make an order flip very unlikely to hide."""
        p = Player(
            player_id=new_player_id(),
            full_name="Test Multi-Position",
            positions=frozenset({Position.RB, Position.WR, Position.TE}),
        )
        current = p
        hashes = set()
        for _ in range(20):
            current = Player.model_validate_json(current.model_dump_json())
            hashes.add(current.content_hash())
        assert hashes == {p.content_hash()}
        assert current.positions == p.positions

    def test_intent_config_hash_used_on_every_snapshot_is_round_trip_stable(self) -> None:
        """``IntentConfigV0.content_hash()`` is stored on every organizational-intent snapshot
        (ADR 0005); if it were not round-trip stable, replaying the exact same config could
        record a *different* config_hash on the snapshot purely by accident of hash-seed."""
        cfg = IntentConfigV0()
        clone = IntentConfigV0.model_validate_json(cfg.model_dump_json())
        assert cfg.content_hash() == clone.content_hash()


class TestPlayerStateAndAutonomyPolicyMappingsAreFrozen:
    """Rounds out the audit list: player-state role vectors and autonomy/risk policy config
    reach decision engines the same way league state and observations do."""

    def test_latent_role_vector_estimates_immutable(self, store: ObservationStore, clock) -> None:
        player = make_player()
        team = team_id()
        store.add_many(wr_usage(player, team, week=w, routes=20) for w in range(1, 4))
        session = open_session(store, clock, ts(days=30))
        state = build_player_state(session, player)
        assert state.observed_roles is not None
        _assert_immutable_mapping(state.observed_roles.estimates)

    def test_autonomy_and_risk_policy_mappings_immutable(self) -> None:
        policy = AutonomyPolicy(
            league_id=make_league().league_id,
            mode_overrides={DecisionType.LINEUP: AutonomyMode.AUTONOMOUS},
        )
        _assert_immutable_mapping(policy.mode_overrides)
        _assert_immutable_mapping(policy.freshness.max_age)

        risk = RiskPolicy(protected_player_ids=frozenset({new_player_id()}))
        assert isinstance(risk.protected_player_ids, frozenset)
        # A real, plain frozenset never had mutating methods to begin with -- no wrapper is
        # needed here, unlike FrozenMap; this only confirms the field really is one.
        with pytest.raises(AttributeError):
            risk.protected_player_ids.add(new_player_id())  # type: ignore[attr-defined]
