"""Assemble a ``PlayerState`` from knowable observations (no projections in v0.1)."""

from __future__ import annotations

from fantasy_gm.domain.ids import SnapshotId
from fantasy_gm.domain.knowledge import effective_by, of_type
from fantasy_gm.domain.nfl import (
    DepthChartSignal,
    InjuryStatus,
    Player,
    PlayerTeamAssignment,
    UsageSnapshot,
)
from fantasy_gm.domain.observation import Observation
from fantasy_gm.domain.state import PlayerState
from fantasy_gm.domain.time import KnowledgeCutoff
from fantasy_gm.player_state.store import ObservationQuery, ObservationStore


def build_player_state(
    store: ObservationStore,
    player: Player,
    cutoff: KnowledgeCutoff,
    *,
    recent_games: int = 5,
    intent_snapshot_id: SnapshotId | None = None,
) -> PlayerState:
    known = effective_by(
        store.known_as_of(cutoff, ObservationQuery(player_id=player.player_id)), cutoff.as_of
    )
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
    depth = tuple(d for d in of_type(known, DepthChartSignal) if d.team_id == team_id)
    latest_depth: dict[tuple[str, str], DepthChartSignal] = {}
    for d in depth:
        latest_depth[(d.position, d.source_type)] = d
    if not latest_depth:
        gaps.append("no depth-chart signal for current team")

    usage = of_type(known, UsageSnapshot)[-recent_games:]
    if not usage:
        gaps.append("no usage observed")

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
        intent_snapshot_id=intent_snapshot_id,
        data_gaps=tuple(gaps),
        source_observation_ids=tuple(o.observation_id for o in used),
    )
