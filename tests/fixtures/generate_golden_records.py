"""One-off generator for the golden v1 record fixtures.

Run ONLY when intentionally adding a new golden fixture. Never regenerate an existing fixture to
"fix" a failing test: a failure means a durable model changed incompatibly and needs a legacy
reader (see ADR 0009).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from tests.factories import (
    NOW,
    fitted_artifact,
    lineup_decision,
    make_player,
    memory_store,
    open_session,
    status_event,
    team_id,
    ts,
    wr_usage,
)

from fantasy_gm.domain.clock import ManualClock
from fantasy_gm.domain.decision import DecisionStatus, ObservedOutcome
from fantasy_gm.domain.identity import (
    EntityType,
    IdentityMappingEvent,
    MappingMethod,
    MappingStatus,
    ProviderRef,
)
from fantasy_gm.records.registry import DEFAULT_CODECS

OUT = Path(__file__).parent / "records"


def dump(name: str, record: object) -> None:
    path = OUT / f"{name}.json"
    if path.exists():
        print(f"skip existing {path.name}")
        return
    path.write_text(json.dumps(record.model_dump(), indent=2, sort_keys=True) + "\n")  # type: ignore[attr-defined]
    print(f"wrote {path.name}")


def main() -> None:
    clock = ManualClock(NOW)
    store = memory_store()
    usage = wr_usage(make_player(), team_id(), 1, routes=30)
    store.add(usage)
    decision = lineup_decision(open_session(store, clock, ts(days=6)))
    c = DEFAULT_CODECS
    dump("decision_v1", c.decision.encode(decision))
    dump(
        "decision_status_event_v1",
        c.status_event.encode(status_event(decision, DecisionStatus.RECORDED)),
    )
    dump(
        "observed_outcome_v1",
        c.observed_outcome.encode(
            ObservedOutcome(
                decision_id=decision.decision_id,
                known_at=ts(days=8),
                realized={"lineup_points": 101.5},
            )
        ),
    )
    dump("observation_usage_snapshot_v1", c.observation(usage.kind).encode(usage))
    dump(
        "identity_mapping_event_v1",
        c.identity_event.encode(
            IdentityMappingEvent(
                ref=ProviderRef(
                    provider="sleeper", entity_type=EntityType.PLAYER, external_id="S-1"
                ),
                internal_id="plr_golden",
                status=MappingStatus.VERIFIED,
                method=MappingMethod.CURATED_CROSSWALK,
                confidence=1.0,
                asserted_by="golden",
            )
        ),
    )
    dump("model_artifact_v1", c.model_artifact.encode(fitted_artifact(ts())))


if __name__ == "__main__":
    main()
