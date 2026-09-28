"""Draft order generation (snake / linear / third-round reversal) and pick-distance helpers."""

from __future__ import annotations

from collections.abc import Sequence

from fantasy_gm.domain.ids import FantasyTeamId, LeagueId
from fantasy_gm.domain.league import DraftPick, DraftSettings, DraftType


def round_order(
    teams: Sequence[FantasyTeamId], round_number: int, settings: DraftSettings
) -> list[FantasyTeamId]:
    forward = list(teams)
    if settings.draft_type is DraftType.LINEAR:
        return forward
    if settings.draft_type is DraftType.AUCTION:
        raise ValueError("auction drafts have nomination order, not pick order")
    reverse = round_number % 2 == 0
    if settings.third_round_reversal and round_number >= 3:
        reverse = not reverse
    return list(reversed(forward)) if reverse else forward


def generate_draft_order(
    league_id: LeagueId,
    season: int,
    teams: Sequence[FantasyTeamId],
    settings: DraftSettings,
) -> tuple[DraftPick, ...]:
    if len(set(teams)) != len(teams):
        raise ValueError("teams must be unique")
    picks: list[DraftPick] = []
    overall = 0
    for rnd in range(1, settings.rounds + 1):
        for idx, team in enumerate(round_order(teams, rnd, settings), start=1):
            overall += 1
            picks.append(
                DraftPick(
                    league_id=league_id,
                    season=season,
                    round=rnd,
                    pick_in_round=idx,
                    overall=overall,
                    original_team_id=team,
                    owner_team_id=team,
                )
            )
    return tuple(picks)


def picks_until_next_turn(
    picks: Sequence[DraftPick], current_overall: int, team: FantasyTeamId
) -> int | None:
    """Number of picks made by others strictly between ``current_overall`` and ``team``'s next
    pick (honours traded picks via ``owner_team_id``). None if the team has no later pick."""
    for p in sorted(picks, key=lambda p: p.overall):
        if p.overall > current_overall and p.owner_team_id == team:
            return p.overall - current_overall - 1
    return None
