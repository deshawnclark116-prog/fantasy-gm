"""Test factories. All people, teams and provider IDs are fictional."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fantasy_gm.domain.actions import NoAction, SetLineup
from fantasy_gm.domain.autonomy import AutonomyMode, DecisionType
from fantasy_gm.domain.decision import (
    Actor,
    ActorKind,
    ConfidenceAssessment,
    Decision,
    DecisionCandidate,
    DecisionEvidence,
    DecisionStatus,
    DecisionStatusEvent,
    EvidenceKind,
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
    LineupLock,
    PlayoffSettings,
    RosterEntry,
    RosterRules,
    RosterSlot,
    ScoringRules,
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
from fantasy_gm.domain.observation import SourceRef

T0 = datetime(2025, 9, 1, 12, 0, tzinfo=UTC)
PROVIDER = "test_feed"


def ts(days: float = 0, hours: float = 0) -> datetime:
    return T0 + timedelta(days=days, hours=hours)


def source(ingested_at: datetime, provider: str = PROVIDER) -> SourceRef:
    return SourceRef(provider=provider, ingested_at=ingested_at)


def obs_times(effective: datetime, observed: datetime | None = None, lag_hours: float = 0) -> dict:
    """observed_at defaults to effective; ingested_at = observed_at + lag."""
    observed = observed or effective
    return {
        "effective_at": effective,
        "observed_at": observed,
        "source": source(observed + timedelta(hours=lag_hours)),
    }


def make_player(name: str = "Test Receiver", *positions: Position) -> Player:
    return Player(
        player_id=new_player_id(),
        full_name=name,
        positions=frozenset(positions or (Position.WR,)),
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


def te_premium_superflex_rules() -> ScoringRules:
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
    return RosterRules(
        slots=(
            RosterSlot(
                label="QB",
                kind=SlotKind.STARTER,
                count=1,
                eligible_positions=frozenset({Position.QB}),
            ),
            RosterSlot(
                label="RB",
                kind=SlotKind.STARTER,
                count=2,
                eligible_positions=frozenset({Position.RB}),
            ),
            RosterSlot(
                label="WR",
                kind=SlotKind.STARTER,
                count=3,
                eligible_positions=frozenset({Position.WR}),
            ),
            RosterSlot(
                label="TE",
                kind=SlotKind.STARTER,
                count=1,
                eligible_positions=frozenset({Position.TE}),
            ),
            RosterSlot(label="FLEX", kind=SlotKind.STARTER, count=1, eligible_positions=flex),
            RosterSlot(
                label="SUPERFLEX",
                kind=SlotKind.STARTER,
                count=1,
                eligible_positions=flex | {Position.QB},
            ),
            RosterSlot(
                label="K",
                kind=SlotKind.STARTER,
                count=1,
                eligible_positions=frozenset({Position.K}),
            ),
            RosterSlot(
                label="DST",
                kind=SlotKind.STARTER,
                count=1,
                eligible_positions=frozenset({Position.DST}),
            ),
            RosterSlot(label="BN", kind=SlotKind.BENCH, count=6),
            RosterSlot(label="IR", kind=SlotKind.RESERVE, count=2),
        ),
        max_per_position={Position.K: 1},
    )


def make_league(managed: FantasyTeamId | None = None, faab_budget: int | None = 100) -> League:
    return League(
        league_id=new_league_id(),
        name="Test League",
        season=2025,
        num_teams=12,
        scoring=te_premium_superflex_rules(),
        roster_rules=superflex_roster_rules(),
        draft=DraftSettings(draft_type=DraftType.SNAKE, rounds=17, third_round_reversal=True),
        waivers=(
            WaiverSettings(waiver_type=WaiverType.FAAB, faab_budget=faab_budget)
            if faab_budget is not None
            else WaiverSettings(waiver_type=WaiverType.ROLLING)
        ),
        playoffs=PlayoffSettings(playoff_teams=6, first_week=15, last_week=17),
        lineup_lock=LineupLock.PER_GAME,
        trade_deadline_week=11,
        managed_team_id=managed or new_fantasy_team_id(),
    )


def lineup_decision(
    *,
    league_id: LeagueId | None = None,
    team: FantasyTeamId | None = None,
    cutoff: datetime | None = None,
    created: datetime | None = None,
    mode: AutonomyMode = AutonomyMode.RECOMMEND,
    confidence: float = 0.9,
    insufficient: bool = False,
    evidence_observed: datetime | None = None,
    start_player: PlayerId | None = None,
    bench_player: PlayerId | None = None,
) -> Decision:
    cutoff = cutoff or ts(days=6)
    created = created or cutoff + timedelta(minutes=5)
    start_player = start_player or new_player_id()
    bench_player = bench_player or new_player_id()
    evidence = DecisionEvidence(
        kind=EvidenceKind.PROJECTION,
        reference_id="projection:placeholder",
        observed_at=evidence_observed or cutoff - timedelta(hours=1),
        summary="upstream weekly distribution (test fixture)",
    )

    def dist(mean: float) -> OutcomeDistribution:
        return OutcomeDistribution(
            metric="lineup_points",
            unit="fantasy_points",
            mean=mean,
            stdev=8.0,
            quantiles={0.1: mean - 10, 0.5: mean, 0.9: mean + 10},
            method="test_fixture",
        )

    start = DecisionCandidate(
        action=SetLineup(week=1, entries=(RosterEntry(player_id=start_player, slot_label="FLEX"),)),
        estimated_outcome=dist(110.0),
        rationale="start A",
        evidence_ids=(evidence.evidence_id,),
    )
    alt = DecisionCandidate(
        action=SetLineup(week=1, entries=(RosterEntry(player_id=bench_player, slot_label="FLEX"),)),
        estimated_outcome=dist(106.0),
        rationale="start B",
        evidence_ids=(evidence.evidence_id,),
    )
    hold = DecisionCandidate(action=NoAction(), rationale="keep current lineup")
    return Decision(
        league_id=league_id or new_league_id(),
        fantasy_team_id=team or new_fantasy_team_id(),
        decision_type=DecisionType.LINEUP,
        created_at=created,
        information_cutoff=cutoff,
        candidates=(start, alt, hold),
        selected_candidate_id=start.candidate_id,
        model_versions={"decision_engine": "test_fixture_v0"},
        evidence=(evidence,),
        confidence=ConfidenceAssessment(score=confidence, insufficient_evidence=insufficient),
        autonomy_mode=mode,
    )


def status_event(
    decision: Decision,
    status: DecisionStatus,
    at: datetime | None = None,
    actor: ActorKind = ActorKind.SYSTEM,
    **kwargs: object,
) -> DecisionStatusEvent:
    return DecisionStatusEvent(
        decision_id=decision.decision_id,
        status=status,
        occurred_at=at or decision.created_at,
        actor=Actor(kind=actor, actor_id=actor.value),
        **kwargs,  # type: ignore[arg-type]
    )
