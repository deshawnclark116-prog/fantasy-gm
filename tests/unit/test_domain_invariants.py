from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from fantasy_gm.domain.actions import DraftSelection, FreeAgentAddDrop
from fantasy_gm.domain.decision import OutcomeDistribution
from fantasy_gm.domain.ids import new_game_id, new_player_id
from fantasy_gm.domain.nfl import (
    DraftCapital,
    PlayerTeamAssignment,
    RosterStatus,
    SeasonPhase,
    UsageMetric,
    UsageSnapshot,
)
from fantasy_gm.domain.observation import SourceRef
from fantasy_gm.domain.time import KnowledgeCutoff
from tests.factories import lineup_decision, obs_times, team_id, ts


def test_naive_datetimes_rejected() -> None:
    with pytest.raises(ValidationError, match="naive"):
        KnowledgeCutoff(as_of=datetime(2025, 1, 1))  # noqa: DTZ001


def test_non_utc_normalised_to_utc() -> None:
    est = timezone(timedelta(hours=-5))
    cutoff = KnowledgeCutoff(as_of=datetime(2025, 1, 1, 7, tzinfo=est))
    assert cutoff.as_of.utcoffset() == timedelta(0) and cutoff.as_of.hour == 12


def test_models_are_frozen_and_strict() -> None:
    src = SourceRef(provider="p", ingested_at=ts())
    with pytest.raises(ValidationError):
        src.provider = "q"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        SourceRef(provider="p", ingested_at=ts(), unexpected=1)  # type: ignore[call-arg]


def test_provider_name_format() -> None:
    with pytest.raises(ValidationError):
        SourceRef(provider="Sleeper API", ingested_at=ts())


def test_draft_capital_consistency() -> None:
    with pytest.raises(ValidationError):
        DraftCapital(player_id=new_player_id(), draft_year=2024, round=1, **obs_times(ts()))
    udfa = DraftCapital(player_id=new_player_id(), draft_year=2024, **obs_times(ts()))
    assert udfa.undrafted


def test_assignment_team_iff_attached() -> None:
    with pytest.raises(ValidationError):
        PlayerTeamAssignment(
            player_id=new_player_id(),
            team_id=None,
            roster_status=RosterStatus.ACTIVE,
            **obs_times(ts()),
        )
    fa = PlayerTeamAssignment(
        player_id=new_player_id(),
        team_id=None,
        roster_status=RosterStatus.FREE_AGENT,
        **obs_times(ts()),
    )
    assert fa.subject_team_id() is None


def test_usage_missing_metric_is_not_zero() -> None:
    u = UsageSnapshot(
        player_id=new_player_id(),
        team_id=team_id(),
        game_id=new_game_id(),
        season=2025,
        week=1,
        phase=SeasonPhase.REGULAR,
        metrics={UsageMetric.ROUTES: 20},
        **obs_times(ts()),
    )
    assert u.share(UsageMetric.ROUTES, UsageMetric.TEAM_DROPBACKS) is None
    with pytest.raises(ValidationError):
        u.model_validate({**u.model_dump(), "metrics": {UsageMetric.ROUTES: -1}})


def test_outcome_distribution_quantiles_monotone() -> None:
    with pytest.raises(ValidationError):
        OutcomeDistribution(metric="m", unit="u", mean=1, quantiles={0.1: 5, 0.9: 1}, method="x")
    with pytest.raises(ValidationError):
        OutcomeDistribution(metric="m", unit="u", mean=1, quantiles={1.0: 5}, method="x")


def test_decision_invariants() -> None:
    d = lineup_decision()
    assert d.selected.candidate_id == d.selected_candidate_id
    with pytest.raises(ValidationError, match="information_cutoff"):
        lineup_decision(cutoff=ts(days=6), created=ts(days=5))
    with pytest.raises(ValidationError, match="selected_candidate_id"):
        d.model_validate({**d.model_dump(), "selected_candidate_id": "cand_nope"})
    bad_action = d.model_dump()
    bad_action["candidates"][0]["action"] = DraftSelection(
        player_id=new_player_id(), overall_pick=1
    ).model_dump()
    with pytest.raises(ValidationError, match="not a valid action"):
        d.model_validate(bad_action)


def test_decision_hash_is_stable_and_content_sensitive() -> None:
    d = lineup_decision()
    clone = type(d).model_validate_json(d.model_dump_json())
    assert clone.content_hash() == d.content_hash()
    assert d.model_copy(update={"notes": "x"}).content_hash() != d.content_hash()


def test_add_drop_requires_a_player() -> None:
    with pytest.raises(ValidationError):
        FreeAgentAddDrop()
