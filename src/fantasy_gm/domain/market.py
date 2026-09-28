"""Market observations (ADP etc.). Information about *human behaviour*, never ground truth."""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from fantasy_gm.domain.ids import PlayerId
from fantasy_gm.domain.observation import Observation


class MarketADPObservation(Observation):
    kind: ClassVar[str] = "market_adp"
    player_id: PlayerId
    market: str = Field(min_length=1)  # e.g. "sleeper:redraft:12team:ppr" -- provider-defined
    adp: float = Field(gt=0)
    adp_stdev: float | None = Field(default=None, ge=0)
    sample_size: int | None = Field(default=None, ge=0)

    def fact_key(self) -> str:
        return f"adp:{self.market}:{self.player_id}:{self.effective_at.isoformat()}"

    def subject_player_id(self) -> PlayerId:
        return self.player_id
