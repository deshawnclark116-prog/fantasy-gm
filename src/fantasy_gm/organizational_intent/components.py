"""Pure component calculations for Organizational Intent v0.

Each function maps evidence -> ``ComponentResult`` (score, confidence, measurements, provenance,
notes). No IO, no clock reads: ``as_of`` is always passed in.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from fantasy_gm.domain.ids import NFLTeamId
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
    UsageSnapshot,
)
from fantasy_gm.domain.observation import Observation
from fantasy_gm.organizational_intent.config import IntentConfigV0
from fantasy_gm.organizational_intent.inputs import CompetitorEvidence
from fantasy_gm.organizational_intent.snapshot import ProvenanceRecord

_DAYS_PER_YEAR = 365.25


@dataclass(frozen=True)
class ComponentResult:
    method: str
    score: float | None = None
    confidence: float = 0.0
    measurements: dict[str, float] = field(default_factory=dict)
    provenance: tuple[ProvenanceRecord, ...] = ()
    notes: tuple[str, ...] = ()


def provenance(obs: Observation, fact: str) -> ProvenanceRecord:
    return ProvenanceRecord(
        observation_id=obs.observation_id,
        observation_kind=obs.kind,
        provider=obs.source.provider,
        observed_at=obs.observed_at,
        effective_at=obs.effective_at,
        fact=fact,
    )


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _years_between(earlier: datetime, later: datetime) -> float:
    return max(0.0, (later - earlier).total_seconds() / 86400 / _DAYS_PER_YEAR)


def _half_life(age: float, half_life: float) -> float:
    return float(0.5 ** (age / half_life))


def _regime_factor(
    effective_at: datetime, regime_start: datetime | None, cfg: IntentConfigV0
) -> float:
    if regime_start is not None and effective_at < regime_start:
        return cfg.regime_change_discount
    return 1.0


# --------------------------------------------------------------------------- draft


def draft_pick_score(overall_pick: int, max_pick: int) -> float:
    """Monotone ordinal transform of pick number (1 -> 1.0, last pick -> ~0). Not a value chart."""
    return _clamp(1.0 - math.log(overall_pick) / math.log(max_pick))


def draft_investment(
    draft: DraftCapital | None,
    team_id: NFLTeamId | None,
    as_of: datetime,
    cfg: IntentConfigV0,
) -> ComponentResult:
    method = "log_pick_rank_v0"
    if draft is None:
        return ComponentResult(method=method, notes=("no draft-capital observation",))
    years = _years_between(draft.effective_at, as_of)
    confidence = _half_life(years, cfg.draft_confidence_half_life_years)
    notes: list[str] = []
    if draft.undrafted:
        score = 0.0
        fact = f"undrafted ({draft.draft_year})"
    else:
        assert draft.overall_pick is not None
        score = draft_pick_score(draft.overall_pick, cfg.draft_max_overall_pick)
        fact = f"{draft.draft_year} round {draft.round}, overall pick {draft.overall_pick}"
        if draft.drafting_team_id != team_id:
            confidence *= cfg.draft_other_team_factor
            notes.append("drafted by a different organisation than the current team")
    measurements = {"years_since_draft": round(years, 4)}
    if draft.overall_pick is not None:
        measurements["overall_pick"] = float(draft.overall_pick)
    return ComponentResult(
        method=method,
        score=score,
        confidence=_clamp(confidence),
        measurements=measurements,
        provenance=(provenance(draft, fact),),
        notes=tuple(notes),
    )


# --------------------------------------------------------------------------- contract


def _current_contract(
    contracts: Sequence[ContractSignal], team_id: NFLTeamId | None
) -> ContractSignal | None:
    mine = [c for c in contracts if c.team_id == team_id]
    return max(mine, key=lambda c: (c.effective_at, c.observed_at)) if mine else None


def contract_investment(
    contracts: Sequence[ContractSignal], team_id: NFLTeamId | None, cfg: IntentConfigV0
) -> ComponentResult:
    method = "position_market_percentile_v0"
    contract = _current_contract(contracts, team_id)
    if contract is None:
        return ComponentResult(method=method, notes=("no contract observed with current team",))
    prov = provenance(
        contract,
        f"{contract.contract_kind} contract, apy={contract.apy_usd}, "
        f"guaranteed={contract.guaranteed_usd}, "
        f"position_percentile={contract.position_market_percentile}",
    )
    if contract.contract_kind in (ContractKind.ROOKIE_SCALE, ContractKind.UDFA):
        return ComponentResult(
            method=method,
            provenance=(prov,),
            notes=("rookie/UDFA contract: investment is captured by draft capital, not recounted",),
        )
    if contract.position_market_percentile is None:
        return ComponentResult(
            method=method,
            provenance=(prov,),
            notes=("contract has no positional market context; cannot score without inventing it",),
        )
    return ComponentResult(
        method=method,
        score=contract.position_market_percentile,
        confidence=cfg.contract_confidence,
        measurements={"position_market_percentile": contract.position_market_percentile},
        provenance=(prov,),
    )


# --------------------------------------------------------------------------- competition


def _investment_proxy(
    draft: DraftCapital | None,
    contracts: Sequence[ContractSignal],
    team_id: NFLTeamId | None,
    as_of: datetime,
    cfg: IntentConfigV0,
) -> float | None:
    values: list[float] = []
    if draft is not None:
        base = (
            0.0
            if draft.overall_pick is None
            else draft_pick_score(draft.overall_pick, cfg.draft_max_overall_pick)
        )
        years = _years_between(draft.effective_at, as_of)
        values.append(base * _half_life(years, cfg.draft_confidence_half_life_years))
    contract = _current_contract(contracts, team_id)
    if (
        contract is not None
        and contract.position_market_percentile is not None
        and contract.contract_kind not in (ContractKind.ROOKIE_SCALE, ContractKind.UDFA)
    ):
        values.append(contract.position_market_percentile)
    return max(values) if values else None


def roster_competition(
    draft: DraftCapital | None,
    contracts: Sequence[ContractSignal],
    competitors: Sequence[CompetitorEvidence],
    position: Position,
    team_id: NFLTeamId | None,
    as_of: datetime,
    cfg: IntentConfigV0,
) -> ComponentResult:
    method = "investment_rank_in_position_room_v0"
    if team_id is None:
        return ComponentResult(method=method, notes=("player has no current team",))
    primary_roles = cfg.competition_primary_roles.get(position)
    if primary_roles is None:
        return ComponentResult(method=method, notes=(f"no primary-role count for {position}",))
    mine = _investment_proxy(draft, contracts, team_id, as_of, cfg)
    if mine is None:
        return ComponentResult(
            method=method, notes=("player's own investment unknown; cannot rank",)
        )
    rank = 1
    unknown = 0
    prov: list[ProvenanceRecord] = []
    for comp in competitors:
        theirs = _investment_proxy(comp.draft, comp.contracts, team_id, as_of, cfg)
        if theirs is None:
            unknown += 1
            continue
        if theirs > mine:
            rank += 1
        for obs in (comp.draft, _current_contract(comp.contracts, team_id)):
            if obs is not None:
                prov.append(provenance(obs, f"competitor {comp.player_id} investment evidence"))
    room = len(competitors) + 1
    score = 1.0 if rank <= primary_roles else primary_roles / rank
    known_fraction = (room - unknown) / room
    notes = (f"{unknown} same-position teammate(s) with unknown investment",) if unknown else ()
    return ComponentResult(
        method=method,
        score=score,
        confidence=_clamp(cfg.competition_base_confidence * known_fraction),
        measurements={
            "investment_rank": float(rank),
            "room_size": float(room),
            "primary_roles": float(primary_roles),
            "unknown_competitors": float(unknown),
        },
        provenance=tuple(prov),
        notes=notes,
    )


# --------------------------------------------------------------------------- transactions

_ACQUIRE = {
    TeamTransactionKind.DRAFTED,
    TeamTransactionKind.SIGNED,
    TeamTransactionKind.TRADED_FOR,
    TeamTransactionKind.CLAIMED_OFF_WAIVERS,
}
_RETAIN = {TeamTransactionKind.RE_SIGNED, TeamTransactionKind.EXTENDED}
_DEPART = {TeamTransactionKind.RELEASED, TeamTransactionKind.TRADED_AWAY}


def _transaction_delta(txn: RosterTransactionSignal, is_subject: bool) -> float:
    kind = txn.transaction_kind
    if is_subject:
        if kind in _ACQUIRE or kind in _RETAIN:
            return 1.0
        if kind in _DEPART:
            return -2.0
        return 0.0  # the subject's own reserve moves are health, not intent
    if kind in _ACQUIRE:
        return -1.0  # competition added
    if kind in _DEPART or kind is TeamTransactionKind.PLACED_ON_RESERVE:
        return 1.0  # competition removed / sidelined
    if kind is TeamTransactionKind.ACTIVATED_FROM_RESERVE:
        return -1.0
    return 0.0


def recent_transactions(
    transactions: Sequence[RosterTransactionSignal],
    subject_player_id: str,
    position: Position,
    team_id: NFLTeamId | None,
    as_of: datetime,
    cfg: IntentConfigV0,
) -> ComponentResult:
    method = "net_positional_transactions_tanh_v0"
    window_start = as_of - timedelta(days=cfg.transaction_lookback_days)
    relevant = [
        t
        for t in transactions
        if t.team_id == team_id and t.position is position and window_start <= t.effective_at
    ]
    if not relevant:
        return ComponentResult(
            method=method,
            notes=(
                "no positional transactions observed in window; absence cannot be distinguished "
                "from missing feed coverage, so no score is emitted",
            ),
        )
    net = 0.0
    prov: list[ProvenanceRecord] = []
    for txn in relevant:
        is_subject = txn.player_id == subject_player_id
        delta = _transaction_delta(txn, is_subject)
        net += delta
        who = "subject" if is_subject else f"teammate {txn.player_id}"
        prov.append(provenance(txn, f"{who} {txn.transaction_kind} ({delta:+.1f})"))
    return ComponentResult(
        method=method,
        score=_clamp(0.5 + 0.5 * math.tanh(net / 2.0)),
        confidence=cfg.transaction_confidence,
        measurements={"net_signal": net, "transactions": float(len(relevant))},
        provenance=tuple(prov),
    )


# --------------------------------------------------------------------------- depth chart


def depth_chart(
    signals: Sequence[DepthChartSignal],
    position: Position,
    team_id: NFLTeamId | None,
    as_of: datetime,
    regime_start: datetime | None,
    cfg: IntentConfigV0,
) -> ComponentResult:
    method = "inverse_depth_rank_v0"
    usable = [
        s
        for s in signals
        if s.team_id == team_id
        and s.position is position
        and s.source_type is not DepthChartSource.INFERRED_FROM_USAGE
    ]
    if not usable:
        return ComponentResult(method=method, notes=("no usable depth-chart signal",))
    latest_by_source: dict[DepthChartSource, DepthChartSignal] = {}
    for s in sorted(usable, key=lambda s: (s.effective_at, s.observed_at)):
        latest_by_source[s.source_type] = s

    def conf(s: DepthChartSignal) -> float:
        age_days = max(0.0, (as_of - s.effective_at).total_seconds() / 86400)
        return (
            cfg.depth_source_reliability.get(s.source_type, 0.0)
            * _half_life(age_days, cfg.depth_staleness_half_life_days)
            * _regime_factor(s.effective_at, regime_start, cfg)
        )

    best = max(latest_by_source.values(), key=conf)
    ranks = {s.depth_rank for s in latest_by_source.values()}
    notes = ("depth-chart sources disagree on rank",) if len(ranks) > 1 else ()
    return ComponentResult(
        method=method,
        score=1.0 / best.depth_rank,
        confidence=_clamp(conf(best)),
        measurements={"depth_rank": float(best.depth_rank)},
        provenance=tuple(
            provenance(s, f"{s.source_type} depth chart: {s.position}{s.depth_rank}")
            for s in latest_by_source.values()
        ),
        notes=notes,
    )


# --------------------------------------------------------------------------- coaching


@dataclass(frozen=True)
class CoachingResult:
    component: ComponentResult
    regime_start: datetime | None


def coaching_continuity(
    assignments: Sequence[CoachingAssignment],
    team_id: NFLTeamId | None,
    as_of: datetime,
    cfg: IntentConfigV0,
) -> CoachingResult:
    method = "role_continuity_fraction_v0"
    mine = sorted(
        (a for a in assignments if a.team_id == team_id),
        key=lambda a: (a.effective_at, a.observed_at),
    )
    by_role: dict[CoachingRole, list[CoachingAssignment]] = {}
    for a in mine:
        by_role.setdefault(a.role, []).append(a)
    play_caller_role = (
        CoachingRole.OFFENSIVE_PLAY_CALLER
        if CoachingRole.OFFENSIVE_PLAY_CALLER in by_role
        else CoachingRole.OFFENSIVE_COORDINATOR
    )
    roles = (CoachingRole.HEAD_COACH, play_caller_role)
    window_start = as_of - timedelta(days=cfg.coaching_lookback_days)
    known = 0
    stable = 0
    regime_start: datetime | None = None
    prov: list[ProvenanceRecord] = []
    notes: list[str] = []
    for role in roles:
        history = by_role.get(role, [])
        if not history:
            notes.append(f"no {role} assignment observed")
            continue
        current = history[-1]
        # The current tenure starts at the earliest record in the trailing run of this coach.
        tenure_start = current
        previous: CoachingAssignment | None = None
        for a in reversed(history):
            if a.coach_id != current.coach_id:
                previous = a
                break
            tenure_start = a
        prov.append(provenance(current, f"{role}: {current.coach_name}"))
        if previous is None:
            notes.append(f"{role}: no prior holder observed; continuity unknown")
            continue
        known += 1
        change_at = tenure_start.effective_at
        regime_start = change_at if regime_start is None else max(regime_start, change_at)
        prov.append(provenance(previous, f"previous {role}: {previous.coach_name}"))
        if change_at >= window_start:
            notes.append(f"{role} changed {previous.coach_name} -> {current.coach_name}")
        else:
            stable += 1
    if known == 0:
        return CoachingResult(
            ComponentResult(method=method, provenance=tuple(prov), notes=tuple(notes)),
            regime_start,
        )
    return CoachingResult(
        ComponentResult(
            method=method,
            score=stable / known,
            confidence=_clamp(0.8 * known / len(roles)),
            measurements={"roles_known": float(known), "roles_stable": float(stable)},
            provenance=tuple(prov),
            notes=tuple(notes),
        ),
        regime_start,
    )


# --------------------------------------------------------------------------- preseason


def preseason_deployment(
    usage: Sequence[UsageSnapshot],
    season: int,
    team_id: NFLTeamId | None,
    cfg: IntentConfigV0,
) -> ComponentResult:
    method = "first_team_snap_share_v0"
    games = [
        u
        for u in usage
        if u.phase is SeasonPhase.PRESEASON and u.season == season and u.team_id == team_id
    ]
    with_split = [
        u
        for u in games
        if UsageMetric.FIRST_TEAM_SNAPS in u.metrics
        and u.metrics.get(UsageMetric.TEAM_FIRST_TEAM_SNAPS, 0) > 0
    ]
    if not with_split:
        note = (
            "no first-team snap split observed; total preseason snap share is NOT used as a proxy "
            "because starters typically play the fewest preseason snaps"
        )
        return ComponentResult(method=method, notes=(note,) if games else ("no preseason usage",))
    num = sum(u.metrics[UsageMetric.FIRST_TEAM_SNAPS] for u in with_split)
    den = sum(u.metrics[UsageMetric.TEAM_FIRST_TEAM_SNAPS] for u in with_split)
    share = _clamp(num / den)
    games_factor = min(1.0, len(with_split) / cfg.preseason_full_confidence_games)
    return ComponentResult(
        method=method,
        score=share,
        confidence=_clamp(cfg.preseason_first_team_confidence * games_factor),
        measurements={"first_team_snap_share": share, "games": float(len(with_split))},
        provenance=tuple(
            provenance(
                u,
                f"preseason wk{u.week}: first-team snaps "
                f"{u.metrics[UsageMetric.FIRST_TEAM_SNAPS]:g}/"
                f"{u.metrics[UsageMetric.TEAM_FIRST_TEAM_SNAPS]:g}",
            )
            for u in with_split
        ),
    )


# --------------------------------------------------------------------------- actual usage


def primary_usage_share(u: UsageSnapshot, position: Position) -> tuple[float | None, str]:
    """Position-appropriate opportunity share for one game, and the metric used."""
    if position is Position.QB:
        share = u.share(UsageMetric.DROPBACKS, UsageMetric.TEAM_DROPBACKS)
        return share, "dropback_share"
    if position is Position.RB:
        opps = [u.metrics.get(m) for m in (UsageMetric.CARRIES, UsageMetric.TARGETS)]
        team = [u.metrics.get(m) for m in (UsageMetric.TEAM_CARRIES, UsageMetric.TEAM_TARGETS)]
        if None in opps or None in team:
            return None, "opportunity_share"
        den = sum(x for x in team if x is not None)
        num = sum(x for x in opps if x is not None)
        return (min(num / den, 1.0) if den > 0 else None), "opportunity_share"
    if position in (Position.WR, Position.TE):
        return u.share(UsageMetric.ROUTES, UsageMetric.TEAM_DROPBACKS), "route_participation"
    return None, "undefined"


def actual_usage(
    usage: Sequence[UsageSnapshot],
    position: Position,
    season: int,
    team_id: NFLTeamId | None,
    regime_start: datetime | None,
    cfg: IntentConfigV0,
) -> tuple[ComponentResult, float]:
    """Returns (component, effective_games)."""
    method = "recency_weighted_position_share_v0"
    reference = cfg.usage_full_role_reference.get(position)
    games = sorted(
        (
            u
            for u in usage
            if u.phase in (SeasonPhase.REGULAR, SeasonPhase.POSTSEASON)
            and u.team_id == team_id
            and u.season <= season
        ),
        key=lambda u: (u.effective_at, u.observed_at),
        reverse=True,
    )[: cfg.usage_max_games]
    if not games:
        note = "no regular-season usage with current team"
        return ComponentResult(method=method, notes=(note,)), 0.0
    if reference is None:
        return (
            ComponentResult(method=method, notes=(f"no usage role reference for {position}",)),
            0.0,
        )
    weighted_sum = 0.0
    weight_total = 0.0
    n_eff = 0.0
    skipped = 0
    fallbacks = 0
    prov: list[ProvenanceRecord] = []
    for i, u in enumerate(games):
        share, metric = primary_usage_share(u, position)
        quality = 1.0
        if share is None:
            share = u.share(UsageMetric.OFFENSE_SNAPS, UsageMetric.TEAM_OFFENSE_SNAPS)
            metric, quality = "snap_share_fallback", cfg.usage_fallback_quality
            if share is None:
                skipped += 1
                continue
            fallbacks += 1
        evidence_weight = (
            quality
            * _regime_factor(u.effective_at, regime_start, cfg)
            * (cfg.usage_prior_season_discount if u.season < season else 1.0)
        )
        recency = _half_life(float(i), cfg.usage_recency_half_life_games)
        weighted_sum += evidence_weight * recency * share
        weight_total += evidence_weight * recency
        n_eff += evidence_weight
        prov.append(provenance(u, f"{u.season} wk{u.week} {metric}={share:.3f}"))
    notes: list[str] = []
    if skipped:
        notes.append(f"{skipped} game(s) lacked any usable share metric and were excluded")
    if fallbacks:
        notes.append(f"{fallbacks} game(s) used snap share because the primary metric was missing")
    if weight_total == 0:
        return ComponentResult(method=method, provenance=tuple(prov), notes=tuple(notes)), 0.0
    raw_share = weighted_sum / weight_total
    return (
        ComponentResult(
            method=method,
            score=_clamp(raw_share / reference),
            confidence=_clamp(n_eff / (n_eff + cfg.prior_pseudo_games)),
            measurements={
                "weighted_share": round(raw_share, 6),
                "full_role_reference": reference,
                "effective_games": round(n_eff, 6),
                "games_considered": float(len(games)),
            },
            provenance=tuple(prov),
            notes=tuple(notes),
        ),
        n_eff,
    )


def weighted_mean(pairs: Iterable[tuple[float, float]]) -> float | None:
    num = 0.0
    den = 0.0
    for value, weight in pairs:
        num += value * weight
        den += weight
    return None if den == 0 else num / den
