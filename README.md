# Codex Local Telemetry Observatory

Phase 5 adds an immutable, read-only historical archive. SQLite remains the
live evidence store; `archive run` exports canonical `events`, app-server
`token_usage`, and Git `git_snapshots` to explicitly typed, Zstandard-compressed
Parquet files under `year=YYYY/month=MM` Hive partitions. Published files are
trusted only when registered in a verified immutable manifest. DuckDB queries
the registered files in process; it is not a second mutable source of truth.

Phase 4 adds read-only Git state evidence on top of the existing observatory.
Git identity, worktree state, HEAD, status paths, diff statistics, and health
are persisted in SQLite without storing full diffs. See
[docs/phase-4-git.md](docs/phase-4-git.md).

Phase 2 adds a read-only Codex App Server adapter on top of the Phase 1 local
OTLP/HTTP ingestion foundation: protobuf/JSON logs,
metrics, and traces are decoded, normalized into event.v1, persisted to SQLite,
and exposed through read-only health and evidence queries. App Server
initialization, stable thread reconciliation, lifecycle normalization, and
source health are persisted in migration 2. App Server control,
hooks, Git enrichment, analytics, and Codex configuration mutation remain out
of scope. See [docs/phase-2-app-server.md](docs/phase-2-app-server.md).

## Development

```text
uv sync --locked
uv run codex-observatory doctor
npm ci --prefix frontend
npm run build --prefix frontend
```

The v1 server contract is loopback-only (`127.0.0.1`). Runtime paths are resolved with `platformdirs`; no runtime database or data directory is stored in this repository.

Explicit Git capture and query:

```text
uv run codex-observatory git-capture --cwd .
uv run codex-observatory git-query
```

Archive and fixed analytics queries:

```text
uv run codex-observatory archive run
uv run codex-observatory archive verify
uv run codex-observatory analytics event-count
uv run codex-observatory analytics events-by-source
```

See [the Phase 1 OTLP notes](docs/phase-1-otlp.md) for protocol behavior,
privacy, duplicate handling, schema, and authoritative compatibility sources.
