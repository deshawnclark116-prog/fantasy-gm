"""Player state: the system's current belief about a player, as of an information cutoff.

v0.1 is a *container* for observed state plus explicit data gaps. It deliberately contains no
projection -- the projection model is a later milestone.
"""

from __future__ import annotations

from pydantic import Field

from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.ids import NFLTeamId, ObservationId, PlayerId, SnapshotId
from fantasy_gm.domain.nfl import (
    DepthChartSignal,
    InjuryStatus,
    Position,
    RosterStatus,
    UsageSnapshot,
)
from fantasy_gm.domain.time import KnowledgeMode, UtcDatetime


class PlayerState(DomainModel):
    player_id: PlayerId
    position: Position
    as_of: UtcDatetime
    knowledge_mode: KnowledgeMode
    team_id: NFLTeamId | None
    roster_status: RosterStatus | None
    injury: InjuryStatus | None
    depth_chart: tuple[DepthChartSignal, ...] = ()
    recent_usage: tuple[UsageSnapshot, ...] = ()
    intent_snapshot_id: SnapshotId | None = None
    # Explicit statements of what we do not know (e.g. "no injury report observed").
    data_gaps: tuple[str, ...] = ()
    source_observation_ids: tuple[ObservationId, ...] = Field(default_factory=tuple)
