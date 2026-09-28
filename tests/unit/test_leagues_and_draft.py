from __future__ import annotations

from decimal import Decimal

import pytest

from fantasy_gm.domain.ids import new_fantasy_team_id, new_league_id
from fantasy_gm.domain.league import (
    AllowedTier,
    DraftSettings,
    DraftType,
    RosterEntry,
    ScoringRules,
    StatKey,
)
from fantasy_gm.domain.nfl import Position
from fantasy_gm.draft.order import generate_draft_order, picks_until_next_turn
from fantasy_gm.leagues.roster_validation import validate_roster
from tests.factories import make_player, superflex_roster_rules


def test_snake_with_third_round_reversal() -> None:
    teams = [new_fantasy_team_id() for _ in range(4)]
    picks = generate_draft_order(
        new_league_id(),
        2025,
        teams,
        DraftSettings(draft_type=DraftType.SNAKE, rounds=4, third_round_reversal=True),
    )
    by_round = [[p.original_team_id for p in picks if p.round == r] for r in range(1, 5)]
    assert by_round[0] == teams
    assert by_round[1] == teams[::-1]
    assert by_round[2] == teams[::-1]  # reversal
    assert by_round[3] == teams
    assert [p.overall for p in picks] == list(range(1, 17))


def test_picks_until_next_turn_honours_trades() -> None:
    teams = [new_fantasy_team_id() for _ in range(4)]
    picks = list(
        generate_draft_order(
            new_league_id(), 2025, teams, DraftSettings(draft_type=DraftType.SNAKE, rounds=3)
        )
    )
    assert picks_until_next_turn(picks, 1, teams[0]) == 6  # 1 -> 8
    picks[3] = picks[3].model_copy(update={"owner_team_id": teams[0]})  # acquires pick 4
    assert picks_until_next_turn(picks, 1, teams[0]) == 2
    assert picks_until_next_turn(picks, 12, teams[0]) is None


def test_auction_requires_budget() -> None:
    with pytest.raises(ValueError, match="auction_budget"):
        DraftSettings(draft_type=DraftType.AUCTION, rounds=15)


def test_tiered_and_linear_conflict_rejected() -> None:
    with pytest.raises(ValueError, match="both tiered"):
        ScoringRules(
            name="x",
            per_stat={StatKey.DST_POINTS_ALLOWED: Decimal(-1)},
            dst_points_allowed_tiers=(AllowedTier(min_allowed=0, points=Decimal(5)),),
        )


def test_roster_violations() -> None:
    rules = superflex_roster_rules()
    k1, k2 = make_player("K One", Position.K), make_player("K Two", Position.K)
    wr = make_player("WR", Position.WR)
    elig = {p.player_id: p.positions for p in (k1, k2, wr)}
    entries = (
        RosterEntry(player_id=k1.player_id, slot_label="K"),
        RosterEntry(player_id=k2.player_id, slot_label="BN"),
        RosterEntry(player_id=wr.player_id, slot_label="TE"),
        RosterEntry(player_id=wr.player_id, slot_label="NOPE"),
    )
    codes = sorted(v.code for v in validate_roster(entries, rules, elig))
    assert codes == ["duplicate_player", "ineligible", "position_limit", "unknown_slot"]
