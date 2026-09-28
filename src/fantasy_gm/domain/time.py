"""Time primitives, knowledge cutoffs and timestamp-trust metadata.

Every timestamp in the domain is timezone-aware and normalised to UTC. Naive datetimes are
rejected at validation time because they make leak-safe historical reconstruction ambiguous.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, Field

from fantasy_gm.domain.base import DomainModel


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive datetimes are forbidden; supply a timezone-aware timestamp")
    return value.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(ensure_utc)]


def utc_now() -> datetime:
    return datetime.now(UTC)


class KnowledgeMode(StrEnum):
    """How an information cutoff decides whether an observation was knowable.

    SYSTEM_KNOWLEDGE (default, strict, mandatory for LIVE/PAPER): the fact was publicly
    observable *and* had actually been ingested by this system at the cutoff. Ingestion latency
    is real system ignorance and is never retroactively erased.

    PUBLIC_AVAILABILITY: the fact was publicly observable at the cutoff, even if ingested later
    (backfill). Only valid for REPLAY runs, and only as honest as the provider's publication
    timestamps -- see ``TimestampQuality`` / ``TimestampPolicy``.
    """

    SYSTEM_KNOWLEDGE = "system_knowledge"
    PUBLIC_AVAILABILITY = "public_availability"


class TimestampQuality(StrEnum):
    """What an observation's ``observed_at`` actually represents.

    EXACT_PUBLICATION_TIME: provider states when this content was first published.
    LAST_UPDATED_ONLY: provider exposes only a last-modified time. Conservative for *this*
        content (it existed no later than then), though earlier versions may have existed.
    PROVIDER_EVENT_TIME: the timestamp describes the real-world event (e.g. kickoff), not
        publication. Unsafe as a knowability time: stats were published after the event.
    INGESTION_TIME_ONLY: we only know when we fetched it; ``observed_at == ingested_at``.
    UNKNOWN: timestamp semantics unknown; ``observed_at == ingested_at`` is enforced.
    """

    EXACT_PUBLICATION_TIME = "exact_publication_time"
    LAST_UPDATED_ONLY = "last_updated_only"
    PROVIDER_EVENT_TIME = "provider_event_time"
    INGESTION_TIME_ONLY = "ingestion_time_only"
    UNKNOWN = "unknown"


# Qualities whose observed_at may never differ from ingested_at (no fabricated publication time).
INGESTION_BOUND_QUALITIES = frozenset(
    {TimestampQuality.INGESTION_TIME_ONLY, TimestampQuality.UNKNOWN}
)


class InsufficientTimestampAction(StrEnum):
    EXCLUDE = "exclude"  # treat as not knowable for this experiment
    FLAG = "flag"  # allow, but mark the evidence as timestamp-suspect in the manifest


def _default_accepted() -> frozenset[TimestampQuality]:
    return frozenset(
        {
            TimestampQuality.EXACT_PUBLICATION_TIME,
            TimestampQuality.LAST_UPDATED_ONLY,
            TimestampQuality.INGESTION_TIME_ONLY,
        }
    )


class TimestampPolicy(DomainModel):
    """Which timestamp qualities a PUBLIC_AVAILABILITY experiment trusts.

    Ignored under SYSTEM_KNOWLEDGE, where our own ingestion clock bounds knowability.
    """

    accepted: frozenset[TimestampQuality] = Field(default_factory=_default_accepted)
    on_insufficient: InsufficientTimestampAction = InsufficientTimestampAction.EXCLUDE


class KnowledgeCutoff(DomainModel):
    as_of: UtcDatetime
    mode: KnowledgeMode = KnowledgeMode.SYSTEM_KNOWLEDGE
    timestamp_policy: TimestampPolicy = Field(default_factory=TimestampPolicy)
    # Reproducibility pin: only records physically stored by this time are visible. Under
    # SYSTEM_KNOWLEDGE the store always applies ``as_of`` as this bound. For PUBLIC_AVAILABILITY
    # experiments, pinning makes a replay repeatable even as more backfill arrives later.
    stored_by: UtcDatetime | None = None

    def storage_bound(self) -> UtcDatetime | None:
        bounds = [b for b in (self.stored_by,) if b is not None]
        if self.mode is KnowledgeMode.SYSTEM_KNOWLEDGE:
            bounds.append(self.as_of)
        return min(bounds) if bounds else None
