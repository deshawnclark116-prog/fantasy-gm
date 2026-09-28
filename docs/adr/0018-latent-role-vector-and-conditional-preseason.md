# ADR 0018 — Latent role vector; conditional preseason evidence

**Status:** accepted (v0.1.1). Amends ADR 0005.

## Decision
* There is no single "primary usage metric". `RoleDimension` enumerates position-specific
  latent dimensions, grouped into families:
  * deployment, target earning and rushing;
  * situational, pass protection and environment;
  * alignment. Its dimensions (slot rate, personnel role) are reserved as
    `NOT_YET_SUPPORTED`.
* `observed_role_vector` computes an **empirical**, recency-weighted pooled ratio for each
  dimension. It is a measurement, not a projection.
  * A game missing inputs is `UNOBSERVED` for that dimension. It is never counted as zero.
  * For WR/TE, route participation (deployment) and targets per route (target earning) are
    separate dimensions.
* Organizational intent stays component evidence, with no aggregate score. Each
  `ComponentSignal.informs` lists the role dimensions it is a prior or covariate for.
  `ACTUAL_USAGE` scores only *deployment* dimensions. Target-earning values are reported in
  its measurements but never scored as organisational intent.
* Preseason evidence is conditional:
  * A veteran's games in which starters were rested are excluded, so non-usage is not
    negative evidence.
  * A missing rest/first-team context lowers confidence by a source-reliability factor.
  * Unknown experience (no draft record) lowers confidence.
  * Total preseason snap share is never used as a proxy.
