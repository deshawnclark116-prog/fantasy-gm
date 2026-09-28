from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from fantasy_gm.domain.actions import DraftSelection, FreeAgentAddDrop
from fantasy_gm.domain.clock import ManualClock
from fantasy_gm.domain.decision import OutcomeDistribution
from fantasy_gm.domain.execution import (
    ExecutionEvent,
    ExecutionEventKind,
    ExecutionPhase,
    derive_execution_state,
)
from fantasy_gm.domain.ids import DecisionId, new_attempt_id, new_game_id, new_player_id
from fantasy_gm.domain.nfl import (
    DraftCapital,
    PlayerTeamAssignment,
    RosterStatus,
    SeasonPhase,
    UsageMetric,
    UsageSnapshot,
)
from fantasy_gm.domain.observation import SourceRef
from fantasy_gm.domain.time import KnowledgeCutoff, TimestampQuality
from tests.factories import NOW, lineup_decision, memory_store, obs_times, open_session, team_id, ts


def test_naive_datetimes_rejected() -> None:
    with pytest.raises(ValidationError, match="naive"):
        KnowledgeCutoff(as_of=datetime(2025, 1, 1))  # noqa: DTZ001


def test_non_utc_normalised_to_utc() -> None:
    est = timezone(timedelta(hours=-5))
    cutoff = KnowledgeCutoff(as_of=datetime(2025, 1, 1, 7, tzinfo=est))
    assert cutoff.as_of.utcoffset() == timedelta(0) and cutoff.as_of.hour == 12


def test_models_are_frozen_and_strict() -> None:
    src = SourceRef(
        provider="p", ingested_at=ts(), timestamp_quality=TimestampQuality.EXACT_PUBLICATION_TIME
    )
    with pytest.raises(ValidationError):
        src.provider = "q"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        SourceRef(
            provider="Sleeper API", ingested_at=ts(), timestamp_quality=TimestampQuality.UNKNOWN
        )


def test_draft_capital_consistency() -> None:
    with pytest.raises(ValidationError):
        DraftCapital(player_id=new_player_id(), draft_year=2024, round=1, **obs_times(ts()))
    assert DraftCapital(player_id=new_player_id(), draft_year=2024, **obs_times(ts())).undrafted


def test_assignment_team_iff_attached() -> None:
    with pytest.raises(ValidationError):
        PlayerTeamAssignment(
            player_id=new_player_id(),
            team_id=None,
            roster_status=RosterStatus.ACTIVE,
            **obs_times(ts()),
        )


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


def test_outcome_distribution_quantiles_monotone() -> None:
    with pytest.raises(ValidationError):
        OutcomeDistribution(metric="m", unit="u", mean=1, quantiles={0.1: 5, 0.9: 1}, method="x")


def test_decision_invariants() -> None:
    clock = ManualClock(NOW)
    d = lineup_decision(open_session(memory_store(), clock, ts(days=6)))
    with pytest.raises(ValidationError, match="selected_candidate_id"):
        d.model_validate({**d.model_dump(), "selected_candidate_id": "cand_nope"})
    bad = d.model_dump()
    bad["candidates"][0]["action"] = DraftSelection(
        player_id=new_player_id(), overall_pick=1
    ).model_dump()
    with pytest.raises(ValidationError, match="not a valid action"):
        d.model_validate(bad)
    with pytest.raises(ValidationError, match="manifest"):
        d.model_validate({**d.model_dump(), "decision_artifact_hash": "not-in-manifest"})
    with pytest.raises(ValidationError, match="cutoff"):
        d.model_validate({**d.model_dump(), "information_cutoff": ts(days=5)})


def test_decision_hash_is_stable_and_content_sensitive() -> None:
    clock = ManualClock(NOW)
    d = lineup_decision(open_session(memory_store(), clock, ts(days=6)))
    clone = type(d).model_validate_json(d.model_dump_json())
    assert clone.content_hash() == d.content_hash()
    assert d.model_copy(update={"notes": "x"}).content_hash() != d.content_hash()


def test_add_drop_requires_a_player() -> None:
    with pytest.raises(ValidationError):
        FreeAgentAddDrop()


def test_execution_state_machine() -> None:
    did = DecisionId("dec_x")
    att = new_attempt_id()

    def ev(kind: ExecutionEventKind, lease: datetime | None = None) -> ExecutionEvent:
        return ExecutionEvent(
            decision_id=did,
            attempt_id=att,
            kind=kind,
            occurred_at=NOW,
            provider="p",
            idempotency_key="k",
            action_fingerprint="f",
            worker_id="w",
            lease_expires_at=lease,
        )

    started = ev(ExecutionEventKind.ATTEMPT_STARTED, NOW + timedelta(minutes=1))
    assert derive_execution_state((), NOW).phase is ExecutionPhase.READY
    assert derive_execution_state((started,), NOW).phase is ExecutionPhase.IN_FLIGHT
    assert derive_execution_state((started,), NOW + timedelta(minutes=2)).phase is (
        ExecutionPhase.UNCERTAIN
    )
    unknown = ev(ExecutionEventKind.OUTCOME_UNKNOWN)
    assert derive_execution_state((started, unknown), NOW).phase is ExecutionPhase.UNCERTAIN
    done = ev(ExecutionEventKind.RECONCILED_EXECUTED)
    assert derive_execution_state((started, unknown, done), NOW).phase is ExecutionPhase.SUCCEEDED
