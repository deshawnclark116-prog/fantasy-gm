"""Build decisions from a knowledge session so evidence is declared by construction."""

from __future__ import annotations

from datetime import datetime

from fantasy_gm.domain.artifacts import ModelArtifactManifest
from fantasy_gm.domain.autonomy import AutonomyMode, DecisionType
from fantasy_gm.domain.decision import ConfidenceAssessment, Decision, DecisionCandidate
from fantasy_gm.domain.ids import CandidateId, FantasyTeamId, LeagueId
from fantasy_gm.domain.seeds import SeedSpec
from fantasy_gm.knowledge.session import KnowledgeSession


def build_decision(
    session: KnowledgeSession,
    *,
    league_id: LeagueId,
    fantasy_team_id: FantasyTeamId,
    decision_type: DecisionType,
    candidates: tuple[DecisionCandidate, ...],
    selected_candidate_id: CandidateId,
    engine_versions: dict[str, str],
    decision_artifact: ModelArtifactManifest,
    regime: str,
    confidence: ConfidenceAssessment,
    autonomy_mode: AutonomyMode,
    decision_time: datetime | None = None,
    simulation_seed: SeedSpec | None = None,
    notes: str = "",
) -> Decision:
    """Registers the decision artifact, seals the session and returns the immutable decision.

    ``decision_time`` defaults to the session clock for LIVE/PAPER runs and to the cutoff for
    replays. The information cutoff and knowledge mode always come from the session.
    """
    session.use_model(decision_artifact)
    manifest = session.seal()
    if decision_time is None:
        decision_time = session.clock.now() if session.run.is_real_time else session.cutoff.as_of
    return Decision(
        league_id=league_id,
        fantasy_team_id=fantasy_team_id,
        decision_type=decision_type,
        run=session.run,
        decision_time=decision_time,
        information_cutoff=session.cutoff.as_of,
        candidates=candidates,
        selected_candidate_id=selected_candidate_id,
        engine_versions=engine_versions,
        decision_artifact_hash=decision_artifact.artifact_hash,
        regime=regime,
        evidence=manifest,
        confidence=confidence,
        autonomy_mode=autonomy_mode,
        simulation_seed=simulation_seed,
        notes=notes,
    )
