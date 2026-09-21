# Retention contract v1

Phase 7.1 defines selection and planning contracts only. It adds no `DELETE`,
archive-coverage verifier, executor, scheduler, checkpoint tuning, or `VACUUM`.
A plan is diagnostic data in memory and is not persisted or authorization to
mutate SQLite.

## Policy and cutoff semantics

`RetentionPolicyV1` has `enabled`, `hot_days`, `raw_days`, and one timezone-aware
`evaluation_time`. The evaluation time is normalized to UTC once and shared by
every table selection in a run; a planner must not read the clock per table.
The defaults are `enabled = false`, `hot_days = 30`, and `raw_days = 7`.
Disabled means no executable retention action may be taken.

For retention class `c`, `cutoff(c) = evaluation_time - period(c)`. A row is
eligible only when its normalized UTC timestamp is **strictly less than** the
cutoff. Equality is ineligible. Null or invalid timestamps are ineligible and
must make planning report a diagnostic rather than guess.

This preserves the Phase 0 defaults: normalized SQLite hot data is 30 days;
raw metadata and forensic raw payloads are each 7 days. The v1 `raw_days`
applies to both raw classes because their existing defaults are equal. The
existing 365-day historical Parquet and indefinite (`0`) aggregate settings do
not select SQLite rows and remain unchanged. The processing-spool limit remains
separate. A future desire to give metadata and forensic payloads different
periods requires a new policy version; v1 must not silently choose between the
two. The existing `run_interval_hours` is scheduler configuration and does not
alter cutoff calculation.

## Complete SQLite table classification

Every application table created by migrations 1--5 is classified exactly once.
SQLite's internal tables (for example `sqlite_sequence`) are not application
tables.

| Classification | Tables | Meaning |
|---|---|---|
| `PRUNABLE_HISTORY` | `raw_events`, `events`, `app_server_messages`, `app_server_token_usage`, `hook_events`, `correlation_edges`, `git_snapshots`, `git_snapshot_paths`, `git_snapshot_correlations` | Append-like evidence that a future version may prune only after all conditions below are met. |
| `CURRENT_PROJECTION` | `threads`, `turns`, `thread_items`, `repositories`, `worktrees` | Latest lifecycle/entity projection; v1 never age-prunes it. Closed-row compaction needs a later contract. |
| `CONTROL_STATE` | `schema_migrations`, `collector_health`, `app_server_state`, `hook_source_state`, `git_health`, `analytics_health` | Migration, source, and health state required to operate/interpret the store. |
| `ARCHIVE_CONTROL` | `archive_batches`, `archive_files`, `archive_watermarks`, `archive_health` | Archive proof, progress, and health; deleting it would destroy coverage evidence. |
| `AUDIT` | _none in migrations 1--5_ | Reserved for immutable retention/execution audit records in a later migration. |

## Prunable-history registry

“Required dataset” names the archive evidence that must cover each stable row
identity before deletion. Only `events`, `token_usage`, and `git_snapshots`
exist today. Every other named dataset is deliberately **not implemented**, so
those rows cannot yet satisfy coverage. Parent coverage never implies child
coverage. Periods below are defaults, not permission to delete.

| Table | Timestamp | Class / default | Stable row identity | Required archive dataset | Foreign-key implications |
|---|---|---|---|---|---|
| `raw_events` | `received_at` | raw / 7 days | (`ingest_id`) | `raw_events` (future) | Parent of `hook_events.ingest_id`; `duplicate_of` is not an FK. |
| `events` | `event_time` | hot / 30 days | (`event_id`) | `events` | Parent of both event columns in `correlation_edges` and of `hook_events.event_id`. `event_seq` is the archive watermark, not the stable coverage identity. |
| `app_server_messages` | `received_at` | raw / 7 days | (`message_id`) | `app_server_messages` (future) | Child of non-prunable `app_server_state`; no dependent child. |
| `app_server_token_usage` | `observed_at` | hot / 30 days | (`thread_id`, `turn_id`) | `token_usage` | Child of `app_server_state`; schema has no FK to `threads`/`turns`. Archive watermark uses SQLite `rowid`, not stable coverage identity. |
| `hook_events` | `received_at` | hot / 30 days | (`ingest_id`) | `hook_events` (future) | Child of `raw_events` and `events`; delete before either parent. |
| `correlation_edges` | `created_at` | hot / 30 days | (`edge_id`) | `correlation_edges` (future) | Both event references point to `events`; delete before either referenced event. |
| `git_snapshots` | `captured_at` | hot / 30 days | (`snapshot_observation_id`) | `git_snapshots` | Child of current `repositories` and `worktrees`; parent of paths and correlations. Archive watermark uses `rowid`, not stable coverage identity. |
| `git_snapshot_paths` | parent `git_snapshots.captured_at` | hot / 30 days | (`snapshot_observation_id`, `path`) | `git_snapshot_paths` (future) | Has no timestamp of its own; eligibility is inherited only from the covered parent. Delete before parent. |
| `git_snapshot_correlations` | `created_at` | hot / 30 days | (`correlation_id`) | `git_snapshot_correlations` (future) | Child of `git_snapshots`; delete before parent. |

