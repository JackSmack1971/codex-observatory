# Codex Local Telemetry Observatory

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

See [the Phase 1 OTLP notes](docs/phase-1-otlp.md) for protocol behavior,
privacy, duplicate handling, schema, and authoritative compatibility sources.
