# Retention contract v1

Retention is bounded and class-specific: hot canonical data, raw metadata, forensic raw payloads, immutable historical exports, and optional aggregates have separate limits. `aggregate_days = 0` means indefinite retention.

Historical exports are immutable Parquet batches with a manifest registry and SHA-256 digest. Hot data may be deleted only after the archive manifest is verified and registered. Pruning follows referential order: archive-covered projections and child rows first, lifecycle rows only when inactive and unreferenced, and canonical events last. Open sessions, threads, and turns are never removed solely because their start time is old.

## Phase 7.1 planning contract

Phase 7.1 defines planning contracts only. It does not add a retention runner,
archive verification, deletion capability, or a schema migration.

`src/codex_observatory/retention.py` is the authoritative, machine-readable
table-classification registry. The table below is generated from that registry
and is guarded by a consistency test. SQLite-internal tables (currently
`sqlite_sequence`) are intentionally excluded; `schema_migrations` is application
audit state and is included.

<!-- TABLE_CLASSIFICATIONS:START -->
| Table | Classification |
| --- | --- |
| `schema_migrations` | `AUDIT` |
| `raw_events` | `PRUNABLE_HISTORY` |
| `events` | `PRUNABLE_HISTORY` |
| `collector_health` | `CONTROL_STATE` |
| `app_server_state` | `CONTROL_STATE` |
| `app_server_messages` | `PRUNABLE_HISTORY` |
| `threads` | `CURRENT_PROJECTION` |
| `turns` | `CURRENT_PROJECTION` |
| `thread_items` | `CURRENT_PROJECTION` |
| `app_server_token_usage` | `CURRENT_PROJECTION` |
| `hook_events` | `PRUNABLE_HISTORY` |
| `hook_source_state` | `CONTROL_STATE` |
| `correlation_edges` | `PRUNABLE_HISTORY` |
| `repositories` | `CURRENT_PROJECTION` |
| `worktrees` | `CURRENT_PROJECTION` |
| `git_snapshots` | `PRUNABLE_HISTORY` |
| `git_snapshot_paths` | `PRUNABLE_HISTORY` |
| `git_snapshot_correlations` | `PRUNABLE_HISTORY` |
| `git_health` | `CONTROL_STATE` |
| `archive_batches` | `ARCHIVE_CONTROL` |
| `archive_files` | `ARCHIVE_CONTROL` |
| `archive_watermarks` | `ARCHIVE_CONTROL` |
| `archive_health` | `ARCHIVE_CONTROL` |
| `analytics_health` | `CONTROL_STATE` |
| `retention_runs` | `AUDIT` |
| `retention_run_tables` | `AUDIT` |
<!-- TABLE_CLASSIFICATIONS:END -->

`app_server_token_usage` is updated in place for a `(thread_id, turn_id)` and
therefore represents current/latest cumulative token-usage state. It is not
age-prunable under Retention Contract v1, even though a historical archive
dataset currently exports related token evidence. Immutable token-history
retention is a `FUTURE_PHASE` concern requiring a separate schema and ingestion
contract; this contract does not redesign token ingestion.

### Timestamp validation and diagnostics

`RetentionPolicyV1.eligible` operates only on already validated, timezone-aware
`datetime` values and deterministically rejects other input. Timestamp
extraction, parsing, and null handling belong to the future planner. The planner
must not coerce or guess malformed or null timestamps: it must classify those
rows as ineligible and add a `RetentionDiagnosticV1` to the `RetentionPlanV1`,
preserving the table, timestamp column, row identity, and reason.
