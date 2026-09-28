"""Gather the leak-safe evidence bundle the intent engine consumes (IO boundary)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping

from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.ids import NFLTeamId, PlayerId
from fantasy_gm.domain.knowledge import of_type
from fantasy_gm.domain.nfl import (
    CoachingAssignment,
    ContractSignal,
    DepthChartSignal,
    DraftCapital,
    Player,
    PlayerTeamAssignment,
    Position,
    PreseasonGameContext,
    RosterTransactionSignal,
    UsageSnapshot,
)
from fantasy_gm.domain.observation import Observation
from fantasy_gm.domain.time import KnowledgeCutoff
from fantasy_gm.player_state.store import ObservationQuery, ObservationReader


class CompetitorEvidence(DomainModel):
    player_id: PlayerId
    draft: DraftCapital | None = None
    contracts: tuple[ContractSignal, ...] = ()


class IntentInputs(DomainModel):
    player: Player
    position: Position
    season: int
    cutoff: KnowledgeCutoff
    team_id: NFLTeamId | None
    assignment: PlayerTeamAssignment | None = None
    draft: DraftCapital | None = None
    contracts: tuple[ContractSignal, ...] = ()
    depth_charts: tuple[DepthChartSignal, ...] = ()
    usage: tuple[UsageSnapshot, ...] = ()
    transactions: tuple[RosterTransactionSignal, ...] = ()
    coaching: tuple[CoachingAssignment, ...] = ()
    preseason_contexts: tuple[PreseasonGameContext, ...] = ()
    competitors: tuple[CompetitorEvidence, ...] = ()

    def all_observations(self) -> Iterator[Observation]:
        if self.assignment is not None:
            yield self.assignment
        if self.draft is not None:
            yield self.draft
        yield from self.contracts
        yield from self.depth_charts
        yield from self.usage
        yield from self.transactions
        yield from self.coaching
        yield from self.preseason_contexts
        for comp in self.competitors:
            if comp.draft is not None:
                yield comp.draft
            yield from comp.contracts


def _player_facts(reader: ObservationReader, player_id: PlayerId) -> list[Observation]:
    return reader.read(ObservationQuery(player_id=player_id))


def _current_team(facts: list[Observation]) -> PlayerTeamAssignment | None:
    assignments = of_type(facts, PlayerTeamAssignment)
    return assignments[-1] if assignments else None


def gather_intent_inputs(
    reader: ObservationReader,
    directory: Mapping[PlayerId, Player],
    player_id: PlayerId,
    season: int,
) -> IntentInputs:
    """Every read goes through ``reader`` (a knowledge session), so all evidence used is
    automatically recorded in the session's manifest."""
    cutoff = reader.cutoff
    player = directory[player_id]
    position = player.primary_position
    facts = _player_facts(reader, player_id)
    assignment = _current_team(facts)
    team_id = assignment.team_id if assignment else None
    drafts = of_type(facts, DraftCapital)

    transactions: list[RosterTransactionSignal] = []
    coaching: list[CoachingAssignment] = []
    contexts: list[PreseasonGameContext] = []
    competitors: list[CompetitorEvidence] = []
    if team_id is not None:
        team_facts = reader.read(ObservationQuery(team_id=team_id))
        transactions = of_type(team_facts, RosterTransactionSignal)
        coaching = of_type(team_facts, CoachingAssignment)
        contexts = [c for c in of_type(team_facts, PreseasonGameContext) if c.season == season]
        teammate_ids = {a.player_id for a in of_type(team_facts, PlayerTeamAssignment)} - {
            player_id
        }
        for mate_id in sorted(teammate_ids):
            mate = directory.get(mate_id)
            if mate is None or mate.primary_position is not position:
                continue
            mate_facts = _player_facts(reader, mate_id)
            mate_assignment = _current_team(mate_facts)
            if mate_assignment is None or mate_assignment.team_id != team_id:
                continue  # formerly on the team
            mate_drafts = of_type(mate_facts, DraftCapital)
            competitors.append(
                CompetitorEvidence(
                    player_id=mate_id,
                    draft=mate_drafts[-1] if mate_drafts else None,
                    contracts=tuple(of_type(mate_facts, ContractSignal)),
                )
            )

    return IntentInputs(
        player=player,
        position=position,
        season=season,
        cutoff=cutoff,
        team_id=team_id,
        assignment=assignment,
        draft=drafts[-1] if drafts else None,
        contracts=tuple(of_type(facts, ContractSignal)),
        depth_charts=tuple(of_type(facts, DepthChartSignal)),
        usage=tuple(of_type(facts, UsageSnapshot)),
        transactions=tuple(transactions),
        coaching=tuple(coaching),
        preseason_contexts=tuple(contexts),
        competitors=tuple(competitors),
    )
