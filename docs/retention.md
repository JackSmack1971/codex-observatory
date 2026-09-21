# Retention contract v1

Retention is bounded and class-specific: hot canonical data, raw metadata, forensic raw payloads, immutable historical exports, and optional aggregates have separate limits. `aggregate_days = 0` means indefinite retention.

Historical exports are immutable Parquet batches with a manifest registry and SHA-256 digest. Hot data may be deleted only after the archive manifest is verified and registered. Pruning follows referential order: archive-covered projections and child rows first, lifecycle rows only when inactive and unreferenced, and canonical events last. Open sessions, threads, and turns are never removed solely because their start time is old.

## Phase 7.1 planning contract

Phase 7.1 defines planning contracts only. It does not add a retention runner,
archive verification, deletion capability, or a schema migration.

## Phase 7.2 durable audit state

Phase 7.2 adds migration 6 and only the durable planning and execution evidence
stored in `retention_runs` and `retention_run_tables`. It does not add a
retention runner, archive coverage verification, deletion capability, hot/cold
query changes, vacuuming, scheduling, or dashboard mutation.

`src/codex_observatory/retention.py` is the authoritative, machine-readable
table-classification registry. The table below is generated from that registry
and is guarded by a consistency test. SQLite-internal tables (currently
`sqlite_sequence`) are intentionally excluded; `schema_migrations` is application
audit state and is included.

## Phase 7.3 dry-run planning

`codex-observatory retention plan` captures one UTC evaluation time, resolves
the configured hot and raw cutoffs, records every eligible row identity, and
persists a `PLANNED` audit run. Planning never verifies archive coverage and
never deletes data: coverage is reported as `NOT_YET_VERIFIED`, and planned
deletions remain zero. A timestamp exactly equal to its cutoff is ineligible.
Candidate selection and audit persistence occur in one `BEGIN IMMEDIATE`
transaction, so all tables are evaluated from one database snapshot. Composite
primary keys are recorded as canonical JSON objects rather than delimiter-joined
text. The optional `--config` argument accepts a complete observatory TOML file,
including a `[retention]` table as shown by `config/retention.example.toml`.

## Phase 7.4 archive-coverage proof

Planning now proves archive coverage for each eligible candidate without
deleting live data. A candidate is `COVERED` only when its stable identity is
read from a Phase 5 Parquet file in a `PUBLISHED` batch after re-verifying the
registered manifest path and digest, manifest dataset and canonical schema,
registered file metadata, safe in-root Parquet path, file SHA-256, readable
Parquet schema, row count, and identity column. A published registry row by
itself is never coverage.

Verification also requires the manifest and registry to agree on the complete
unique file-ID set, file sizes, file and row totals, and batch aggregates. The
reader streams identities in bounded chunks and retains only identities that
are candidates in the current plan. If no archive root is configured, the
proof has not run and the plan is `BLOCKED`, including when there are no
candidates.

Canonical events map by `event_id`, and Git snapshots map by
`snapshot_observation_id`. Tables without evidence carrying a safe Phase 5
identity are `UNCOVERED`; the planner does not infer a relationship. A missing
identity in otherwise valid evidence is also `UNCOVERED`. If relevant
published evidence cannot be verified, an otherwise unmatched candidate is
`BLOCKED`. Covered identities remain usable even when a separate batch is
invalid.

Only `COVERED` candidates enter `planned_delete_count` (`would_delete` in CLI
output), and that exact decision is stored on each candidate as
`planned_delete`. `UNCOVERED` and `BLOCKED` candidates never do. A covered Git
snapshot with path or correlation children is also excluded because Phase 5
does not preserve those child identities and the SQLite foreign keys prohibit
deleting their parent. A plan is `VERIFIED`
only after this proof has run and its proposed set consists solely of covered
rows; evidence failures make the plan `BLOCKED`. `VERIFIED` remains a dry-run
state: Phase 7.4 contains no `DELETE`, hot/cold query change, or `VACUUM`.

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
| `retention_run_candidates` | `AUDIT` |
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
