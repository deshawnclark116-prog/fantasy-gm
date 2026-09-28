"""Leak-safe "what was knowable at time T" semantics.

This module is the single source of truth for information-cutoff filtering. Every store
implementation (in-memory, SQL) must delegate to ``resolve_known`` so the semantics cannot drift.
"""

from __future__ import annotations

from collections.abc import Iterable

from fantasy_gm.domain.observation import Observation
from fantasy_gm.domain.time import KnowledgeCutoff, KnowledgeMode, UtcDatetime


class LeakageError(RuntimeError):
    """Raised when post-cutoff information reaches a computation that must be leak-free."""


def is_known(observation: Observation, cutoff: KnowledgeCutoff) -> bool:
    if observation.observed_at > cutoff.as_of:
        return False
    if cutoff.mode is KnowledgeMode.SYSTEM_KNOWLEDGE:
        return observation.source.ingested_at <= cutoff.as_of
    return True


def _revision_order(observation: Observation) -> tuple[UtcDatetime, UtcDatetime, str]:
    return (observation.observed_at, observation.source.ingested_at, observation.observation_id)


def resolve_known[O: Observation](observations: Iterable[O], cutoff: KnowledgeCutoff) -> list[O]:
    """Return the latest *known* revision of every fact, as of ``cutoff``.

    Revisions published after the cutoff are invisible, so a later stat correction can never
    rewrite what a historical decision saw. Output is ordered by (effective_at, observed_at).
    """
    latest: dict[tuple[str, str], O] = {}
    for obs in observations:
        if not is_known(obs, cutoff):
            continue
        key = (obs.kind, obs.fact_key())
        current = latest.get(key)
        if current is None or _revision_order(obs) > _revision_order(current):
            latest[key] = obs
    return sorted(latest.values(), key=lambda o: (o.effective_at, o.observed_at, o.observation_id))


def assert_known(observations: Iterable[Observation], cutoff: KnowledgeCutoff) -> None:
    """Defence in depth for pure engines that receive pre-filtered inputs."""
    for obs in observations:
        if not is_known(obs, cutoff):
            raise LeakageError(
                f"{obs.kind} {obs.observation_id} observed_at={obs.observed_at.isoformat()} "
                f"ingested_at={obs.source.ingested_at.isoformat()} is not knowable at "
                f"{cutoff.as_of.isoformat()} ({cutoff.mode})"
            )


def effective_by[O: Observation](observations: Iterable[O], as_of: UtcDatetime) -> list[O]:
    """Keep only facts already in effect at ``as_of`` (known future-dated facts excluded)."""
    return [o for o in observations if o.effective_at <= as_of]


def of_type[O: Observation](observations: Iterable[Observation], cls: type[O]) -> list[O]:
    return [o for o in observations if isinstance(o, cls)]
