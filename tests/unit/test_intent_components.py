from __future__ import annotations

from datetime import timedelta

import pytest

from fantasy_gm.domain.ids import CoachId, new_player_id
from fantasy_gm.domain.knowledge import LeakageError
from fantasy_gm.domain.nfl import (
    CoachingAssignment,
    CoachingRole,
    ContractKind,
    ContractSignal,
    DepthChartSignal,
    DepthChartSource,
    DraftCapital,
    Position,
    RosterTransactionSignal,
    SeasonPhase,
    TeamTransactionKind,
    UsageMetric,
)
from fantasy_gm.domain.time import KnowledgeCutoff
from fantasy_gm.organizational_intent import components as c
from fantasy_gm.organizational_intent.config import IntentConfigV0
from fantasy_gm.organizational_intent.engine import compute_intent_snapshot
from fantasy_gm.organizational_intent.inputs import IntentInputs
from fantasy_gm.organizational_intent.snapshot import IntentComponent
from tests.factories import make_player, obs_times, team_id, ts, wr_usage

CFG = IntentConfigV0()


def test_draft_score_monotone_and_bounded() -> None:
    scores = [c.draft_pick_score(p, CFG.draft_max_overall_pick) for p in (1, 12, 32, 100, 262)]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == 1.0 and 0.0 <= scores[-1] < 0.01


def test_draft_confidence_decays_and_other_team_discount() -> None:
    team = team_id()
    draft = DraftCapital(
        player_id=new_player_id(),
        draft_year=2021,
        round=1,
        overall_pick=5,
        drafting_team_id=team,
        **obs_times(ts(days=-4 * 365)),
    )
    same = c.draft_investment(draft, team, ts(), CFG)
    other = c.draft_investment(draft, team_id(), ts(), CFG)
    fresh = c.draft_investment(
        draft.model_copy(update={"effective_at": ts(days=-10)}), team, ts(), CFG
    )
    assert fresh.confidence > same.confidence > other.confidence
    assert other.notes


def test_rookie_contract_not_double_counted() -> None:
    team = team_id()
    contract = ContractSignal(
        player_id=new_player_id(),
        team_id=team,
        contract_kind=ContractKind.ROOKIE_SCALE,
        position_market_percentile=0.9,
        **obs_times(ts(days=-100)),
    )
    res = c.contract_investment([contract], team, CFG)
    assert res.score is None and "draft capital" in res.notes[0]
    vet = contract.model_copy(update={"contract_kind": ContractKind.EXTENSION})
    assert c.contract_investment([vet], team, CFG).score == 0.9


def test_transactions_signal_and_absence() -> None:
    team, me = team_id(), new_player_id()
    assert c.recent_transactions([], me, Position.RB, team, ts(), CFG).score is None
    added = RosterTransactionSignal(
        team_id=team,
        player_id=new_player_id(),
        position=Position.RB,
        transaction_kind=TeamTransactionKind.SIGNED,
        **obs_times(ts(days=-20)),
    )
    old = added.model_copy(update={"effective_at": ts(days=-400)})
    res = c.recent_transactions([added, old], me, Position.RB, team, ts(), CFG)
    assert res.score is not None and res.score < 0.5
    assert res.measurements["transactions"] == 1


def test_inferred_depth_chart_excluded_and_regime_discount() -> None:
    team, pid = team_id(), new_player_id()
    inferred = DepthChartSignal(
        player_id=pid,
        team_id=team,
        position=Position.WR,
        depth_rank=1,
        source_type=DepthChartSource.INFERRED_FROM_USAGE,
        **obs_times(ts(days=-1)),
    )
    assert c.depth_chart([inferred], Position.WR, team, ts(), None, CFG).score is None
    official = inferred.model_copy(update={"source_type": DepthChartSource.OFFICIAL_TEAM})
    base = c.depth_chart([official], Position.WR, team, ts(), None, CFG)
    discounted = c.depth_chart([official], Position.WR, team, ts(), ts(), CFG)
    assert discounted.confidence == pytest.approx(base.confidence * CFG.regime_change_discount)


