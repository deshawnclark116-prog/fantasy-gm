# Foundation v0.1 — self-review: weaknesses, leakage risks, open questions

Written by the implementation engineer for the architecture review. Ordered roughly by risk.

## A. Leakage / honesty risks

1. **The ledger only verifies declared evidence.** `Decision` rejects evidence whose
   `observed_at` is after the cutoff, but it cannot prove that the engine used *only* declared
   evidence. The structural mitigation is that stores require a `KnowledgeCutoff`, but a future
   engine could still read a cache, a model trained on later data, or the clock. Proposal:
   decision engines receive a cutoff-bound read handle only, and every model artefact carries
   a `trained_through` timestamp that must be `<= information_cutoff`.
2. **`DecisionEvidence.observed_at` is caller-supplied.** It isn't cross-checked against the
   store (for example against `ingested_at` in SYSTEM_KNOWLEDGE mode). A reference-resolving
   validator at record time would close this.
3. **`created_at` is caller-supplied.** Nothing stops a backdated live decision. Proposal: the
   ledger stamps a server-side `recorded_at`, and live decisions must satisfy
   `information_cutoff <= recorded_at` with bounded skew.
4. **PUBLIC_AVAILABILITY depends on provider timestamps.** Backtests are only as honest as each
   provider's publication times. Many feeds expose only "last updated", which is wrong for
   corrections. We need a per-provider timestamp-trust classification, and possibly a
   conservative publication-lag model for backfills.
5. **Future-effective facts are dropped.** `effective_by` excludes known facts that take
   effect later (for example a signing announced before its effective date). This is safe,
   but it may throw away legitimate intent signals.
6. **The model trained-through date is not yet represented anywhere.** See A1.

## B. Hard-coded / premature assumptions

1. **All Organizational Intent v0 numbers are unvalidated.** This includes the log-pick
   transform, half-lives, depth-source reliabilities, primary-role counts (WR=3), full-role
   usage references, `k=4` pseudo-games, the regime discount and the conflict threshold. They
   are centralised and hashed, but they shape outputs.
2. **Component scores live on heterogeneous scales.** Draft uses a log-pick rank, usage uses
   share ÷ reference, and competition uses a rank ratio. Conflict detection compares their
   weighted mean with usage, which is a heuristic, not a calibrated comparison.
3. **Usage counts only the current team.** A traded player's history with the old team is
   ignored for intent. That is arguably correct for "organisational intent", but player-level
   role evidence must come from the player-state/projection layer.
4. **Injury-shortened games are not detected.** A game the player left in the 1st quarter
   drags the share down. A game-participation / early-exit flag is needed on `UsageSnapshot`.
5. **Recency is weighted by game index, not calendar time or snaps.** Byes, injuries and
   season boundaries are all treated as one step apart. Season boundaries get an extra flat
   discount.
6. **`Player.positions` is static.** Position changes (such as a WR converted to RB) and
   platform-specific eligibility aren't time-varying. Eligibility is passed per league to
   roster validation, but no league-eligibility observation type exists yet.
7. **`League` is static.** Commissioners change settings mid-season. It should probably become
   a versioned observation like `FantasyRoster`.
8. **`StatKey` is a closed enum.** Exotic league stats need an enum addition, which is
   deliberate: unknown stats fail loudly. Scoring uses `Decimal` while simulation uses
   `float`.
9. **Competition uses the primary position only.** Multi-position players and
   slot-vs-outside WR roles aren't modelled.
10. **Coaching identity is an opaque `coach_id`** with no provider mapping or scheme
    descriptors yet (offensive system, personnel tendencies).

## C. Engineering gaps

1. **Schema evolution of ledger payloads.** Stored decisions are re-validated with the
   *current* `Decision` model on read. Tightening a validator later could make historical
   decisions unloadable. Proposal: add a `schema_version` to each payload and keep
   versioned readers. Hash verification already runs on stored bytes, so it is not coupled
   to re-serialisation.
2. **No idempotency key on provider writes.** A timeout followed by a retry could
   double-execute a waiver claim or trade. The execution service needs a per-decision
   idempotency token passed to providers, plus reconciliation against `transactions()`.
3. **Provider protocols are synchronous.** This is fine for batch ingestion, but live draft
   rooms and game-day inactives may want async and streaming. This is an open question.
4. **The SQL as-of query resolves revisions in Python** after an indexed pre-filter. This
   needs a SQL window-function implementation, or point-in-time materialisations, at scale.
5. **Concurrent status appends** are serialised only by the `(decision_id, seq)` unique
   constraint. The loser gets an IntegrityError, not a friendly error.
6. **The autonomy policy in force is not recorded on the decision.** Only `autonomy_mode` is
   recorded. The policy hash and routing reasons should be persisted with the initial status
   event.
7. **Grading assumes one scalar metric per decision** plus optional observed alternatives.
   Counterfactuals for trades and waivers (the value of the path not taken) are not
   observable and need simulation-based grading, clearly labelled as such.
8. **Simulation reproducibility is pinned to the numpy version**, which is recorded but not
   enforced. The reference samplers assume independence between players; there are no
   stacks, game script or weather correlation.
9. **`ConfidenceAssessment.score` is caller-defined** and not yet calibrated. The autonomy
   threshold (0.8) therefore has no empirical meaning yet.

## D. Questions for the architect

1. Should organisational intent eventually feed projections as a *prior on role share*? That
   would give it a natural common scale and make conflict detection principled. Or should it
   stay a separate explanatory layer?
2. Which usage metric should be the primary role signal per position (route participation vs
   targets-per-route-run vs snap share for WR/TE; opportunity share vs snap share for RB)?
   Which vendor can supply routes and first-read targets with honest timestamps?
3. How should we treat preseason for rookies versus veterans (starters resting)? Is
   first-team snap share obtainable at all from affordable sources?
4. Is `SYSTEM_KNOWLEDGE` the right default for *every* live decision, including ones made in
   the minutes after news breaks when ingestion lags?
5. Which decision types should never be eligible for AUTONOMOUS (trades? drops of rostered
   starters?) regardless of per-league policy?
6. Should `League` settings and platform eligibility become observations now, before
   ingestion is built?
