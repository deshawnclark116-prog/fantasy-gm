"""Run context: whether a decision is live, paper (shadow) or a historical replay."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.ids import RunId, new_run_id
from fantasy_gm.domain.time import KnowledgeMode


class RunMode(StrEnum):
    LIVE = "live"  # real league, may notify and (policy permitting) execute
    PAPER = "paper"  # real-time shadow run: live data, never notifies or executes
    REPLAY = "replay"  # historical reconstruction/backtest; recorded now about the past


class RunContext(DomainModel):
    run_id: RunId = Field(default_factory=new_run_id)
    mode: RunMode
    knowledge_mode: KnowledgeMode = KnowledgeMode.SYSTEM_KNOWLEDGE
    label: str = ""

    @model_validator(mode="after")
    def _live_requires_system_knowledge(self) -> Self:
        if self.mode in (RunMode.LIVE, RunMode.PAPER) and (
            self.knowledge_mode is not KnowledgeMode.SYSTEM_KNOWLEDGE
        ):
            raise ValueError(f"{self.mode} runs must use SYSTEM_KNOWLEDGE")
        return self

    @property
    def is_real_time(self) -> bool:
        return self.mode in (RunMode.LIVE, RunMode.PAPER)
