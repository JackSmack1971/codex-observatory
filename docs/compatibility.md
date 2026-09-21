# Compatibility contract v1

Compatibility is versioned evidence, not a best-effort guess. The registry under `compatibility/codex/` will map a tested Codex version to captured App Server JSON/TypeScript schemas and fixtures. A cross-source alias is valid only when a live test establishes field equivalence for that exact version.

Unknown Codex versions default to `warn`; deployments may choose `fail`. When `require_generated_app_server_schema = true`, missing generated schema evidence is a visible compatibility issue. Schema capture and verification are later CLI commands and are intentionally unimplemented in Phase 0.

The v1 runtime target is Python `>=3.14,<3.15` and Node 24.x for frontend development. The current bootstrap environment must report any mismatch rather than hiding it.
