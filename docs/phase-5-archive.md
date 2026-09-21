# Phase 5 archive and analytics

Phase 5 is historical storage only. It does not delete SQLite rows, prune
retention, expose arbitrary SQL, create dashboard views, or persist a DuckDB
catalog.

## Contract

SQLite is authoritative live evidence. A dataset export reads rows after its
stored SQLite high-watermark (`events.event_seq`, or the source table's
SQLite `rowid` for the existing token-usage and Git snapshot projections).
The inclusive selection is `(previous_watermark, current_watermark]`. A
no-new-row export is a no-op. The watermark is advanced in the same SQLite
transaction that publishes the batch registry entries.

Datasets are deliberately small and normalized:

* `events` preserves canonical IDs, event/observation timestamps, source and
  adapter provenance, correlation IDs, category/name/status, and sanitized
  canonical JSON attributes. Hook tool and approval evidence remains here
  because those are canonical event facts, not separate SQLite tables.
* `token_usage` preserves app-server thread/turn identity, observation time,
  sanitized usage JSON, and extracted token totals.
* `git_snapshots` preserves repository/worktree IDs, Git state, timestamps,
  counts, and evidence digests. Path evidence remains available in SQLite for
  later expansion without inventing a new archive authority.

All datasets use explicit Parquet types. IDs and JSON are `VARCHAR`, counts
are `BIGINT`, booleans are `BOOLEAN`, and timestamps are Parquet `TIMESTAMP`
values interpreted as UTC. Missing values remain `NULL`. Files use DuckDB's
stable `COPY ... FORMAT parquet` writer with Zstandard compression and default
row-group sizing. Hive partitions are only `year` and `month`, so source,
repository, thread, and session remain columns rather than tiny partitions.

Each published batch has a UUID batch ID, UUID file names, a manifest at
`manifests/<dataset>/<batch_id>.json`, whole-file SHA-256 digests, file byte
sizes, row counts, and min/max timestamps. Temporary output is written under
`.staging`, verified with DuckDB, and atomically moved into the archive. A
manifest and registered file are immutable; stray Parquet files are ignored.

Schema versions are stored in both manifest and registry. Additive compatible
files can be read with DuckDB `union_by_name`; incompatible semantic changes
must use a new schema version and are not coerced or rewritten in place.

## Health and CLI

Archive-write health and DuckDB-query health are separate SQLite records.
`doctor` reports `ARCHIVE_EMPTY`, `ARCHIVE_HEALTHY`, `ARCHIVE_DEGRADED`, or
`ARCHIVE_FAILED`, and independently reports `DUCKDB_AVAILABLE`,
`DUCKDB_QUERY_HEALTHY`, or `DUCKDB_QUERY_FAILED`. An empty archive before the
first export is normal.

The supported analytics surface is fixed backend code: event counts, event
groupings by source/category/repository, token totals, and tool-event
success/status counts. There is no browser or user-provided SQL surface.

## Documentation decisions

The implementation follows the current official [DuckDB Parquet
documentation](https://duckdb.org/docs/current/data/parquet/overview),
[partitioned-write documentation](https://duckdb.org/docs/current/data/partitioning/partitioned_writes),
[Parquet tips](https://duckdb.org/docs/current/data/parquet/tips), and
[concurrency model](https://duckdb.org/docs/current/connect/concurrency).
DuckDB 1.5.5 supports direct Parquet reads, filter/projection pushdown,
`union_by_name`, filename metadata, UUID filename patterns, `APPEND`, Hive
partitioning, Zstandard, and configurable row groups; its multi-process native
database writer model is intentionally not used here.

The [Apache Parquet concepts](https://parquet.apache.org/docs/concepts/),
[metadata](https://parquet.apache.org/docs/file-format/metadata/), and
[logical types](https://parquet.apache.org/docs/file-format/types/logicaltypes/)
specification explains the file/footer/row-group structure and why this phase
uses explicit logical types and immutable files rather than inferred schemas.
