# Codex Local Telemetry Observatory

Phase 0 establishes the repository, locked development environments, privacy and compatibility contracts, and configuration/doctor skeleton. Collectors, durable storage, product views, App Server control, and Codex configuration mutation are intentionally not implemented yet.

## Development

```text
uv sync --locked
uv run codex-observatory doctor
npm ci --prefix frontend
npm run build --prefix frontend
```

The v1 server contract is loopback-only (`127.0.0.1`). Runtime paths are resolved with `platformdirs`; no runtime database or data directory is stored in this repository.
