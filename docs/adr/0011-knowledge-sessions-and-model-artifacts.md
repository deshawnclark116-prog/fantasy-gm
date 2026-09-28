# ADR 0011 — Knowledge sessions, evidence manifests and model-artifact provenance

**Status:** accepted (v0.1.1)

## Decision
* Decision engines receive a `KnowledgeSession`, not a store. It is bound to a `RunContext`
  and a `KnowledgeCutoff`.
  * Every `read()` is cutoff-filtered and re-checked with `assert_known` (defence against a
    faulty store).
  * Every read is automatically recorded in an evidence manifest: stable id, content hash,
    `known_at`, `ingested_at`, `effective_at`, provider and timestamp quality.
  * Models are registered with `use_model()`. Results computed inside the session are
    registered with `record_derived()`, which must cite evidence the session actually read.
  * `evidence_ids_for()` refuses observations that the session did not read.
  * `seal()` freezes the manifest, after which reads raise.
* `build_decision(session, ...)` registers the decision artifact, seals the session, and
  copies the cutoff, knowledge mode and run into the immutable `Decision`.
* `Decision` validation re-checks the manifest against the cutoff (defence in depth for
  hand-built manifests). It also requires the decision artifact to be declared in the
  manifest.
* `ModelArtifactManifest` records:
  * name and version, and code version;
  * `fit_kind` (`FITTED` or `HAND_SPECIFIED`) and `trained_through`;
  * the training-data manifest hash, and the feature schema version and hash;
  * the calibration artifact hash and version;
  * the config hash and a runtime fingerprint.

  Its identity is its content hash. A `FITTED` artifact with `trained_through > cutoff` is
  refused by the session and invalidates a manifest.
* `HAND_SPECIFIED` artifacts (such as Organizational Intent v0) have no training window. They
  are allowed in replays but reported as `hand_specified_artifacts`, because the author's own
  knowledge is a residual leakage risk.
