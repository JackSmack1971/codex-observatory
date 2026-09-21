# Retention contract v1

Retention is bounded and class-specific: hot canonical data, raw metadata, forensic raw payloads, immutable historical exports, and optional aggregates have separate limits. `aggregate_days = 0` means indefinite retention.

Historical exports are immutable Parquet batches with a manifest registry and SHA-256 digest. Hot data may be deleted only after the archive manifest is verified and registered. Pruning follows referential order: archive-covered projections and child rows first, lifecycle rows only when inactive and unreferenced, and canonical events last. Open sessions, threads, and turns are never removed solely because their start time is old.

Phase 0 defines the contract only. No database, archive writer, or retention runner exists yet.
