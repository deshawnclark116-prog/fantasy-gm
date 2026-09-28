"""Roster/lineup legality against league roster rules.

Eligibility is supplied by the caller per league because fantasy platforms assign positional
eligibility independently of the NFL (and differently from each other).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping

from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.ids import PlayerId
from fantasy_gm.domain.league import FantasyRoster, RosterEntry, RosterRules, SlotKind
from fantasy_gm.domain.nfl import Position


class RosterViolation(DomainModel):
    code: str
    message: str


def validate_roster(
    entries: tuple[RosterEntry, ...] | FantasyRoster,
    rules: RosterRules,
    eligibility: Mapping[PlayerId, frozenset[Position]],
) -> list[RosterViolation]:
    rows = entries.entries if isinstance(entries, FantasyRoster) else entries
    violations: list[RosterViolation] = []

    dupes = [pid for pid, n in Counter(e.player_id for e in rows).items() if n > 1]
    for pid in dupes:
        violations.append(RosterViolation(code="duplicate_player", message=f"{pid} rostered twice"))

    labels = {s.label for s in rules.slots}
    per_slot = Counter(e.slot_label for e in rows)
    for label, used in per_slot.items():
        if label not in labels:
            violations.append(RosterViolation(code="unknown_slot", message=f"no slot {label!r}"))
            continue
        cap = rules.slot(label).count
        if used > cap:
            violations.append(
                RosterViolation(code="slot_overfilled", message=f"{label}: {used} > {cap}")
            )

    for e in rows:
        if e.slot_label not in labels:
            continue
        slot = rules.slot(e.slot_label)
        if slot.kind is not SlotKind.STARTER or not slot.eligible_positions:
            continue
        positions = eligibility.get(e.player_id)
        if positions is None:
            violations.append(
                RosterViolation(code="unknown_eligibility", message=f"{e.player_id} eligibility")
            )
        elif not positions & slot.eligible_positions:
            violations.append(
                RosterViolation(
                    code="ineligible",
                    message=f"{e.player_id} ({sorted(positions)}) cannot fill {slot.label}",
                )
            )

    for pos, limit in rules.max_per_position.items():
        count = sum(1 for e in rows if pos in eligibility.get(e.player_id, frozenset()))
        if count > limit:
            violations.append(
                RosterViolation(code="position_limit", message=f"{pos}: {count} > {limit}")
            )
    return violations
