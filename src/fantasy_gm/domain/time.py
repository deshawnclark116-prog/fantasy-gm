"""Time primitives.

Every timestamp in the domain is timezone-aware and normalised to UTC. Naive datetimes are
rejected at validation time because they make leak-safe historical reconstruction ambiguous.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator

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

    SYSTEM_KNOWLEDGE (default, strict): the fact was publicly observable *and* had actually been
    ingested by this system at the cutoff. Use for every live decision -- it reflects what the
    system really had in hand.

    PUBLIC_AVAILABILITY: the fact was publicly observable at the cutoff, even if we ingested it
    later (backfill). Needed for historical backtests built from backfilled data. Its honesty
    depends entirely on providers reporting truthful publication timestamps.
    """

    SYSTEM_KNOWLEDGE = "system_knowledge"
    PUBLIC_AVAILABILITY = "public_availability"


class KnowledgeCutoff(DomainModel):
    as_of: UtcDatetime
    mode: KnowledgeMode = KnowledgeMode.SYSTEM_KNOWLEDGE
