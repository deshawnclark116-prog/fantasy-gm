"""Stable internal identifiers.

Internal IDs are opaque, prefixed, randomly generated strings. They are minted once by this
system and never derived from a provider's identifier, so Sleeper/ESPN/Yahoo IDs can change,
collide or disappear without touching domain identity. Provider IDs live only in
``fantasy_gm.domain.identity.ProviderIdMapping``.
"""

from __future__ import annotations

import uuid
from typing import NewType

PlayerId = NewType("PlayerId", str)
NFLTeamId = NewType("NFLTeamId", str)
GameId = NewType("GameId", str)
CoachId = NewType("CoachId", str)
LeagueId = NewType("LeagueId", str)
FantasyTeamId = NewType("FantasyTeamId", str)
ObservationId = NewType("ObservationId", str)
DecisionId = NewType("DecisionId", str)
CandidateId = NewType("CandidateId", str)
EvidenceId = NewType("EvidenceId", str)
OutcomeId = NewType("OutcomeId", str)
SnapshotId = NewType("SnapshotId", str)
StatusEventId = NewType("StatusEventId", str)
GradeId = NewType("GradeId", str)


def _new(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def new_player_id() -> PlayerId:
    return PlayerId(_new("plr"))


def new_nfl_team_id() -> NFLTeamId:
    return NFLTeamId(_new("nfl"))


def new_game_id() -> GameId:
    return GameId(_new("gam"))


def new_coach_id() -> CoachId:
    return CoachId(_new("cch"))


def new_league_id() -> LeagueId:
    return LeagueId(_new("lg"))


def new_fantasy_team_id() -> FantasyTeamId:
    return FantasyTeamId(_new("ft"))


def new_observation_id() -> ObservationId:
    return ObservationId(_new("obs"))


def new_decision_id() -> DecisionId:
    return DecisionId(_new("dec"))


def new_candidate_id() -> CandidateId:
    return CandidateId(_new("cand"))


def new_evidence_id() -> EvidenceId:
    return EvidenceId(_new("evd"))


def new_outcome_id() -> OutcomeId:
    return OutcomeId(_new("out"))


def new_snapshot_id() -> SnapshotId:
    return SnapshotId(_new("snap"))


def new_status_event_id() -> StatusEventId:
    return StatusEventId(_new("dse"))


def new_grade_id() -> GradeId:
    return GradeId(_new("grd"))
