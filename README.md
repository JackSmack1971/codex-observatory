# Codex Local Telemetry Observatory

Phase 1 adds the local OTLP/HTTP ingestion foundation: protobuf/JSON logs,
metrics, and traces are decoded, normalized into event.v1, persisted to SQLite,
and exposed through read-only health and evidence queries. App Server control,
hooks, Git enrichment, analytics, and Codex configuration mutation remain out
of scope.

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
