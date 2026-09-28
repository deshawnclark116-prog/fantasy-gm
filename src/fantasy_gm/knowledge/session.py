"""KnowledgeSession: the only read interface decision engines receive (ADR 0008).

A session is bound at construction to a run context and a knowledge cutoff. Every read is
filtered by the cutoff *and* automatically recorded in the evidence manifest (stable reference
id, content hash, knowability timestamps, timestamp quality). Model artifacts and derived
results must also be registered through the session. ``seal()`` freezes the manifest; the
session then refuses further reads, so nothing can be consulted after the evidence is declared.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from datetime import timedelta

from fantasy_gm.domain.artifacts import FitKind, ModelArtifactManifest
from fantasy_gm.domain.base import DomainModel
from fantasy_gm.domain.clock import Clock
from fantasy_gm.domain.evidence import EvidenceEntry, EvidenceKind, EvidenceManifest, evidence_id
from fantasy_gm.domain.knowledge import (
    LeakageError,
    assert_known,
    effective_by,
    timestamp_trusted,
)
from fantasy_gm.domain.observation import Observation
from fantasy_gm.domain.run_context import RunContext, RunMode
from fantasy_gm.domain.time import KnowledgeCutoff
from fantasy_gm.player_state.store import ObservationQuery, ObservationStore


class SessionSealedError(RuntimeError):
    pass


class SessionConfigurationError(ValueError):
    pass


class KnowledgeSession:
    def __init__(
        self,
        run: RunContext,
        cutoff: KnowledgeCutoff,
        store: ObservationStore,
        clock: Clock,
        *,
        max_live_cutoff_skew: timedelta = timedelta(seconds=30),
        allow_hand_specified_in_replay: bool = True,
    ) -> None:
        if cutoff.mode is not run.knowledge_mode:
            raise SessionConfigurationError("cutoff knowledge mode must match the run context")
        now = clock.now()
        if run.is_real_time and cutoff.as_of > now + max_live_cutoff_skew:
            raise SessionConfigurationError(
                f"{run.mode} session cutoff {cutoff.as_of.isoformat()} is in the future"
            )
        if run.mode is RunMode.REPLAY and cutoff.as_of > now:
            raise SessionConfigurationError("a replay cannot be cut off in the future")
        self._run = run
        self._cutoff = cutoff
        self.__store = store  # name-mangled: engines never get the raw store
        self._clock = clock
        self._allow_hand_specified = allow_hand_specified_in_replay
        self._entries: dict[str, EvidenceEntry] = {}
        self._sealed: EvidenceManifest | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ properties

    @property
    def run(self) -> RunContext:
        return self._run

    @property
    def cutoff(self) -> KnowledgeCutoff:
        return self._cutoff

    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def sealed(self) -> bool:
        return self._sealed is not None

    # ------------------------------------------------------------------ reads

    def read(
        self, query: ObservationQuery, *, include_future_effective: bool = False
    ) -> list[Observation]:
        self._ensure_open()
        found = self.__store.known_as_of(self._cutoff, query)
        assert_known(found, self._cutoff)  # defence in depth against a faulty store
        if not include_future_effective:
            found = effective_by(found, self._cutoff.as_of)
        for obs in found:
            self._record_observation(obs)
        return found

    def evidence_ids_for(self, observations: Iterable[Observation]) -> tuple[str, ...]:
        ids = tuple(evidence_id(EvidenceKind.OBSERVATION, o.observation_id) for o in observations)
        missing = [i for i in ids if i not in self._entries]
        if missing:
            raise LeakageError(f"observations not read through this session: {missing}")
        return ids

    # ------------------------------------------------------------------ artifacts & derived

    def use_model(self, artifact: ModelArtifactManifest) -> str:
        self._ensure_open()
        as_of = self._cutoff.as_of
        if artifact.fit_kind is FitKind.FITTED:
            assert artifact.trained_through is not None
            if artifact.trained_through > as_of:
                raise LeakageError(
                    f"artifact {artifact.name}@{artifact.version} trained through "
                    f"{artifact.trained_through.isoformat()} > cutoff {as_of.isoformat()}"
                )
        elif self._run.mode is RunMode.REPLAY and not self._allow_hand_specified:
            raise LeakageError("hand-specified artifacts are not allowed in this replay")
        entry = EvidenceEntry(
            evidence_id=evidence_id(EvidenceKind.MODEL_ARTIFACT, artifact.artifact_hash),
            kind=EvidenceKind.MODEL_ARTIFACT,
            reference_type="model_artifact",
            reference_id=artifact.artifact_hash,
            reference_hash=artifact.artifact_hash,
            fit_kind=artifact.fit_kind,
            trained_through=artifact.trained_through,
            summary=f"{artifact.name}@{artifact.version}",
        )
        self._put(entry)
        return entry.evidence_id

    def record_derived(
        self,
        reference_type: str,
        value: DomainModel,
        reference_id: str,
        derived_from: Iterable[str],
        summary: str = "",
        kind: EvidenceKind = EvidenceKind.DERIVED,
    ) -> str:
        """Register a result computed inside the session (intent snapshot, league state, ...)."""
        self._ensure_open()
        sources = tuple(dict.fromkeys(derived_from))
        unknown = [s for s in sources if s not in self._entries]
        if unknown:
            raise LeakageError(f"derived result cites evidence not read in this session: {unknown}")
        entry = EvidenceEntry(
            evidence_id=evidence_id(kind, reference_id),
            kind=kind,
            reference_type=reference_type,
            reference_id=reference_id,
            reference_hash=value.content_hash(),
            known_at=self._cutoff.as_of,
            derived_from=sources,
            summary=summary,
        )
        self._put(entry)
        return entry.evidence_id

    # ------------------------------------------------------------------ sealing

    def seal(self) -> EvidenceManifest:
        with self._lock:
            if self._sealed is None:
                self._sealed = EvidenceManifest(
                    run_id=self._run.run_id,
                    run_mode=self._run.mode,
                    cutoff=self._cutoff,
                    sealed_at=self._clock.now(),
                    entries=tuple(self._entries.values()),
                )
            return self._sealed

    # ------------------------------------------------------------------ internals

    def _ensure_open(self) -> None:
        if self._sealed is not None:
            raise SessionSealedError("session is sealed; open a new session to read more")

    def _record_observation(self, obs: Observation) -> None:
        self._put(
            EvidenceEntry(
                evidence_id=evidence_id(EvidenceKind.OBSERVATION, obs.observation_id),
                kind=EvidenceKind.OBSERVATION,
                reference_type=obs.kind,
                reference_id=obs.observation_id,
                reference_hash=obs.content_hash(),
                known_at=obs.observed_at,
                ingested_at=obs.source.ingested_at,
                effective_at=obs.effective_at,
                provider=obs.source.provider,
                timestamp_quality=obs.source.timestamp_quality,
                timestamp_suspect=not timestamp_trusted(obs, self._cutoff),
            )
        )

    def _put(self, entry: EvidenceEntry) -> None:
        with self._lock:
            existing = self._entries.get(entry.evidence_id)
            if existing is not None and existing.reference_hash != entry.reference_hash:
                raise LeakageError(f"evidence {entry.evidence_id} changed within one session")
            self._entries[entry.evidence_id] = entry