All archive proof must identify a `PUBLISHED` batch, verified manifest and files,
and demonstrate membership of every stable identity in the selection. A source
watermark or min/max timestamp alone is insufficient proof. Rows sharing an
identity with no matching archive representation remain ineligible.

### Safe future deletion order

Within one transaction with `PRAGMA foreign_keys=ON`, the only safe partial
order is:

1. `git_snapshot_paths` and `git_snapshot_correlations` before `git_snapshots`;
2. `hook_events` before either `raw_events` or `events`;
3. `correlation_edges` before `events`;
4. `app_server_messages` and `app_server_token_usage` have no prunable children.

Independent entries may be interleaved, but each row needs its own timestamp
eligibility and archive coverage; deleting a child merely because its parent is
eligible is forbidden. No current projection, active lifecycle row, control,
archive-control, or audit row participates in v1 pruning.

## `RetentionPlanV1`

The in-memory plan contains schema version `1`, the exact policy (including the
single evaluation timestamp), ordered per-table selections, and the declared
deletion order. Each selection contains table, retention class, timestamp
column, UTC cutoff, ordered tuples of stable row identities, required archive
dataset, and covering published batch IDs. Empty selections are explicit. A
future planner must produce stable ordering by table deletion order and then by
the documented identity columns. The structure contains no SQL and has no
`execute`, `apply`, `delete`, checkpoint, or vacuum operation.

## SQLite operational facts (design constraints)

The following are based on current official SQLite documentation:

* [`DELETE` and transactions](https://www.sqlite.org/lang_transaction.html):
  SQLite automatically starts a transaction when needed; an explicit
  transaction groups the future child-before-parent changes atomically. A
  write transaction is exclusive among writers, and rollback must preserve all
  selected rows on failure.
* [Foreign keys](https://www.sqlite.org/foreignkeys.html): enforcement is a
  per-connection setting and must be enabled outside a transaction. The future
  runner must verify `PRAGMA foreign_keys=ON`; this schema does not specify
  cascading deletes.
* [WAL](https://www.sqlite.org/wal.html): commits append to the WAL while readers
  can continue against older snapshots. Checkpointing transfers WAL content
  toward the database file and can be limited by concurrent readers.
* [Freelist/page reuse](https://www.sqlite.org/fileformat2.html#freelist): pages
  made unused by deletion normally join the database freelist for later reuse.
  Therefore **`DELETE` does not imply physical database-file shrink**.
* [`VACUUM`](https://www.sqlite.org/lang_vacuum.html): rebuilds the database and
  can reclaim free pages, but needs additional disk space and changes rowids for
  tables without an explicit integer primary key. It is out of scope.
* [`wal_checkpoint`](https://www.sqlite.org/pragma.html#pragma_wal_checkpoint)
  moves frames/checkpoints WAL state; it does not rebuild the main database.
  Therefore **checkpoint is not `VACUUM`**.
* [`secure_delete`](https://www.sqlite.org/pragma.html#pragma_secure_delete):
  ordinary deletion can leave recoverable content in free space, WAL, journals,
  or shadow tables. `VACUUM` is a compaction/rebuild operation, not a guarantee
  that all historical copies on the storage medium are securely erased.
  Therefore **`VACUUM` is not secure erase**.

Consequently physical shrink, checkpoint policy, and media sanitization must be
separate future decisions; none is implied by this retention model.