def _coach(team, role, coach, days):  # type: ignore[no-untyped-def]
    return CoachingAssignment(
        team_id=team,
        role=role,
        coach_id=CoachId(coach),
        coach_name=coach,
        **obs_times(ts(days=days)),
    )


def test_coaching_change_sets_regime_and_uses_tenure_start() -> None:
    team = team_id()
    hist = [
        _coach(team, CoachingRole.HEAD_COACH, "hc_old", -900),
        _coach(team, CoachingRole.HEAD_COACH, "hc_new", -200),
        _coach(team, CoachingRole.HEAD_COACH, "hc_new", -20),  # re-observation, same tenure
        _coach(team, CoachingRole.OFFENSIVE_PLAY_CALLER, "oc_a", -900),
        _coach(team, CoachingRole.OFFENSIVE_PLAY_CALLER, "oc_a", -30),
    ]
    res = c.coaching_continuity(hist, team, ts(), CFG)
    assert res.regime_start == ts(days=-200)
    # HC changed inside the lookback; play caller has no prior holder -> unknown, not "stable".
    assert res.component.score == 0.0
    assert res.component.measurements["roles_known"] == 1
    assert any("hc_old -> hc_new" in n for n in res.component.notes)


def test_preseason_total_snaps_not_used_as_proxy() -> None:
    p, team = make_player(), team_id()
    pre = wr_usage(
        p,
        team,
        1,
        routes=10,
        phase=SeasonPhase.PRESEASON,
        extra={UsageMetric.OFFENSE_SNAPS: 50, UsageMetric.TEAM_OFFENSE_SNAPS: 60},
    )
    res = c.preseason_deployment([pre], [], None, 2025, team, CFG)
    assert res.score is None and any("starters" in n for n in res.notes)


def test_usage_other_team_excluded_and_fallback_quality() -> None:
    p, team = make_player(), team_id()
    elsewhere = wr_usage(p, team_id(), 1, routes=40)
    res, n_eff = c.actual_usage(
        [elsewhere], p.player_id, Position.WR, 2025, team, ts(days=60), None, CFG
    )
    assert res.score is None and n_eff == 0
    snaps_only = wr_usage(p, team, 2, routes=0).model_copy(
        update={"metrics": {UsageMetric.OFFENSE_SNAPS: 30, UsageMetric.TEAM_OFFENSE_SNAPS: 60}}
    )
    res, n_eff = c.actual_usage(
        [snaps_only], p.player_id, Position.WR, 2025, team, ts(days=60), None, CFG
    )
    assert n_eff == pytest.approx(CFG.usage_fallback_quality)
    assert any("fell back to rec_snap_share" in n for n in res.notes)
    assert res.method.endswith("rec_snap_share")


def test_engine_refuses_leaked_inputs() -> None:
    p, team = make_player(), team_id()
    future = wr_usage(p, team, 3, routes=30)  # observed after the cutoff below
    inputs = IntentInputs(
        player=p,
        position=Position.WR,
        season=2025,
        cutoff=KnowledgeCutoff(as_of=ts(days=1)),
        team_id=team,
        usage=(future,),
    )
    with pytest.raises(LeakageError):
        compute_intent_snapshot(inputs, computed_at=ts(days=1))


def test_insufficient_evidence_flagged() -> None:
    p = make_player()
    inputs = IntentInputs(
        player=p,
        position=Position.WR,
        season=2025,
        cutoff=KnowledgeCutoff(as_of=ts()),
        team_id=None,
    )
    snap = compute_intent_snapshot(inputs, computed_at=ts() + timedelta(seconds=1))
    assert snap.insufficient_evidence and snap.insufficiency_reasons
    assert all(comp.influence == 0 for comp in snap.components)
    assert snap.component(IntentComponent.ACTUAL_USAGE).score is None


def test_config_hash_changes_with_parameters() -> None:
    assert IntentConfigV0().content_hash() != IntentConfigV0(prior_pseudo_games=8).content_hash()
