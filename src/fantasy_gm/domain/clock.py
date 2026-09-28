"""Injected clocks.

Authoritative "physical" timestamps (ledger ``recorded_at``, execution-attempt times, session
seal times) always come from a ``Clock`` owned by the recording component -- never from the
caller's payload.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Protocol

from fantasy_gm.domain.time import ensure_utc


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    """Deterministic clock for tests and replays."""

    def __init__(self, start: datetime) -> None:
        self._now = ensure_utc(start)
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def set(self, value: datetime) -> None:
        with self._lock:
            self._now = ensure_utc(value)

    def advance(self, delta: timedelta) -> datetime:
        with self._lock:
            self._now = self._now + delta
            return self._now
