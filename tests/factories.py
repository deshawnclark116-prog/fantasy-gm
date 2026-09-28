"""Test factories. All people, teams and provider IDs are fictional."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from fantasy_gm.decisions.builder import build_decision
from fantasy_gm.domain.actions import NoAction, SetLineup
from fantasy_gm.domain.artifacts import FitKind, ModelArtifactManifest
from fantasy_gm.domain.autonomy import AutonomyMode, DecisionType
from fantasy_gm.domain.base import sha256_hex
from fantasy_gm.domain.clock import Clock, ManualClock
from fantasy_gm.domain.decision import (
    Actor,
    ActorKind,
    ConfidenceAssessment,
    Decision,
    DecisionCandidate,
    DecisionStatus,
    DecisionStatusEvent,
    OutcomeDistribution,
)
from fantasy_gm.domain.ids import (
    FantasyTeamId,
    GameId,
    LeagueId,
    NFLTeamId,
    PlayerId,
    new_fantasy_team_id,
    new_game_id,
    new_league_id,
    new_nfl_team_id,
    new_player_id,
)
from fantasy_gm.domain.league import (
    DraftSettings,
    DraftType,
    League,
    LeagueDraftSettings,
    LeagueRosterSettings,
    LeagueScoringSettings,
    LeagueSeasonSettings,
    LeagueWaiverSettings,
    LineupLock,
    PlayoffSettings,
    RosterEntry,
    RosterRules,
    RosterSlot,
    ScoringRules,
    SeasonSettings,
    SlotKind,
    StatKey,
    WaiverSettings,
    WaiverType,
)
from fantasy_gm.domain.nfl import (
    Player,
    PlayerTeamAssignment,
    Position,
    RosterStatus,
    SeasonPhase,
    UsageMetric,
    UsageSnapshot,
)
from fantasy_gm.domain.observation import Observation, SourceRef
from fantasy_gm.domain.run_context import RunContext, RunMode
from fantasy_gm.domain.time import KnowledgeCutoff, KnowledgeMode, TimestampQuality
from fantasy_gm.knowledge.session import KnowledgeSession
from fantasy_gm.player_state.store import InMemoryObservationStore, ObservationStore

T0 = datetime(2025, 9, 1, 12, 0, tzinfo=UTC)  # "historical" season anchor
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)  # the physical present for most tests
# Store clock for tests that simulate a system that was already live during the 2025 season:
# rows are physically stored at EPOCH, so SYSTEM_KNOWLEDGE replays see them per ingested_at.
EPOCH = datetime(2020, 1, 1, tzinfo=UTC)
PROVIDER = "test_feed"
EXACT = TimestampQuality.EXACT_PUBLICATION_TIME


def ts(days: float = 0, hours: float = 0) -> datetime:
    return T0 + timedelta(days=days, hours=hours)


def source(
    ingested_at: datetime, provider: str = PROVIDER, quality: TimestampQuality = EXACT
) -> SourceRef:
    return SourceRef(provider=provider, ingested_at=ingested_at, timestamp_quality=quality)


def obs_times(
    effective: datetime,
    observed: datetime | None = None,
    lag_hours: float = 0,
    quality: TimestampQuality = EXACT,
) -> dict[str, Any]:
    """observed_at defaults to effective; ingested_at = observed_at + lag."""
    observed = observed or effective
    return {
        "effective_at": effective,
        "observed_at": observed,
        "source": source(observed + timedelta(hours=lag_hours), quality=quality),
    }


def make_player(name: str = "Test Receiver", *positions: Position) -> Player:
    return Player(
        player_id=new_player_id(), full_name=name, positions=frozenset(positions or (Position.WR,))
    )


def team_id() -> NFLTeamId:
    return new_nfl_team_id()


def assignment(player: Player, team: NFLTeamId, effective: datetime) -> PlayerTeamAssignment:
    return PlayerTeamAssignment(
        player_id=player.player_id,
        team_id=team,
        roster_status=RosterStatus.ACTIVE,
        **obs_times(effective),
    )


def wr_usage(
    player: Player,
    team: NFLTeamId,
    week: int,
    routes: float,
    team_dropbacks: float = 40,
    season: int = 2025,
    effective: datetime | None = None,
    phase: SeasonPhase = SeasonPhase.REGULAR,
    game: GameId | None = None,
    extra: dict[UsageMetric, float] | None = None,
) -> UsageSnapshot:
    effective = effective or ts(days=7 * week)
    metrics = {UsageMetric.ROUTES: routes, UsageMetric.TEAM_DROPBACKS: team_dropbacks}
    metrics.update(extra or {})
    return UsageSnapshot(
        player_id=player.player_id,
        team_id=team,
        game_id=game or new_game_id(),
        season=season,
        week=week,
        phase=phase,
        metrics=metrics,
        **obs_times(effective, effective + timedelta(hours=12)),
    )


# --------------------------------------------------------------------------- league


def te_premium_rules() -> ScoringRules:
    return ScoringRules(
        name="custom: 0.5 PPR, TE premium, 6pt pass TD",
        per_stat={
            StatKey.PASS_YD: Decimal("0.04"),
            StatKey.PASS_TD: Decimal(6),
            StatKey.PASS_INT: Decimal(-2),
            StatKey.RUSH_YD: Decimal("0.1"),
            StatKey.RUSH_TD: Decimal(6),
            StatKey.REC: Decimal("0.5"),
            StatKey.REC_YD: Decimal("0.1"),
            StatKey.REC_TD: Decimal(6),
            StatKey.FUMBLE_LOST: Decimal(-2),
            StatKey.DST_SACK: Decimal(1),
        },
        position_overrides={Position.TE: {StatKey.REC: Decimal("1.5")}},
    )


def superflex_roster_rules() -> RosterRules:
    flex = frozenset({Position.RB, Position.WR, Position.TE})

    def slot(label: str, count: int, *pos: Position) -> RosterSlot:
        return RosterSlot(
            label=label, kind=SlotKind.STARTER, count=count, eligible_positions=frozenset(pos)
        )

    return RosterRules(
        slots=(
            slot("QB", 1, Position.QB),
            slot("RB", 2, Position.RB),
            slot("WR", 3, Position.WR),
            slot("TE", 1, Position.TE),
            RosterSlot(label="FLEX", kind=SlotKind.STARTER, count=1, eligible_positions=flex),
            RosterSlot(
                label="SUPERFLEX",
                kind=SlotKind.STARTER,
                count=1,
                eligible_positions=flex | {Position.QB},
            ),
            slot("K", 1, Position.K),
            slot("DST", 1, Position.DST),
            RosterSlot(label="BN", kind=SlotKind.BENCH, count=6),
            RosterSlot(label="IR", kind=SlotKind.RESERVE, count=2),
        ),
        max_per_position={Position.K: 1},
    )


def make_league(managed: FantasyTeamId | None = None) -> League:
    return League(
        league_id=new_league_id(),
        name="Test League",
        season=2025,
        num_teams=12,
        managed_team_id=managed or new_fantasy_team_id(),
    )


def league_settings(
    league: League,
    effective: datetime,
    faab_budget: int | None = 100,
    scoring: ScoringRules | None = None,
) -> list[Observation]:
    """The complete initial rule set of a league as temporal observations."""
    lid = league.league_id
    waivers = (
        WaiverSettings(waiver_type=WaiverType.FAAB, faab_budget=faab_budget)
        if faab_budget is not None
        else WaiverSettings(waiver_type=WaiverType.ROLLING)
    )
    return [
        LeagueScoringSettings(
            league_id=lid, scoring=scoring or te_premium_rules(), **obs_times(effective)
        ),
        LeagueRosterSettings(
            league_id=lid, roster_rules=superflex_roster_rules(), **obs_times(effective)
        ),
        LeagueWaiverSettings(league_id=lid, waivers=waivers, **obs_times(effective)),
        LeagueSeasonSettings(
            league_id=lid,
            settings=SeasonSettings(
                playoffs=PlayoffSettings(playoff_teams=6, first_week=15, last_week=17),
                lineup_lock=LineupLock.PER_GAME,
                trade_deadline_week=11,
            ),
            **obs_times(effective),
        ),
        LeagueDraftSettings(
            league_id=lid,
            draft=DraftSettings(draft_type=DraftType.SNAKE, rounds=17, third_round_reversal=True),
            **obs_times(effective),
        ),
    ]


# --------------------------------------------------------------------------- runs & sessions


def memory_store() -> InMemoryObservationStore:
    return InMemoryObservationStore(ManualClock(EPOCH))


def replay_run(mode: KnowledgeMode = KnowledgeMode.SYSTEM_KNOWLEDGE) -> RunContext:
    return RunContext(mode=RunMode.REPLAY, knowledge_mode=mode, label="test replay")


def live_run() -> RunContext:
    return RunContext(mode=RunMode.LIVE, label="test live")


def open_session(
    store: ObservationStore,
    clock: Clock,
    as_of: datetime,
    run: RunContext | None = None,
    cutoff: KnowledgeCutoff | None = None,
) -> KnowledgeSession:
    run = run or replay_run()
    cutoff = cutoff or KnowledgeCutoff(as_of=as_of, mode=run.knowledge_mode)
    return KnowledgeSession(run, cutoff, store, clock)


def heuristic_artifact(
    name: str = "test_decision_engine", version: str = "0"
) -> ModelArtifactManifest:
    return ModelArtifactManifest(
        name=name,
        version=version,
        fit_kind=FitKind.HAND_SPECIFIED,
        config_hash=sha256_hex(f"{name}:{version}"),
        created_at=T0,
    )


def fitted_artifact(trained_through: datetime, name: str = "test_model") -> ModelArtifactManifest:
    return ModelArtifactManifest(
        name=name,
        version="1",
        fit_kind=FitKind.FITTED,
        trained_through=trained_through,
        training_data_manifest_hash=sha256_hex(f"training:{trained_through.isoformat()}"),
        feature_schema_version="features_v1",
        feature_schema_hash=sha256_hex("features_v1"),
        config_hash=sha256_hex("{}"),
        created_at=max(trained_through, NOW),
    )


def _dist(mean: float) -> OutcomeDistribution:
    return OutcomeDistribution(
        metric="lineup_points",
        unit="fantasy_points",
        mean=mean,
        stdev=8.0,
        quantiles={0.1: mean - 10, 0.5: mean, 0.9: mean + 10},
        method="test_fixture",
    )


def lineup_decision(
    session: KnowledgeSession,
    *,
    league_id: LeagueId | None = None,
    team: FantasyTeamId | None = None,
    mode: AutonomyMode = AutonomyMode.RECOMMEND,
    confidence: float = 0.9,
    insufficient: bool = False,
    artifact: ModelArtifactManifest | None = None,
    decision_time: datetime | None = None,
    start_player: PlayerId | None = None,
    bench_player: PlayerId | None = None,
    regime: str = "regular_season",
) -> Decision:
    artifact = artifact or heuristic_artifact()
    art_eid = session.use_model(artifact)
    proj_eid = session.record_derived(
        "weekly_projection", _dist(110.0), f"proj:{new_player_id()}", [art_eid]
    )
    start = DecisionCandidate(
        action=SetLineup(
            week=1,
            entries=(RosterEntry(player_id=start_player or new_player_id(), slot_label="FLEX"),),
        ),
        estimated_outcome=_dist(110.0),
        rationale="start A",
        evidence_ids=(proj_eid,),
    )
    alt = DecisionCandidate(
        action=SetLineup(
            week=1,
            entries=(RosterEntry(player_id=bench_player or new_player_id(), slot_label="FLEX"),),
        ),
        estimated_outcome=_dist(106.0),
        rationale="start B",
        evidence_ids=(proj_eid,),
    )
    hold = DecisionCandidate(action=NoAction(), rationale="keep current lineup")
    return build_decision(
        session,
        league_id=league_id or new_league_id(),
        fantasy_team_id=team or new_fantasy_team_id(),
        decision_type=DecisionType.LINEUP,
        candidates=(start, alt, hold),
        selected_candidate_id=start.candidate_id,
        engine_versions={"decision_engine": "test_fixture_v0"},
        decision_artifact=artifact,
        regime=regime,
        confidence=ConfidenceAssessment(score=confidence, insufficient_evidence=insufficient),
        autonomy_mode=mode,
        decision_time=decision_time,
    )


def status_event(
    decision: Decision,
    status: DecisionStatus,
    at: datetime | None = None,
    actor: ActorKind = ActorKind.SYSTEM,
    **kwargs: Any,
) -> DecisionStatusEvent:
    return DecisionStatusEvent(
        decision_id=decision.decision_id,
        status=status,
        occurred_at=at or decision.decision_time,
        actor=Actor(kind=actor, actor_id=actor.value),
        **kwargs,
    )


__all__ = ["NOW", "T0", "DecisionType", "LeagueId"]
