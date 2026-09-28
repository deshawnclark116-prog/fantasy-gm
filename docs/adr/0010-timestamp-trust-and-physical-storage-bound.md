# ADR 0010 — Timestamp trust and the physical storage bound on knowledge

**Status:** accepted (v0.1.1). Amends ADR 0002.

## Decision
* `SourceRef.timestamp_quality` records what `observed_at` actually means. The provider's
  original timestamp is preserved verbatim in `raw_timestamp` and `raw_timestamp_field`.

  | Quality | Meaning |
  |---|---|
  | `EXACT_PUBLICATION_TIME` | The provider states when the content was published. |
  | `LAST_UPDATED_ONLY` | Only a last-modified time is known. |
  | `PROVIDER_EVENT_TIME` | The time of the event, not of publication. Unsafe. |
  | `INGESTION_TIME_ONLY` | Only our fetch time is known. |
  | `UNKNOWN` | The provider's timestamp semantics are unknown. |

* **No fabrication.** For `INGESTION_TIME_ONLY` and `UNKNOWN`, `observed_at` must equal
  `ingested_at`. In every case `observed_at` may be at most 5 minutes after `ingested_at`
  (clock-skew tolerance).
* `TimestampPolicy` on `KnowledgeCutoff` applies to PUBLIC_AVAILABILITY runs:
  * It sets which qualities are accepted. The default is exact, last-updated and
    ingestion-time.
  * Evidence that fails the policy is either `EXCLUDE`d or `FLAG`ged. A flag appears as
    `timestamp_suspect` in the evidence manifest.
* **Physical storage bound.** Stores stamp their own `recorded_at` when they write a row.
  * Under SYSTEM_KNOWLEDGE a record is knowable only if it was also physically stored by the
    cutoff. A caller-supplied, possibly backdated `ingested_at` therefore cannot leak backfill
    into a system-knowledge replay.
  * `KnowledgeCutoff.stored_by` pins PUBLIC_AVAILABILITY experiments to a store snapshot, so
    they stay repeatable as backfill arrives.
