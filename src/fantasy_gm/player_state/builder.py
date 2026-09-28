"""Assemble a ``PlayerState`` through a cutoff-bound reader (no projections)."""

from __future__ import annotations

from fantasy_gm.domain.ids import SnapshotId
from fantasy_gm.domain.knowledge import of_type
from fantasy_gm.domain.nfl import (
    DepthChartSignal,
    InjuryStatus,
    Player,
    PlayerTeamAssignment,
    SeasonPhase,
    UsageSnapshot,
)
from fantasy_gm.domain.observation import Observation
from fantasy_gm.domain.state import PlayerState
from fantasy_gm.player_state.roles import observed_role_vector
from fantasy_gm.player_state.store import ObservationQuery, ObservationReader


def build_player_state(
    reader: ObservationReader,
    player: Player,
    *,
    recent_games: int = 5,
    intent_snapshot_id: SnapshotId | None = None,
) -> PlayerState:
    cutoff = reader.cutoff
    known = reader.read(ObservationQuery(player_id=player.player_id))
    gaps: list[str] = []

    assignments = of_type(known, PlayerTeamAssignment)
    assignment = assignments[-1] if assignments else None
    if assignment is None:
        gaps.append("no team assignment observed")

    injuries = of_type(known, InjuryStatus)
    injury = injuries[-1] if injuries else None
    if injury is None:
        gaps.append("no injury report observed (absence is not evidence of health)")

    team_id = assignment.team_id if assignment else None
    latest_depth: dict[tuple[str, str], DepthChartSignal] = {}
    for d in of_type(known, DepthChartSignal):
        if d.team_id == team_id:
            latest_depth[(d.position, d.source_type)] = d
    if not latest_depth:
        gaps.append("no depth-chart signal for current team")

    games = [
        u
        for u in of_type(known, UsageSnapshot)
        if u.phase in (SeasonPhase.REGULAR, SeasonPhase.POSTSEASON)
    ]
    usage = games[-recent_games:]
    if not usage:
        gaps.append("no regular-season usage observed")
    roles = observed_role_vector(
        player.player_id, player.primary_position, list(reversed(games)), cutoff.as_of
    )

    used: list[Observation] = [*usage, *latest_depth.values()]
    if assignment:
        used.append(assignment)
    if injury:
        used.append(injury)

    return PlayerState(
        player_id=player.player_id,
        position=player.primary_position,
        as_of=cutoff.as_of,
        knowledge_mode=cutoff.mode,
        team_id=team_id,
        roster_status=assignment.roster_status if assignment else None,
        injury=injury,
        depth_chart=tuple(latest_depth.values()),
        recent_usage=tuple(usage),
        observed_roles=roles,
        intent_snapshot_id=intent_snapshot_id,
        data_gaps=tuple(gaps),
        source_observation_ids=tuple(o.observation_id for o in used),
    )
