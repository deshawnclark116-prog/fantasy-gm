"""Concrete fantasy actions a decision can select.

Actions reference internal IDs only. Translating to provider IDs is the transaction provider's
job at execution time.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.capabilities import ProviderCapability
from fantasy_gm.domain.ids import FantasyTeamId, PlayerId
from fantasy_gm.domain.league import RosterEntry


class NoAction(DomainModel):
    """Explicitly doing nothing is a decision and must be recorded/graded like any other."""

    action_type: Literal["no_action"] = "no_action"

    @property
    def required_capability(self) -> None:
        return None


class DraftSelection(DomainModel):
    action_type: Literal["draft_selection"] = "draft_selection"
    player_id: PlayerId
    overall_pick: int = Field(ge=1)

    @property
    def required_capability(self) -> ProviderCapability:
        return ProviderCapability.DRAFT_WRITE


class SetLineup(DomainModel):
    action_type: Literal["set_lineup"] = "set_lineup"
    week: int = Field(ge=0, le=25)
    entries: tuple[RosterEntry, ...] = Field(min_length=1)

    @property
    def required_capability(self) -> ProviderCapability:
        return ProviderCapability.LINEUP_WRITE


class FreeAgentAddDrop(DomainModel):
    action_type: Literal["free_agent_add_drop"] = "free_agent_add_drop"
    add_player_id: PlayerId | None = None
    drop_player_id: PlayerId | None = None

    @model_validator(mode="after")
    def _something(self) -> Self:
        if self.add_player_id is None and self.drop_player_id is None:
            raise ValueError("add/drop must add or drop at least one player")
        return self

    @property
    def required_capability(self) -> ProviderCapability:
        return ProviderCapability.ADD_DROP_WRITE


class WaiverClaim(DomainModel):
    action_type: Literal["waiver_claim"] = "waiver_claim"
    add_player_id: PlayerId
    drop_player_id: PlayerId | None = None
    faab_bid: int | None = Field(default=None, ge=0)
    claim_priority: int | None = Field(default=None, ge=1)

    @property
    def required_capability(self) -> ProviderCapability:
        return ProviderCapability.WAIVER_WRITE


class ProposeTrade(DomainModel):
    action_type: Literal["propose_trade"] = "propose_trade"
    counterparty_team_id: FantasyTeamId
    send_player_ids: tuple[PlayerId, ...]
    receive_player_ids: tuple[PlayerId, ...]

    @model_validator(mode="after")
    def _non_empty(self) -> Self:
        if not self.send_player_ids and not self.receive_player_ids:
            raise ValueError("trade must move at least one player")
        return self

    @property
    def required_capability(self) -> ProviderCapability:
        return ProviderCapability.TRADE_PROPOSE_WRITE


class RespondToTrade(DomainModel):
    action_type: Literal["respond_to_trade"] = "respond_to_trade"
    provider_trade_ref: str
    accept: bool

    @property
    def required_capability(self) -> ProviderCapability:
        return ProviderCapability.TRADE_RESPOND_WRITE


Action = Annotated[
    NoAction
    | DraftSelection
    | SetLineup
    | FreeAgentAddDrop
    | WaiverClaim
    | ProposeTrade
    | RespondToTrade,
    Field(discriminator="action_type"),
]
