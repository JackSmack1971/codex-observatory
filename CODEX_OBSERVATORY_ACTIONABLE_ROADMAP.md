# Codex Local Telemetry Observatory
## Actionable Implementation Roadmap — Empty Repository Edition

**Status:** implementation contract  
**Research baseline:** 2026-09-20  
**Target:** local-first, Windows-first, cross-platform Codex telemetry/governance observatory  
**Repository assumption:** empty Git repository  
**Primary invariant:** the observatory **observes, reconstructs, measures, and explains**; it does **not authorize, approve, block, rewrite, or execute Codex work**.

> This document converts the supplied “Codex Local Telemetry Observatory — Full Implementation Blueprint v1.0” into an execution-ready build plan. Where the source used words such as “recommended,” “optional,” “where possible,” or left integration mechanics implicit, this document resolves them into concrete v1 decisions unless current documentation does not safely permit that.

---

# 0. Agent Operating Contract

An implementation agent MUST follow these rules.

1. Work phases in dependency order. Do not skip a phase gate.
2. Treat this document as normative for v1 unless a current primary source directly contradicts it.
3. When a Codex/OpenAI contract is version-sensitive, capture the installed version and bind evidence to that version.
4. Prefer documented machine interfaces over parsing local implementation files.
5. Never turn an observational hook into an authorization hook.
6. Never claim a causal relationship when only temporal correlation exists.
7. Never store prompts, raw tool arguments, or raw tool output by default.
8. Never expose the local server beyond loopback in v1.
9. Preserve independent evidence from different sources even when events represent the same conceptual operation.
10. A task is complete only when its specified verification commands/tests pass and its artifacts exist.
11. If a live Codex contract differs from the fixture/schema for the detected Codex version, fail the compatibility gate visibly; do not silently reinterpret it.
12. If telemetry is unavailable, Codex itself must continue functioning.

**Source-of-truth order for implementation questions:**

```text
Current official Codex/OpenAI documentation
> generated App Server schema for the installed Codex version
> OpenTelemetry OTLP specification
> official SQLite/DuckDB/Python/FastAPI/React/Vite documentation
> reproducible local contract test
> this roadmap
> assumption
```

---

# 1. Open Questions Scan — MUST OCCUR BEFORE IMPLEMENTATION

The original blueprint is strong architecturally, but an agent starting from an empty repository would encounter the following unresolved or underspecified questions.

| ID | Open question | Why it blocks implementation | Resolution status |
|---|---|---|---|
| OQ-001 | Can the observatory passively attach to an arbitrary already-running Codex CLI App Server stream? | Determines whether App Server can be a universal live source. | **Resolved: no universal passive attach contract exists.** |
| OQ-002 | What is App Server’s safe v1 role? | Prevents accidental control-plane coupling or thread mutation. | **Resolved: reconciliation + versioned enrichment; live only in explicit integration tests.** |
| OQ-003 | Where must Codex OTel configuration live? | Project-local setup cannot be assumed to control OTel. | **Resolved: user/system layer, not project-local.** |
| OQ-004 | What happens when the user already has an OTel exporter? | Codex exposes one configured exporter per signal; blindly overwriting it is destructive. | **Resolved: refuse destructive overwrite by default.** |
| OQ-005 | Should v1 implement an OTLP receiver or require OpenTelemetry Collector? | Changes deployment, protocol ownership, Windows installation, and failure behavior. | **Resolved: embedded OTLP/HTTP receiver; Collector used as interoperability oracle, not runtime dependency.** |
| OQ-006 | Which OTLP encodings are accepted? | Codex supports binary and JSON; receiver behavior must be exact. | **Resolved: protobuf + OTLP JSON, optional gzip.** |
| OQ-007 | What must an OTLP success/error response look like? | A generic `200 {}` handler is not a correct protocol implementation for all encodings/errors. | **Resolved: signal-specific Export*ServiceResponse / OTLP Status semantics.** |
| OQ-008 | Which Python and Node baselines should an empty repo use? | Reproducibility and Windows support require a fixed baseline. | **Resolved: Python 3.14.x; Node 24 LTS.** |
| OQ-009 | Which dependency managers should be authoritative? | Source listed uv and pnpm without defining bootstrap policy. | **Resolved: uv + npm.** |
| OQ-010 | Where should runtime state live? | Committing `data/observatory.db` or placing durable state inside the repo is unsafe/noisy. | **Resolved: OS user-data directories; repo contains no runtime DB.** |
| OQ-011 | How is one-writer SQLite discipline enforced? | FastAPI, collectors, retention, and WebSocket replay can otherwise create lock contention. | **Resolved: dedicated writer task/connection; request handlers are read-only.** |
| OQ-012 | How can the WebSocket `sequence` be resumed after restart? | The source API requires resumability but the original schema lacks a durable sequence column. | **Resolved: monotonic SQLite `event_seq`.** |
| OQ-013 | How is “same operation, independent evidence” represented? | The source describes `operation_id` conceptually but has no durable tables. | **Resolved: `operations` + `operation_evidence`.** |
| OQ-014 | Does the schema preserve real OTLP traces? | Generic event rows do not preserve span parentage, links, events, or nanosecond timing. | **Resolved: dedicated `spans` table plus canonical events.** |
| OQ-015 | How are raw protobuf payloads stored under minimal privacy? | Original raw table assumes JSON while OTLP may be binary; privacy says raw content should be minimized. | **Resolved: transient spool + digest metadata in minimal mode; durable wire body only with explicit forensic opt-in.** |
| OQ-016 | How do hooks remain neutral and reliable if the API server is down? | Synchronous HTTP hooks could slow or break operator workflows. | **Resolved: atomic file-spool hook writer, exit 0, no decision output.** |
| OQ-017 | Which hook configuration representation is used? | Codex can load `hooks.json` and inline TOML simultaneously and warn/merge them. | **Resolved: user-level `hooks.json`; refuse auto-install if same layer already uses inline hooks unless explicitly reconciled.** |
| OQ-018 | How are Codex schema upgrades detected? | App Server and telemetry evolve rapidly. | **Resolved: version capture + generated App Server JSON/TS schema + compatibility fixtures.** |
| OQ-019 | How are OTel IDs joined to hook/App Server IDs? | Similar naming does not prove shared identity. | **Resolved: exact join only after the field is documented or verified for the tested Codex version; otherwise downgrade confidence.** |
| OQ-020 | How are OpenAI organization costs attributed to Codex sessions? | Aggregate org billing is not a session ledger. | **Resolved: no session-dollar attribution without an exact supported identifier.** |
| OQ-021 | How should FastAPI initialize background resources? | Deprecated startup/shutdown patterns would create immediate technical debt. | **Resolved: FastAPI lifespan context.** |
| OQ-022 | How is the React frontend served in production? | Dev proxy and production hosting otherwise diverge. | **Resolved: one origin; built static frontend served by FastAPI.** |
| OQ-023 | What is the migration mechanism? | Empty-repo agents need deterministic fresh DB and upgrade paths. | **Resolved: numbered immutable SQL migrations + SHA-256 checksums.** |
| OQ-024 | How are archives proven before hot-data deletion? | Original retention algorithm requires proof but no archive registry exists. | **Resolved: `archive_batches` manifest registry.** |
| OQ-025 | What is “normal load” for performance acceptance? | “Zero drops under normal load” is otherwise untestable. | **Resolved: defined synthetic workload in the performance gate.** |
| OQ-026 | How is the application packaged for Windows? | A Python service plus Vite app otherwise needs two manual runtimes after install. | **Resolved: build frontend into Python package static assets; single `codex-observatory serve` runtime.** |
| OQ-027 | How are credentials persisted? | “OS secret storage or env” leaves implementation ambiguous. | **Resolved: environment-only in v1; OS keyring deferred.** |
| OQ-028 | Is remote access part of v1? | TLS/auth/CSRF/RBAC requirements explode scope. | **Resolved: no; hard loopback-only v1.** |

**Implementation gate OQ-GATE:** do not create product code until the research record in §2 and decisions in §3 are accepted as the v1 contract.

---

# 2. Research Record — Answers to the Open Questions

This section records the primary-source research that converts the preceding questions into implementation constraints.

## R-001 — Codex OTel is explicit, asynchronous, and configured outside project-local control

Current Codex documentation says OTel export is disabled by default, supports OTLP HTTP/gRPC exporters, supports `binary` or `json` for OTLP/HTTP, and keeps raw user prompts disabled unless `otel.log_user_prompt = true`. OTel event metadata includes a conversation identifier, model, CLI version, environment, sandbox/approval configuration, and event-specific fields. The current configuration reference separately exposes log, metric, and trace exporters.

**Consequence:** the observatory must edit or instruct changes at the user/system Codex configuration layer, must keep `log_user_prompt=false`, and must not assume repository `.codex/config.toml` can redirect OTel.

Primary sources:
- https://developers.openai.com/codex/config-advanced
- https://developers.openai.com/codex/config-reference
- https://developers.openai.com/codex/config-sample

## R-002 — App Server is a client/server protocol, not a universal passive tap

Current App Server documentation says clients initialize a connection, then start/resume/fork threads and consume notifications. `thread/start` automatically subscribes that connection to turn/item events. `thread/read` explicitly reads a persisted thread **without subscribing**. `thread/loaded/list` lists threads currently in memory, and `thread/unsubscribe` only removes an existing connection subscription; no general “subscribe to arbitrary thread without resuming/controlling it” operation is documented.

The default App Server transport is stdio JSONL. Current documentation treats the App Server command and its WebSocket transport as experimental for production use; WebSocket is explicitly unsupported for production workloads. The CLI can generate version-specific TypeScript and JSON Schema bundles.

**Consequence:** v1 must not advertise “attach to any live Codex CLI” via App Server. App Server is used for documented persisted-thread reconciliation and version-aware enrichment. Live App Server stream handling is implemented and tested against controlled App Server sessions, but universal live coverage comes from OTel + hooks.

Primary source:
- https://developers.openai.com/codex/app-server

## R-003 — Hooks are rich but can affect execution if misused

Current Codex hook events include `SessionStart`, `PreToolUse`, `PermissionRequest`, `PostToolUse`, `PreCompact`, `PostCompact`, `UserPromptSubmit`, `SubagentStart`, `SubagentStop`, `Stop`, `Interrupt`, and `SessionEnd`. Command hooks receive JSON on stdin. `transcript_path` is explicitly not a stable exchange interface. A command hook exiting `0` without output is success/continue. Some hooks are capable of blocking/rewriting or injecting context.

Codex loads hooks from `hooks.json` or inline `[hooks]` configuration. Using both forms in the same configuration layer causes both to load and produces a warning. User-level hooks are independent of project trust; project-local hooks require trust.

**Consequence:** observatory hooks write telemetry only, emit no stdout decision body, exit `0`, and never parse transcripts as a contract. Installation uses one user-level hook representation.

Primary source:
- https://developers.openai.com/codex/hooks

## R-004 — OTLP has protocol-level obligations

OTLP/HTTP defines `/v1/logs`, `/v1/metrics`, and `/v1/traces`, protobuf (`application/x-protobuf`) and JSON (`application/json`) encodings, optional compression such as gzip, signal-specific `Export*ServiceResponse` bodies, partial-success fields, and structured error/retry behavior. Traces, metrics, and logs are stable OTLP signals.

**Consequence:** the embedded receiver is not “three generic POST endpoints.” It has signal-specific decoders, response serializers, media-type validation, decompression bounds, and protocol tests against an OpenTelemetry Collector/exporter fixture.

Primary source:
- https://opentelemetry.io/docs/specs/otlp/

## R-005 — SQLite WAL fits the local live-store role, but only one writer should be owned

SQLite WAL permits concurrent readers with a writer but still only one writer at a time. WAL is same-host oriented and can require checkpoint management. Busy conditions remain possible.

**Consequence:** one application-owned write connection processes batched write commands. API request handlers and DuckDB readers never write directly. WAL checkpoint and size are health metrics.

Primary source:
- https://www.sqlite.org/wal.html

## R-006 — DuckDB is appropriate for immutable Parquet analytics, not the primary multi-process write store

DuckDB can query Parquet directly, including file collections and Hive-style partitions, and its Python relation API can write Parquet. This matches an append-only historical analytics role.

**Consequence:** SQLite is live truth; Parquet is immutable archive; DuckDB is an in-process analytical query engine opened by the backend, not a second transactional source of truth.

Primary source:
- https://duckdb.org/docs/current/data/parquet/overview
- https://duckdb.org/docs/current/data/partitioning/hive_partitioning
- https://duckdb.org/docs/current/connect/concurrency

## R-007 — Current runtime baselines

As of the research date, Python 3.14 is the current stable feature series and 3.14.7 is the current maintenance release; Python 3.15 remains pre-release. Node 24 is LTS while Node 26 is Current. Vite requires Node 20.19+ or 22.12+ at minimum. uv provides exact lockfile syncing and recommends committing `uv.lock`.

**Consequence:** v1 targets Python `>=3.14,<3.15`, records `.python-version` as `3.14`, and targets Node `24.x`. `uv.lock` and `package-lock.json` are committed and CI uses locked installs.

Primary sources:
- https://www.python.org/downloads/release/python-3147/
- https://www.python.org/getit/
- https://nodejs.org/en/about/previous-releases
- https://vite.dev/guide/
- https://docs.astral.sh/uv/concepts/projects/sync/

## R-008 — FastAPI/React lifecycle choices

Current FastAPI guidance uses the application lifespan context manager for startup/shutdown resource management; older event handlers are deprecated. FastAPI supports WebSocket tests with `TestClient`. Current FastAPI also documents serving a built frontend from a directory. React’s browser entrypoint uses `createRoot`, and a Vite client-only SPA is a supported starting pattern.

**Consequence:** backend collector startup/shutdown belongs in `lifespan`. Production serves one built React root from FastAPI. Development uses Vite with an API/WS proxy.

Primary sources:
- https://fastapi.tiangolo.com/advanced/events/
- https://fastapi.tiangolo.com/advanced/testing-websockets/
- https://fastapi.tiangolo.com/tutorial/frontend/
- https://react.dev/reference/react-dom/client/createRoot
- https://vite.dev/guide/

## R-009 — Organization Usage/Costs are Admin API data, not per-Codex-session billing

The current OpenAI Admin API exposes organization usage endpoints and `GET /organization/costs`. Usage responses are paginated with `has_more` / `next_page`, and usage endpoints support time buckets and grouping fields such as project/model/API key where applicable. Current SDK examples use `OPENAI_ADMIN_KEY` / `admin_api_key`.

**Consequence:** Admin sync is optional, credentials stay outside durable observatory state, pagination cursors are persisted, and all cost/session relationships are labeled `aggregate` or `unknown` unless an exact documented shared identifier exists.

Primary sources:
- https://developers.openai.com/api/reference/python/resources/admin/subresources/organization
- https://developers.openai.com/api/reference/python/resources/admin/subresources/organization/subresources/usage

---

# 3. Concrete v1 Decision Register

These decisions supersede ambiguity in the original reference blueprint for v1.

| Decision | Normative choice | Rationale / consequence |
|---|---|---|
| D-001 | Product/package name: `codex-observatory` / `codex_observatory` | Stable naming from repository through CLI/imports. |
| D-002 | Python `>=3.14,<3.15`; `.python-version=3.14` | Stable current Python line; avoid 3.15 pre-release. |
| D-003 | Node 24 LTS; npm; commit `package-lock.json` | Lowest bootstrap dependency on Windows; Vite supported. |
| D-004 | Python manager: uv; commit `uv.lock`; CI uses `uv sync --locked` | Reproducible installs. |
| D-005 | Backend: FastAPI + Pydantic + stdlib `sqlite3`; no ORM | Schema is explicit, append-heavy, and migration-controlled. |
| D-006 | Backend lifecycle uses FastAPI `lifespan` | Current supported lifecycle pattern. |
| D-007 | One dedicated SQLite writer task owns the only normal write connection | Enforces WAL concurrency model and batching. |
| D-008 | SQLite WAL + `foreign_keys=ON` + `busy_timeout=5000` + `synchronous=NORMAL` | Local throughput with bounded durability; durable spool covers transient ingest. |
| D-009 | Durable WebSocket/event ordering uses `event_seq INTEGER PRIMARY KEY AUTOINCREMENT` | Restart-safe replay and deterministic tie-breaking. |
| D-010 | Event IDs use UUIDv7 where available (Python 3.14 stdlib) | Time-sortable unique IDs without external package. |
| D-011 | Embedded OTLP/HTTP receiver is a first-class adapter | No external collector required for normal installation. |
| D-012 | OTLP receiver supports protobuf + JSON + gzip; max decompressed request 64 MiB | Matches standard protocol surface; protects local process. |
| D-013 | OTel log, metric, and trace paths are separate protocol handlers | Correct response messages and validation per signal. |
| D-014 | OTel configuration uses `protocol="binary"` in generated Codex setup | Binary is simpler/faster; JSON remains supported/interoperability-tested. |
| D-015 | Existing non-Observatory Codex OTel exporters are never overwritten without `--force` | Avoid hijacking the user’s observability pipeline. |
| D-016 | Hooks install to user-level `~/.codex/hooks.json` only | Works across repos and avoids trust dependence. |
| D-017 | If the user-level Codex config already contains inline hooks, `configure-codex --apply` fails safely and prints reconciliation instructions | Avoid duplicate/merged hook surprises. |
| D-018 | Hook transport is atomic file-per-event spool, not synchronous HTTP | Codex execution remains independent of observatory availability. |
| D-019 | Hook handlers always best-effort, no decision output, exit 0 | Telemetry must not become policy. |
| D-020 | `transcript_path` is metadata only; transcript files are never parsed in v1 | Documented as unstable. |
| D-021 | App Server v1 default mode is `reconcile`, and its evidence stability is labeled `experimental` for the current documented App Server surface | Uses documented `thread/list`/`thread/read` without pretending to passively subscribe or overstate provider stability. |
| D-022 | App Server live-stream normalization is contract-tested but is not required for arbitrary external CLI sessions | Correctly scopes capability. |
| D-023 | App Server WebSocket transport is not a production dependency | Documented experimental/unsupported transport. |
| D-024 | Capture generated App Server JSON/TS schemas for each tested Codex version | Version-locked compatibility evidence. |
| D-025 | No undocumented Codex SQLite/table parsing | Local internals remain diagnostic only. |
| D-026 | Git evidence comes from Git commands, not filesystem heuristics | Avoid stale/incorrect repository claims. |
| D-027 | Runtime data lives in platform-specific user data directories via `platformdirs` | Repo remains clean; Windows-friendly. |
| D-028 | Default privacy mode `minimal`; prompts off; args/output digest-only | Useful telemetry without content capture. |
| D-029 | In minimal mode, wire payloads may exist only in a short-lived processing spool and are deleted after successful normalization; durable raw body storage is off | Reconciles binary protocol recovery with privacy. |
| D-030 | `forensic` is explicit opt-in and may persist raw wire bodies for the configured retention period | High-risk data never appears by default. |
| D-031 | Operations are separate from evidence events | Preserves independent source evidence and avoids destructive dedupe. |
| D-032 | Correlation has `method`, numeric `confidence`, and `rule_version`; undocumented ID equivalence is never assumed | Makes weak joins visible and upgrade-safe. |
| D-033 | Historical exports are immutable Parquet batches with registry + manifest + SHA-256 | Allows proven retention before deletion. |
| D-034 | DuckDB query engine is read-only with respect to canonical live state | Avoids dual-writer architecture. |
| D-035 | Frontend is a single React/Vite SPA; production assets served by FastAPI on same origin | Simplifies CORS/security/install. |
| D-036 | Frontend state uses backend APIs + WebSocket; no Redux/global state framework in v1 | Read-heavy UI does not justify extra state machinery. |
| D-037 | Use ECharts for charts and React Router for navigation; exact versions are lockfile-controlled | Avoid bespoke charting while keeping version drift reproducible. |
| D-038 | OpenAPI schema generates frontend TypeScript API types during build/CI | Prevents API/frontend contract drift. |
| D-039 | OpenAI Admin API sync uses official Python SDK and `OPENAI_ADMIN_KEY` env only | No credential persistence. |
| D-040 | No per-session dollar value from aggregate billing | Prevents false precision. |
| D-041 | Remote serving is out of scope; startup rejects non-loopback host in v1 | Avoids incomplete auth/TLS surface. |
| D-042 | Hand-written immutable SQL migrations + checksum table | Simple, inspectable schema evolution. |
| D-043 | CI runs deterministic contract fixtures on Windows + Ubuntu; live Codex tests are explicit/manual | Auth/runtime-sensitive tests do not make CI flaky. |
| D-044 | Production package includes prebuilt frontend under Python package data | One command starts the installed app. |
| D-045 | “Normal load” = 100 events/s sustained 10 min + 1,000-event burst; zero accepted-event loss | Makes performance gate measurable. |

---

# 4. Fully Implementable Architecture

## 4.1 System boundary

```text
Codex CLI
  ├─ OTel logs/metrics/traces ─────────────┐
  ├─ lifecycle hooks → atomic spool ───────┤
  ├─ persisted threads ← App Server read ──┤
  └─ repository cwd → Git commands ────────┤
                                            ▼
                                   Collector Supervisor
                                            │
                                  RawEnvelope boundary
                                            │
                         ┌──────────────────┴──────────────────┐
                         ▼                                     ▼
                  durable ingest spool                  protocol diagnostics
                         │
                         ▼
                  normalization workers
                         │
                         ▼
             correlation / operation assembly
                         │
                         ▼
                 single SQLite writer
                         │
              ┌──────────┼─────────────┐
              ▼          ▼             ▼
          REST views   WS replay   archive selector
                                      │
                                      ▼
                               immutable Parquet
                                      │
                                      ▼
                                   DuckDB
                                      │
                                      ▼
                                  analytics

Optional OpenAI Admin Usage/Costs ──► normalized aggregate billing tables
```

**Authority boundary:** no collector or dashboard endpoint can grant an approval, modify a Codex tool invocation, change sandbox policy, or start a Codex turn.

## 4.2 Repository layout

```text
codex-observatory/
├─ .editorconfig
├─ .gitignore
├─ .python-version
├─ LICENSE
├─ README.md
├─ AGENTS.md
├─ pyproject.toml
├─ uv.lock
├─ package.json                    # root convenience scripts only
├─ docs/
│  ├─ architecture.md
│  ├─ event-schema.md
│  ├─ source-contracts.md
│  ├─ api-contract.md
│  ├─ privacy.md
│  ├─ retention.md
│  ├─ compatibility.md
│  ├─ operations.md
│  └─ research/
│     └─ 2026-09-20-source-validation.md
├─ config/
│  ├─ observatory.example.toml
│  └─ retention.example.toml
├─ compatibility/
│  └─ codex/
│     ├─ registry.json
│     ├─ schemas/
│     │  └─ <codex-version>/
│     │     ├─ json/
│     │     └─ ts/
│     └─ fixtures/
│        └─ <codex-version>/
├─ src/
│  └─ codex_observatory/
│     ├─ __init__.py
│     ├─ __main__.py
│     ├─ cli.py
│     ├─ main.py
│     ├─ config.py
│     ├─ paths.py
│     ├─ version.py
│     ├─ static/                   # generated production frontend
│     ├─ domain/
│     │  ├─ envelope.py
│     │  ├─ event.py
│     │  ├─ operation.py
│     │  ├─ provenance.py
│     │  ├─ correlation.py
│     │  └─ enums.py
│     ├─ adapters/
│     │  ├─ base.py
│     │  ├─ otlp/
│     │  │  ├─ receiver.py
│     │  │  ├─ codec.py
│     │  │  ├─ logs.py
│     │  │  ├─ metrics.py
│     │  │  └─ traces.py
│     │  ├─ hooks/
│     │  │  ├─ spool_writer.py
│     │  │  ├─ spool_reader.py
│     │  │  └─ mapper.py
│     │  ├─ app_server/
│     │  │  ├─ process.py
│     │  │  ├─ rpc.py
│     │  │  ├─ reconcile.py
│     │  │  ├─ stream_mapper.py
│     │  │  └─ compatibility.py
│     │  ├─ git/
│     │  │  ├─ commands.py
│     │  │  └─ snapshots.py
│     │  ├─ codex_local/
│     │  │  └─ discovery.py
│     │  └─ openai_admin/
│     │     ├─ client.py
│     │     ├─ usage.py
│     │     └─ costs.py
│     ├─ ingest/
│     │  ├─ supervisor.py
│     │  ├─ queue.py
│     │  ├─ normalize.py
│     │  ├─ correlate.py
│     │  ├─ operations.py
│     │  └─ recovery.py
│     ├─ storage/
│     │  ├─ sqlite.py
│     │  ├─ writer.py
│     │  ├─ queries.py
│     │  ├─ migrations.py
│     │  ├─ migrations/
│     │  │  ├─ 0001_core.sql
│     │  │  ├─ 0002_operations.sql
│     │  │  ├─ 0003_otel.sql
│     │  │  ├─ 0004_archives.sql
│     │  │  └─ 0005_admin_api.sql
│     │  ├─ parquet.py
│     │  └─ duckdb.py
│     ├─ analytics/
│     │  ├─ overview.py
│     │  ├─ percentiles.py
│     │  ├─ agents.py
│     │  ├─ tools.py
│     │  └─ governance.py
│     ├─ api/
│     │  ├─ app.py
│     │  ├─ errors.py
│     │  ├─ schemas.py
│     │  ├─ websocket.py
│     │  └─ routes/
│     │     ├─ overview.py
│     │     ├─ sessions.py
│     │     ├─ events.py
│     │     ├─ agents.py
│     │     ├─ tools.py
│     │     ├─ approvals.py
│     │     ├─ skills.py
│     │     ├─ repos.py
│     │     ├─ costs.py
│     │     └─ health.py
│     ├─ retention/
│     │  ├─ scheduler.py
│     │  ├─ archive.py
│     │  └─ prune.py
│     └─ health/
│        ├─ model.py
│        └─ checks.py
├─ hooks/
│  ├─ codex_observatory_hook.py
│  └─ hooks.template.json
├─ frontend/
│  ├─ package.json
│  ├─ package-lock.json
│  ├─ tsconfig.json
│  ├─ vite.config.ts
│  ├─ index.html
│  └─ src/
│     ├─ main.tsx
│     ├─ App.tsx
│     ├─ api/
│     ├─ components/
│     ├─ charts/
│     ├─ hooks/
│     ├─ types/
│     └─ views/
│        ├─ Overview.tsx
│        ├─ Sessions.tsx
│        ├─ SessionDetail.tsx
│        ├─ Agents.tsx
│        ├─ Turns.tsx
│        ├─ Tools.tsx
│        ├─ Approvals.tsx
│        ├─ Skills.tsx
│        ├─ Git.tsx
│        ├─ Costs.tsx
│        ├─ Explorer.tsx
│        └─ Health.tsx
├─ tests/
│  ├─ unit/
│  ├─ contracts/
│  ├─ adapters/
│  ├─ integration/
│  ├─ live/
│  ├─ e2e/
│  └─ fixtures/
├─ scripts/
│  ├─ build_frontend.py
│  ├─ capture_codex_schema.py
│  ├─ generate_openapi_types.py
│  └─ verify_release.py
└─ .github/
   └─ workflows/
      ├─ ci.yml
      └─ live-contract.yml
```

**Do not commit runtime `data/` directories.**

## 4.3 Runtime paths

Resolve with `platformdirs`.

```text
config_dir   = user_config_dir("codex-observatory")
data_dir     = user_data_dir("codex-observatory")
cache_dir    = user_cache_dir("codex-observatory")
log_dir      = user_log_dir("codex-observatory")
```

Under `data_dir`:

```text
observatory.db
spool/
  incoming/
  processing/
  failed/
parquet/
manifests/
```

Never require administrator privileges. `doctor` MUST print resolved paths.

## 4.4 Python dependencies

Runtime dependencies:

```text
fastapi
uvicorn[standard]
pydantic
platformdirs
tomlkit
httpx
duckdb
opentelemetry-proto
protobuf
openai
```

Development/test dependencies:

```text
pytest
pytest-asyncio
pytest-cov
ruff
mypy
httpx
```

Use stdlib `sqlite3`, `hashlib`, `uuid`, `tomllib`, `asyncio`, `subprocess`, `pathlib`, and `gzip` where possible.

## 4.5 Frontend dependencies

Runtime:

```text
react
react-dom
react-router-dom
echarts
```

Development:

```text
typescript
vite
@vitejs/plugin-react
vitest
@testing-library/react
@testing-library/jest-dom
@playwright/test
openapi-typescript
eslint
```

Exact versions are determined once during bootstrap and pinned by `package-lock.json`.

---

# 5. Bootstrap From an Empty Repository

Run from the empty repository root.

```bash
git init
printf "3.14\n" > .python-version
uv init --package --python 3.14
uv add fastapi 'uvicorn[standard]' pydantic platformdirs tomlkit httpx duckdb opentelemetry-proto protobuf openai
uv add --dev pytest pytest-asyncio pytest-cov ruff mypy
npm create vite@latest frontend -- --template react-ts
cd frontend
npm install
npm install react-router-dom echarts
npm install -D vitest @testing-library/react @testing-library/jest-dom @playwright/test openapi-typescript eslint
cd ..
uv lock
```

Then create the repository tree from §4.2 before implementing behavior.

Root `pyproject.toml` MUST expose:

```toml
[project.scripts]
codex-observatory = "codex_observatory.cli:main"
```

Set:

```toml
requires-python = ">=3.14,<3.15"
```

Root `.gitignore` MUST exclude:

```text
.venv/
__pycache__/
.pytest_cache/
.mypy_cache/
.ruff_cache/
coverage.xml
htmlcov/
frontend/node_modules/
frontend/dist/
src/codex_observatory/static/*
*.db
*.db-wal
*.db-shm
*.parquet
.env
```

The production static directory may be generated during build; retain a `.gitkeep` if needed.

---

# 6. Configuration Contract v1

File: `config/observatory.example.toml`

```toml
schema_version = 1

[server]
host = "127.0.0.1"
port = 8765

[storage]
# null means resolve with platformdirs
sqlite_path = null
parquet_root = null
spool_root = null
sqlite_synchronous = "NORMAL"
write_batch_size = 100
write_batch_max_wait_ms = 50

[collectors.otlp]
enabled = true
max_decompressed_bytes = 67108864
accept_protobuf = true
accept_json = true
accept_gzip = true

[collectors.hooks]
enabled = true
poll_interval_ms = 250

[collectors.git]
enabled = true
command_timeout_seconds = 5

[collectors.app_server]
enabled = true
mode = "reconcile"
reconcile_interval_seconds = 30
include_turns = true

[collectors.openai_admin]
enabled = false
sync_interval_minutes = 60
bucket_width = "1h"

[privacy]
mode = "minimal"
store_prompts = false
store_tool_arguments = "digest"
store_tool_output = "digest"
persist_raw_wire_payloads = false
processing_spool_max_hours = 1

[retention]
hot_days = 30
raw_metadata_days = 7
forensic_raw_days = 7
historical_days = 365
aggregate_days = 0 # 0 = indefinite
run_interval_hours = 24

[compatibility]
unknown_codex_version = "warn"
require_generated_app_server_schema = true
```

Validation rules:

```text
host MUST be loopback in v1.
forensic raw payload persistence requires privacy.mode="forensic".
store_prompts=true requires privacy.mode="forensic".
No API/admin key field exists in the schema.
All paths resolve to absolute paths before runtime use.
```

---

# 7. Canonical Data Contracts

## 7.1 RawEnvelope v1

```json
{
  "schema": "codex.observatory.raw.v1",
  "ingest_id": "uuid7",
  "received_at": "RFC3339 UTC",
  "source": "native_otel",
  "source_instance": "local-default",
  "source_event_type": "logs",
  "source_version": "codex-x.y.z",
  "content_type": "application/x-protobuf",
  "content_encoding": null,
  "payload_encoding": "protobuf",
  "payload_sha256": "sha256:...",
  "payload_size": 1234,
  "payload_ref": "spool://... or null",
  "cursor": null,
  "metadata": {}
}
```

`payload_ref` may be null after successful normalization in minimal mode.

## 7.2 Canonical Event v1

Keep the supplied conceptual schema, adding durable ordering and correlation rule identity:

```json
{
  "schema": "codex.observatory.event.v1",
  "event_id": "uuid7",
  "event_seq": 12345,
  "event_time": "RFC3339 UTC",
  "event_time_unix_nano": 0,
  "observed_at": "RFC3339 UTC",
  "classification": {
    "source_class": "native_otel",
    "fact_type": "native",
    "stability": "documented"
  },
  "provenance": {
    "source_event": "codex.tool_result",
    "source_instance": "local-default",
    "source_version": "0.x",
    "raw_event_sha256": "sha256:...",
    "adapter_version": "otel-adapter/1"
  },
  "correlation": {
    "session_id": null,
    "thread_id": null,
    "turn_id": null,
    "item_id": null,
    "call_id": null,
    "agent_id": null,
    "parent_agent_id": null,
    "trace_id": null,
    "span_id": null,
    "operation_id": null
  },
  "project": {
    "repo_id": null,
    "repo_root": null,
    "worktree": null,
    "git_snapshot_id": null
  },
  "runtime": {
    "codex_version": null,
    "model": null,
    "auth_mode": null,
    "terminal_type": null
  },
  "event": {
    "category": "tool",
    "name": "shell",
    "phase": "completed",
    "status": "success"
  },
  "attributes": {}
}
```

Canonical categories remain:

```text
session thread turn agent model api tool approval file_change git
skill hook context network mcp usage cost collector diagnostic
```

## 7.3 CorrelationResult v1

```json
{
  "operation_id": "uuid7",
  "method": "native_id",
  "confidence": 1.0,
  "rule_version": "correlator/1",
  "evidence_event_ids": ["..."],
  "reason": "same documented call_id"
}
```

Allowed initial methods:

```text
native_id           1.00
explicit_parent     1.00
verified_alias      0.99
repo_time_window    0.70
time_only           0.30
unresolved          0.00
```

`verified_alias` requires a compatibility rule scoped to a tested Codex version.

---

# 8. SQLite Schema v1

Implement as immutable numbered SQL migrations. At minimum create the following tables.

## 8.1 `schema_migrations`

```sql
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    checksum TEXT NOT NULL UNIQUE
);
```

## 8.2 `raw_events`

```sql
CREATE TABLE raw_events (
    ingest_id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL,
    source TEXT NOT NULL,
    source_instance TEXT NOT NULL,
    source_event_type TEXT NOT NULL,
    source_version TEXT,
    content_type TEXT,
    content_encoding TEXT,
    payload_encoding TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    payload_size INTEGER NOT NULL,
    payload_ref TEXT,
    parse_status TEXT NOT NULL,
    error_code TEXT,
    error_message TEXT,
    retention_class TEXT NOT NULL
);
```

## 8.3 `events`

```sql
CREATE TABLE events (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_time TEXT NOT NULL,
    event_time_unix_nano INTEGER,
    observed_at TEXT NOT NULL,

    source_class TEXT NOT NULL,
    fact_type TEXT NOT NULL,
    stability TEXT NOT NULL,

    source_event TEXT NOT NULL,
    source_instance TEXT NOT NULL,
    source_version TEXT,
    raw_event_sha256 TEXT NOT NULL,
    adapter_version TEXT NOT NULL,

    session_id TEXT,
    thread_id TEXT,
    turn_id TEXT,
    item_id TEXT,
    call_id TEXT,
    agent_id TEXT,
    parent_agent_id TEXT,
    trace_id TEXT,
    span_id TEXT,
    operation_id TEXT,

    repo_id TEXT,
    git_snapshot_id TEXT,

    category TEXT NOT NULL,
    name TEXT NOT NULL,
    phase TEXT,
    status TEXT,
    attributes_json TEXT NOT NULL
);
```

Indexes:

```sql
CREATE INDEX idx_events_time ON events(event_time, event_seq);
CREATE INDEX idx_events_thread ON events(thread_id, event_time, event_seq);
CREATE INDEX idx_events_turn ON events(turn_id, event_time, event_seq);
CREATE INDEX idx_events_category ON events(category, name, event_time);
CREATE INDEX idx_events_call ON events(call_id);
CREATE INDEX idx_events_agent ON events(agent_id, event_time);
CREATE INDEX idx_events_operation ON events(operation_id, event_seq);
```

## 8.4 Operation tables

```sql
CREATE TABLE operations (
    operation_id TEXT PRIMARY KEY,
    operation_type TEXT NOT NULL,
    first_event_seq INTEGER NOT NULL,
    last_event_seq INTEGER NOT NULL,
    status TEXT,
    correlation_method TEXT NOT NULL,
    correlation_confidence REAL NOT NULL CHECK (correlation_confidence BETWEEN 0 AND 1),
    correlation_rule_version TEXT NOT NULL,
    FOREIGN KEY(first_event_seq) REFERENCES events(event_seq),
    FOREIGN KEY(last_event_seq) REFERENCES events(event_seq)
);

CREATE TABLE operation_evidence (
    operation_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL,
    evidence_role TEXT NOT NULL,
    PRIMARY KEY(operation_id, event_seq),
    FOREIGN KEY(operation_id) REFERENCES operations(operation_id),
    FOREIGN KEY(event_seq) REFERENCES events(event_seq)
);
```

## 8.5 Lifecycle/domain tables

Implement source blueprint tables with foreign keys and indexes:

```text
sessions
threads
turns
agents
tool_calls
approvals
token_usage
skill_events
git_snapshots
repositories
metric_samples
source_cursors
collector_health
```

Add `event_seq` / `source_event_id` references where a row is derived from canonical evidence.

## 8.6 OTLP spans

```sql
CREATE TABLE spans (
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    parent_span_id TEXT,
    name TEXT NOT NULL,
    kind INTEGER,
    start_time_unix_nano INTEGER NOT NULL,
    end_time_unix_nano INTEGER NOT NULL,
    status_code INTEGER,
    status_message TEXT,
    resource_attributes_json TEXT NOT NULL,
    scope_name TEXT,
    scope_version TEXT,
    attributes_json TEXT NOT NULL,
    events_json TEXT NOT NULL,
    links_json TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    PRIMARY KEY(trace_id, span_id)
);
```

## 8.7 Rich metric samples

Do not discard native metric semantics:

```sql
CREATE TABLE metric_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_name TEXT NOT NULL,
    description TEXT,
    unit TEXT,
    metric_type TEXT NOT NULL,
    start_time_unix_nano INTEGER,
    time_unix_nano INTEGER NOT NULL,
    value REAL,
    count INTEGER,
    sum REAL,
    aggregation_temporality INTEGER,
    is_monotonic INTEGER,
    bucket_json TEXT,
    exemplar_json TEXT,
    labels_json TEXT NOT NULL,
    resource_attributes_json TEXT NOT NULL,
    scope_name TEXT,
    source_event_id TEXT NOT NULL
);
```

## 8.8 Archive registry

```sql
CREATE TABLE archive_batches (
    archive_id TEXT PRIMARY KEY,
    dataset TEXT NOT NULL,
    created_at TEXT NOT NULL,
    file_path TEXT NOT NULL UNIQUE,
    manifest_path TEXT NOT NULL UNIQUE,
    row_count INTEGER NOT NULL,
    min_event_time TEXT,
    max_event_time TEXT,
    schema_version TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    verified_at TEXT,
    source_min_event_seq INTEGER,
    source_max_event_seq INTEGER,
    prune_completed_at TEXT
);
```

## 8.9 Admin API tables

Keep usage and costs separate exactly as the source requires. Persist the server cursor in `source_cursors`, not as a billing identity.

---

# 9. Ingestion and Durability Design

## 9.1 Spool format

Use atomic one-file-per-envelope JSON for portability and crash recovery.

Filename:

```text
<received_unix_ns>_<ingest_uuid7>.json
```

Write algorithm:

```text
serialize envelope metadata + temporary payload representation
write to <name>.tmp
flush
fsync file
os.replace(tmp, incoming/name.json)
```

When minimal privacy is active, the wire payload is permitted only in the processing spool, never in durable DB/Parquet. After successful normalization and DB commit, delete the spool file. A recovery job deletes stale successfully-committed processing envelopes and quarantines malformed ones without exposing content in logs.

## 9.2 Queue

```text
incoming adapter
→ durable spool acknowledgement point
→ bounded asyncio normalization queue
→ normalizer
→ correlator
→ WriterCommand queue
→ SQLite writer task
```

The service may acknowledge an OTLP request only after the request has been decoded enough to validate framing **and** its envelope has been durably accepted for processing.

If durable acceptance fails because of disk/permission exhaustion, return a protocol-correct retryable error; do not lie with HTTP 200.

## 9.3 Single writer

The writer owns:

```text
one sqlite3 connection
BEGIN/COMMIT batching
migration application
canonical event insert
projection table upserts
archive registry writes
source cursor writes
collector health writes
```

No FastAPI route receives this write connection.

Batch policy:

```text
commit when batch_size >= 100
OR oldest command age >= 50 ms
OR graceful shutdown
```

---

# 10. OTLP Adapter Contract

Endpoints:

```text
POST /v1/logs
POST /v1/metrics
POST /v1/traces
```

Supported headers:

```text
Content-Type: application/x-protobuf | application/json
Content-Encoding: absent | gzip
```

Pipeline per request:

```text
1. Validate method/path/media type.
2. Bound compressed + decompressed body size.
3. Decompress gzip when declared.
4. Compute SHA-256 over decompressed canonical wire body.
5. Decode signal-specific Export*ServiceRequest.
6. Produce one RawEnvelope for the request.
7. Spool durably.
8. Emit normalized signal records.
9. Return signal-specific Export*ServiceResponse using matching encoding.
```

For OTLP JSON, implement the OTLP JSON mapping rather than assuming ordinary protobuf JSON is identical. Add golden fixtures containing trace/span IDs and 64-bit timestamps.

Unknown attributes:

```text
preserve in JSON extension payload
never make unknown attribute a fatal parse condition
```

Unknown required structural changes:

```text
parse_status = incompatible
preserve digest/diagnostic
increment unknown_schema
return correct error/retry status if request cannot be safely accepted
```

Self metrics:

```text
observatory.otlp.requests
observatory.otlp.decode_errors
observatory.otlp.request_bytes
observatory.otlp.accept_latency_ms
```

---

# 11. Codex Configuration Installer

CLI:

```text
codex-observatory configure-codex --check
codex-observatory configure-codex --print
codex-observatory configure-codex --apply
codex-observatory configure-codex --apply --force
```

## 11.1 OTel edit behavior

Read user-level Codex `config.toml` preserving formatting/comments with `tomlkit`.

Required target values:

```toml
[otel]
environment = "local-observatory"
log_user_prompt = false
exporter = "otlp-http"
metrics_exporter = "otlp-http"
trace_exporter = "otlp-http"

[otel.exporter."otlp-http"]
endpoint = "http://127.0.0.1:8765/v1/logs"
protocol = "binary"

[otel.metrics_exporter."otlp-http"]
endpoint = "http://127.0.0.1:8765/v1/metrics"
protocol = "binary"

[otel.trace_exporter."otlp-http"]
endpoint = "http://127.0.0.1:8765/v1/traces"
protocol = "binary"
```

Before modifying:

```text
if any corresponding exporter is non-none and does not already target this observatory:
    --check => CONFLICT
    --apply => refuse, exit nonzero, print exact existing fields
    --apply --force => create timestamped backup, then replace only OTel keys
```

Never change `[analytics]` because Codex product analytics is distinct from the user-configured observatory export.

## 11.2 Hook edit behavior

Preferred user file:

```text
~/.codex/hooks.json
```

Before install:

```text
if inline user-level [hooks] exists:
    refuse automatic hook install
    tell operator to reconcile to one representation
```

Merge Observatory handlers by an exact command marker/path; preserve unrelated hooks. Always backup before write; write temp + atomic replace.

Recommended observational events:

```text
SessionStart
PermissionRequest
PostToolUse
UserPromptSubmit      # metadata only; never prompt body persistence
SubagentStart
SubagentStop
PreCompact
PostCompact
Stop
SessionEnd
Interrupt
```

`PreToolUse` is excluded from default installation because it sits on a control-sensitive path. It may be enabled in a diagnostic profile only with a handler that produces no decision/rewrite output.

---

# 12. Hook Handler Contract

`hooks/codex_observatory_hook.py` must use only the standard library so it remains runnable even when the observatory venv is unavailable.

Algorithm:

```text
read stdin bytes
parse JSON if possible
construct metadata envelope
redact/discard known content fields according to minimal mode
write atomic spool file to CODEX_OBSERVATORY_SPOOL or resolved default
emit no stdout
exit 0
```

Failure behavior:

```text
bad JSON            => best-effort diagnostic envelope, exit 0
spool unavailable   => no stdout, exit 0
collector offline   => irrelevant; hook writes file directly
```

Never:

```text
return decision=block
rewrite tool input
set continue=false
emit additionalContext
parse transcript_path contents
wait for FastAPI
```

---

# 13. App Server Adapter Contract

## 13.1 v1 modes

```text
disabled
reconcile       # DEFAULT
controlled_test # live protocol/fixture tests only
```

Do not expose an “attach” mode until official App Server documentation defines a non-mutating live subscription contract for arbitrary existing threads.

## 13.2 Reconcile mode

Launch a local `codex app-server` child over stdio JSONL.

Handshake:

```text
initialize
initialized
```

Then:

```text
thread/list with pagination
for new/changed thread:
    thread/read(includeTurns=true)
```

Persist summaries/turns as `app_server` evidence with source stability `documented`.

Do not call:

```text
turn/start
turn/steer
thread/goal/set
thread/archive
thread/delete
thread/metadata/update
thread/compact/start
thread/rollback
thread/inject_items
```

Do not call `thread/resume` merely to subscribe; it changes runtime state and violates the observatory boundary.

## 13.3 Controlled live mode

Used only by `tests/live/` to prove mappers against documented notifications in a session intentionally started through that App Server connection.

Capture:

```text
thread/started
thread/status/changed
turn/started
turn/completed
turn/diff/updated
turn/plan/updated
item/started
item/completed
item/commandExecution/outputDelta
thread/tokenUsage/updated
hook/started
hook/completed
model/rerouted
model/safetyBuffering/updated
```

For items, final state always comes from `item/completed`.

## 13.4 Schema compatibility

CLI:

```text
codex-observatory schema capture
codex-observatory schema verify
```

Capture command internally runs:

```text
codex --version
codex app-server generate-json-schema --out <version-dir>/json
codex app-server generate-ts --out <version-dir>/ts
```

Registry entry:

```json
{
  "codex_version": "...",
  "captured_at": "...",
  "json_schema_sha256": "...",
  "ts_schema_sha256": "...",
  "fixture_set_sha256": "...",
  "status": "SUPPORTED"
}
```

Unknown installed version:

```text
capture schemas
run contract fixture parser
if only additive fields => NEWER_UNVERIFIED + continue
if required semantic break => INCOMPATIBLE + disable affected adapter only
```

---

# 14. Git Adapter Contract

Git is invoked with subprocess argument arrays, never shell interpolation.

Capture:

```text
git rev-parse --show-toplevel
git rev-parse HEAD
git symbolic-ref --short -q HEAD
git status --porcelain=v2 --branch
git diff --numstat
git diff --cached --numstat
git config --get remote.origin.url
```

Normalize Windows path casing/separators only for identity calculation; preserve native displayed path.

`repo_id`:

```text
sha256(normalized_canonical_root + "\n" + normalized_remote_identity_or_empty)
```

`git_snapshot_id`:

```text
sha256(repo_id + head_sha + branch + staged_digest + unstaged_digest + untracked_name_digest)
```

Capture triggers:

```text
SessionStart hook
turn boundary when known
PostToolUse for mutation-capable tools, debounced
SessionEnd / Stop
manual API request for diagnostics
```

Never claim “committed” from a file-change event; only Git proves commit state.

---

# 15. Correlation and Operation Assembly

## 15.1 Exact joins

Only exact documented or version-verified identifiers may create confidence `>=0.99`.

Examples:

```text
same call_id
same thread_id
same turn_id
same App Server item_id
explicit parent agent/thread relationship
version-scoped verified alias mapping
```

## 15.2 Heuristic joins

`repo_time_window` requirements:

```text
same repo_id
compatible event category/tool
absolute event-time delta <= configured window
no competing candidate with equal/better score
```

Default window: 2 seconds.

`time_only` may support UI grouping but MUST NOT merge operations automatically when more than one plausible candidate exists.

## 15.3 Deduplication

Deduplicate only byte/identity duplicates from the **same source instance**:

```text
same source + source_instance + source_event identity/digest
```

Never delete cross-source corroborating evidence.

---

# 16. Query/API Contract

Base path:

```text
/api/v1
```

Health probes:

```text
GET /healthz     # process alive; no deep dependencies
GET /readyz      # migrations complete + writer ready + SQLite readable
```

Read endpoints:

```text
GET /api/v1/overview
GET /api/v1/sessions
GET /api/v1/sessions/{session_id}
GET /api/v1/threads/{thread_id}
GET /api/v1/turns/{turn_id}
GET /api/v1/events
GET /api/v1/tools
GET /api/v1/approvals
GET /api/v1/agents
GET /api/v1/skills
GET /api/v1/repos
GET /api/v1/costs
GET /api/v1/health
```

Allowed filters:

```text
from
to
repo_id
session_id
thread_id
turn_id
agent_id
model
tool
status
source_class
category
fact_type
```

Pagination:

```text
limit <= 500
after_seq for event streams
opaque cursor for non-event list endpoints
```

Error envelope:

```json
{
  "error": {
    "code": "invalid_filter",
    "message": "...",
    "details": {}
  }
}
```

No mutation endpoints are added in v1 except explicitly local maintenance endpoints if eventually required; initial v1 uses CLI for maintenance to keep HTTP read-only.

---

# 17. Live WebSocket Contract

Endpoint:

```text
WS /api/v1/live
```

Client subscribe message:

```json
{
  "type": "subscribe",
  "resume_from_sequence": 12345,
  "filters": {
    "session_id": null,
    "categories": ["tool", "turn", "agent"]
  }
}
```

Server event:

```json
{
  "type": "event",
  "sequence": 12346,
  "payload": {}
}
```

Server control messages:

```text
hello
heartbeat
replay_complete
reset_required
error
```

Rules:

```text
sequence == events.event_seq
on connect: replay rows > resume_from_sequence up to configured replay limit
then switch to live fanout
if requested sequence has been pruned: reset_required with oldest_available_sequence
heartbeat every 20s
idle timeout 60s without pong/client traffic
```

WebSocket delivery is a projection, not durable storage. Lost clients recover from `event_seq` via replay.

---

# 18. Parquet / DuckDB Archive Contract

Datasets:

```text
events
metrics
tool_calls
token_usage
approvals
spans
```

Path shape:

```text
<parquet_root>/<dataset>/year=YYYY/month=MM/day=DD/source=<source>/part-<uuid7>.parquet
```

Do not create a partition for every session/thread.

Each immutable batch gets a sidecar manifest:

```json
{
  "archive_id": "uuid7",
  "dataset": "events",
  "file": "...",
  "rows": 12043,
  "min_event_time": "...",
  "max_event_time": "...",
  "source_min_event_seq": 1,
  "source_max_event_seq": 12043,
  "schema": "codex.observatory.event.v1",
  "sha256": "..."
}
```

Verification order before prune:

```text
file exists
manifest parses
SHA-256 matches
DuckDB can open file
row_count matches
min/max timestamps match
canonical sampled rows equal SQLite source
archive_batches.verified_at set
ONLY THEN may hot rows be pruned
```

Never mutate existing Parquet files in place.

---

# 19. Privacy and Security Contract

## 19.1 Modes

```text
minimal (default)
  prompts: off
  tool args: digest
  tool output: digest
  durable raw wire body: off

diagnostic
  prompts: off
  selected structured arguments: allowlist only
  output: bounded sanitized excerpt
  durable raw wire body: off unless explicitly selected per source

forensic
  prompts: opt-in only
  arguments/output: full where permitted
  durable raw wire body: on
  retention: bounded
```

## 19.2 Redaction order

Before any textual content becomes durable:

```text
1. classify field
2. apply mode policy
3. redact configured secret patterns
4. enforce byte cap
5. persist
```

Digests use SHA-256 over canonical UTF-8 / canonical JSON encoding; document canonicalization in `docs/privacy.md`.

## 19.3 Server security

At config load:

```text
reject host not in {127.0.0.1, ::1, localhost} for v1
```

Do not add CORS wildcard. In production there is no cross-origin frontend.

Secrets:

```text
OPENAI_ADMIN_KEY: process environment only
never DB
never API response
never frontend bundle
never logs
```

---

# 20. Retention Contract

Defaults:

```text
SQLite normalized hot data     30 days
raw metadata                   7 days
forensic wire payload          7 days
Parquet normalized history     365 days
daily aggregates               indefinite
processing spool               <= 1 hour after successful normalization
```

Retention run:

```text
select eligible rows
build archive batch
write Parquet temp file
fsync/close
atomically promote file
write manifest
verify file + manifest
record archive_batches.verified_at
transactionally mark eligible hot range archived
prune only rows fully covered by verified archive
checkpoint WAL
emit retention event
```

If any verification fails:

```text
prune nothing from affected range
health = DEGRADED
preserve failure diagnostic
```

Hot-data pruning MUST follow referential order. Delete archive-covered child/projection rows first (`operation_evidence`, operations no longer referenced, spans/metrics/tool/approval/token/skill projections as applicable), then lifecycle rows that are fully outside retention and no longer active, and delete canonical `events` last. Never prune a still-open session/thread/turn solely because its start time is old. The retention integration test must run with foreign keys enabled so an incorrect deletion order fails loudly.

---

# 21. OpenAI Admin Adapter

Disabled by default.

Initialization:

```python
client = OpenAI(admin_api_key=os.environ["OPENAI_ADMIN_KEY"])
```

v1 sync scope:

```text
organization usage: completions first
organization costs
```

Add other usage families after generic bucket storage is proven.

Pagination:

```text
request page
persist returned buckets idempotently
if has_more:
    persist next_page cursor after committed page
    request next page
else:
    clear/advance cursor state
```

Retry:

```text
429/5xx => exponential backoff with jitter, bounded attempts per sync cycle
4xx auth/permission => adapter DEGRADED, no hot loop
```

Correlation classification:

```text
direct     = exact documented shared identifier exists
aggregate  = project/model/time bucket only
unknown    = cannot safely link
```

The UI must never transform `aggregate` into a per-session dollar amount.

---

# 22. Frontend Product Contract

## 22.1 Views in implementation order

```text
1. Health
2. Overview
3. Sessions
4. Session Detail / Timeline
5. Turns
6. Tools
7. Agents
8. Approvals
9. Skills
10. Git
11. Explorer
12. Costs (only when adapter enabled)
```

Health comes first in implementation even though Overview is the landing page; developers need to see collector failures while building the rest.

## 22.2 Required information semantics

Every detailed event renders:

```text
time
source_class
fact_type: native | derived | enriched | experimental
stability
correlation method/confidence when correlated
```

Unknown parent agent/thread is shown as `Unresolved`, never silently omitted.

## 22.3 Overview cards

```text
active sessions
active agents
turns today
tokens today
tool calls
tool failure rate
approval requests
approval denial rate
API p50/p95
turn p50/p95
collector health
```

Derived cards display `DERIVED` in the UI.

## 22.4 Timeline ordering

```text
ORDER BY event_time, event_seq
```

Never browser receive time.

## 22.5 API type generation

CI/build steps:

```text
start/import FastAPI app without collectors
export OpenAPI JSON
run openapi-typescript
write frontend/src/types/api.generated.ts
fail CI if generation changes committed output
```

---

# 23. Derived Analytics Contract

Initial formulas:

```text
cached_input_ratio = cached_input_tokens / input_tokens
output_input_ratio = output_tokens / input_tokens
agent_token_share = agent_tokens / session_tokens
agents_per_session = agent_spawns / sessions
failed_agent_rate = failed_agents / completed_agents
tool_success_rate = successful_calls / completed_calls
tool_failure_rate = failed_calls / completed_calls
approval_request_rate = approvals_requested / tool_calls
approval_denial_rate = denied / resolved_approvals
thread_skill_truncation_rate = truncated_threads / skill-bearing_threads
```

Null/zero denominator rule:

```text
return null, not 0 or infinity
```

Every derived metric response includes:

```json
{
  "fact_type": "derived",
  "formula_version": "analytics/1",
  "inputs": ["..."]
}
```

Do not add “productivity,” “quality,” or “intent” scores.

---

# 24. CLI Contract

Commands required for v1:

```text
codex-observatory init
codex-observatory serve
codex-observatory doctor
codex-observatory configure-codex --check|--print|--apply
codex-observatory migrate
codex-observatory archive run
codex-observatory retention run
codex-observatory admin-sync
codex-observatory schema capture|verify
codex-observatory verify-install
```

`doctor` checks:

```text
Python version
Node/frontend build presence
Codex executable + version
Codex OTel config and conflicts
hook representation/conflicts
hook spool writability
runtime paths
SQLite open/migration/WAL
port availability
App Server schema registry state
Admin key presence only when adapter enabled
Parquet path writability
clock sanity
```

Exit codes:

```text
0 healthy
1 degraded/nonfatal configuration issue
2 incompatible/blocking setup issue
```

---

# 25. Testing Architecture

## 25.1 Test tiers

```text
unit          pure transforms/formulas/config validation
contract      protocol fixtures, schema versions, migration digest
adapter       each external adapter with fake server/process
integration   FastAPI + SQLite + writer + spool + WebSocket
live          real installed Codex, opt-in/manual
end-to-end    Playwright against built app with deterministic fixture DB
failure       disk/lock/network/version fault injection
performance   synthetic ingest/query benchmarks
```

## 25.2 Deterministic fixtures

Store sanitized fixtures for:

```text
OTLP protobuf logs/metrics/traces
OTLP JSON logs/metrics/traces
gzip OTLP
Codex hook events
App Server list/read responses
App Server controlled-live notifications
Git status variants
Admin Usage/Costs pagination
```

Each fixture has:

```text
source version
capture date
SHA-256
redaction declaration
expected normalized record(s)
```

## 25.3 Required acceptance IDs

Preserve and implement the source suite, expanded as follows.

### OTLP

```text
A01 protobuf logs success
A02 protobuf metrics success
A03 protobuf traces preserve IDs/parentage
A04 malformed protobuf does not crash
A05 JSON OTLP equivalence with protobuf golden fixture
A06 gzip equivalence
A07 unsupported media type rejected
A08 decompression bomb / >64MiB rejected safely
A09 protocol-correct success response by signal/encoding
A10 durable spool failure returns retryable failure, not false success
```

### Codex native signal fixtures/live tests

```text
N01 conversation/session event
N02 API request event/metric
N03 turn.token_usage categories
N04 tool.call + duration
N05 approval.requested
N06 multi_agent.spawn when exercised
N07 skill.injected when exercised
```

### App Server

```text
B01 handshake required
B02 thread/list pagination
B03 thread/read does not call resume
B04 persisted turns normalize
B05 controlled-live turn lifecycle
B06 item/completed wins final item state
B07 restart reconciliation creates no duplicate thread
B08 unknown additive schema -> NEWER_UNVERIFIED
B09 semantic break disables adapter, not service
```

### Hooks

```text
C01 hooks-on vs hooks-off authorization outcome equivalent
C02 handler emits no stdout
C03 handler exits 0 on spool failure
C04 nonzero tool result still captured when Codex emits PostToolUse
C05 SubagentStart IDs normalized
C06 transcript_path contents never opened
C07 existing inline-hooks conflict blocks auto-install
```

### Correlation

```text
D01 exact native ID confidence >= .99
D02 independent evidence retained
D03 timestamp-only never high confidence
D04 competing heuristic candidates remain unresolved
D05 undocumented alias cannot become verified_alias
```

### Database

```text
E01 journal_mode=wal
E02 concurrent read while ingesting succeeds
E03 foreign keys enforced
E04 migrations replay from empty DB
E05 migration checksum mutation fails startup
E06 event_seq monotonic and restart-persistent
E07 single-writer invariant exercised under load
```

### Archive/retention

```text
F01 Parquet manifest values verified
F02 DuckDB canonical fields equal SQLite source
F03 Hive partition pruning test
F04 corrupt archive prevents prune
F05 missing manifest prevents prune
F06 verified range only is pruned
```

### Privacy/security

```text
P01 default Codex config keeps log_user_prompt=false
P02 store_prompts=false
P03 args/output digest-only
P04 secret canary absent from SQLite
P05 secret canary absent from Parquet
P06 secret canary absent from logs
P07 non-loopback bind rejected
P08 Admin key never returned by API
```

### Admin API

```text
G01 next_page fully consumed
G02 cursor persisted after committed page
G03 retry after 429 does not duplicate bucket
G04 usage/cost tables separate
G05 no direct session attribution without exact supported ID
```

### Dashboard

```text
U01 Overview cards equal backend aggregate fixtures
U02 timeline orders event_time,event_seq
U03 unknown parent visible
U04 source/fact labels visible
U05 reconnect resumes from event_seq
U06 pruned resume emits reset_required
U07 Health view surfaces degraded adapter
```

### Failure injection

```text
X01 OTLP request invalid/downstream writer unavailable
X02 App Server child disconnect
X03 SQLite busy/locked
X04 disk full
X05 Parquet export fail
X06 Admin API 429
X07 Admin API 500
X08 unknown Codex version
X09 hook spool unavailable
X10 clock skew
X11 corrupt spool record
```

Global invariant for X-series:

```text
Codex execution is not blocked by observatory telemetry failure.
```

---

# 26. Performance Gate

Synthetic workload definition:

```text
10 minutes sustained at 100 accepted telemetry envelopes/sec
plus one burst of 1,000 envelopes inside 1 second
50% metrics
30% logs/events
20% spans
concurrent 5 REST readers
concurrent 2 WebSocket clients
```

Targets on a normal local developer machine; record hardware with result:

```text
OTLP durable-accept p95          < 50 ms
normalization p95               < 25 ms
live UI/event delivery p95      < 500 ms
overview query p95              < 250 ms
session detail query p95        < 500 ms
accepted events lost            0
uncorrelated != dropped
writer queue never unbounded    true
```

These are project SLOs, not OpenAI guarantees.

---

# 27. CI Contract

`ci.yml`:

Matrix:

```text
windows-latest + Python 3.14 + Node 24
ubuntu-latest  + Python 3.14 + Node 24
```

Required steps:

```text
checkout
install uv
uv sync --locked
npm ci --prefix frontend
ruff check .
mypy src
pytest -m "not live" --cov
npm test --prefix frontend -- --run
build frontend
verify OpenAPI generated types clean
build Python package
run verify_release.py
Playwright Chromium smoke test on Windows or Ubuntu
```

`live-contract.yml`:

```text
workflow_dispatch only
requires Codex installed/authenticated on eligible runner
runs tests/live
captures installed codex --version
never writes resulting private telemetry fixtures back to repo automatically
```

---

# 28. Release Packaging

Build flow:

```text
npm ci --prefix frontend
npm run build --prefix frontend
uv run python scripts/build_frontend.py
uv build
```

`build_frontend.py`:

```text
delete generated static contents
copy frontend/dist/* -> src/codex_observatory/static/
write build-manifest.json with frontend package-lock hash + timestamp + git SHA
```

Wheel includes `static/**` as package data.

Installed experience:

```text
codex-observatory init
codex-observatory doctor
codex-observatory configure-codex --print
codex-observatory configure-codex --apply
codex-observatory serve
```

Browser target:

```text
http://127.0.0.1:8765/
```

No Node runtime required after the package has been built.

---

# 29. Sequential Implementation Roadmap

Each task card is executable by an agent. A phase gate blocks the next phase.

## PHASE 0 — Repository and contracts

### T0.1 — Scaffold repository

**Depends:** none  
**Create:** tree in §4.2, `.python-version`, base `pyproject.toml`, Vite React TS app, lockfiles, `.gitignore`, README stub.  
**Verify:**

```text
uv sync --locked
npm ci --prefix frontend
python -c "import codex_observatory"
npm run build --prefix frontend
```

### T0.2 — Write normative domain documents

**Depends:** T0.1  
**Create:** `docs/event-schema.md`, `source-contracts.md`, `privacy.md`, `retention.md`, `compatibility.md`.  
**Requirement:** copy the canonical classifications/invariants from this roadmap; no code yet.

### T0.3 — Implement config + paths

**Depends:** T0.1  
**Implement:** TOML load, Pydantic validation, platformdirs paths, loopback enforcement, env-only Admin credential detection.  
**Tests:** default config, invalid host, forensic constraints, path resolution, unknown keys policy.

### T0.4 — CLI skeleton

**Depends:** T0.3  
**Implement:** argparse command tree with commands from §24; unimplemented commands return explicit `NOT_IMPLEMENTED` until their phase lands.  
**Gate:** `codex-observatory doctor` runs and reports missing components without stack trace.

**PHASE-0 GATE:** fresh clone can install locked backend/frontend dependencies, import the package, build the frontend, parse default config, and execute CLI help/doctor.

---

## PHASE 1 — Durable storage foundation

### T1.1 — Migration engine

**Create:** migration loader, checksum verification, transactional apply.  
**Behavior:** applied migration checksum mismatch is fatal and names the version.

### T1.2 — Core schema

**Create:** migrations 0001–0005 from §8.  
**Add:** schema digest test generated from `sqlite_master` normalized DDL.

### T1.3 — SQLite connection policies

**Set on every connection:**

```sql
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
```

**Set on initialization writer connection:**

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
```

### T1.4 — Single writer actor

**Implement:** bounded `asyncio.Queue[WriterCommand]`, batch transactions, graceful flush, failure state.  
**No route exists yet.**

### T1.5 — Read repository

**Implement:** read-only connections and typed query helpers; no write SQL in query layer.

**PHASE-1 GATE:** E01–E07 pass; restart preserves monotonic `event_seq`; migration replay from empty DB is deterministic.

---

## PHASE 2 — Raw envelope and spool substrate

### T2.1 — Domain models

Implement `RawEnvelope`, `CanonicalEvent`, `CorrelationResult`, provenance/source enums, privacy retention classes.

### T2.2 — Atomic spool

Implement incoming/processing/failed state transitions with `os.replace`; crash-recovery scan is idempotent.

### T2.3 — Ingestion supervisor

Implement adapter registration, health, bounded normalization queue, stop/drain semantics, self-metrics counters.

### T2.4 — Minimal privacy enforcement

Implement content classification before durable writes. Add secret-canary tests at this layer before external adapters exist.

**PHASE-2 GATE:** simulated envelopes survive process restart, normalize exactly once, and leave no durable raw content in minimal mode.

---

## PHASE 3 — OTLP receiver

### T3.1 — Protocol codecs

Implement protobuf and OTLP JSON decoders/encoders per signal with golden fixtures.

### T3.2 — FastAPI app + lifespan

Create API app; lifespan initializes config, migrations, writer, ingest supervisor; shutdown drains queue/spool safely.

### T3.3 — OTLP routes

Implement `/v1/logs`, `/v1/metrics`, `/v1/traces`, gzip, size caps, proper Export responses, structured errors.

### T3.4 — Normalize logs

Map documented Codex event names without discarding provider attributes. Unknown events become canonical `diagnostic`/provider-extension events, not failures.

### T3.5 — Normalize metrics

Preserve metric name/type/labels/unit/histograms/exemplars and populate domain projections only for recognized Codex metrics.

### T3.6 — Normalize traces

Populate `spans` preserving trace/span IDs, parent IDs, nanosecond times, status, events, links, resource/scope attributes.

### T3.7 — Interoperability test

Use an OpenTelemetry-compliant emitter/Collector fixture to send protobuf, JSON, and gzip payloads.

**PHASE-3 GATE:** A01–A10 pass and a real Codex OTel session can be received in binary mode without prompts.

---

## PHASE 4 — Codex installer, hooks, and Git

### T4.1 — Codex config inspector

Discover user Codex config path, parse existing OTel and inline hooks, report conflicts without mutation.

### T4.2 — Safe OTel configuration writer

Implement backup + atomic TOML edit + `--force` semantics from §11.1.

### T4.3 — Standard-library hook writer

Implement one hook executable script with event-neutral behavior and atomic spool writes.

### T4.4 — `hooks.json` installer

Merge Observatory handlers without overwriting unrelated handlers; reject same-layer inline-hook conflict.

### T4.5 — Hook normalization

Map lifecycle metadata; do not read transcripts; tag all records source=`hook` and fact_type=`native` or `enriched` appropriately.

### T4.6 — Git snapshots

Implement exact commands, path normalization, repo/snapshot IDs, debounced triggers, non-repository handling.

### T4.7 — Hook neutrality experiment

Execute equivalent Codex actions with hooks disabled/enabled. Store only test evidence, not private prompt contents. Verify authorization result equivalence.

**PHASE-4 GATE:** C01–C07 + Git tests + privacy canary pass. Observability hook receiver may be stopped entirely without blocking Codex.

---

## PHASE 5 — Correlation and canonical operations

### T5.1 — Operation assembler

Implement `operations`/`operation_evidence`; exact duplicate suppression only within same source identity.

### T5.2 — Exact rules

Implement documented key joins. Every rule includes version and unit fixture.

### T5.3 — Version-verified alias registry

Create compatibility JSON mapping only after live test establishes a cross-source field equivalence for a specific Codex version.

### T5.4 — Heuristic rules

Implement repo/time scoring with ambiguity detection; no forced join on ties.

### T5.5 — Confidence surfaces

Add correlation method/confidence to API/domain output now, before UI exists.

**PHASE-5 GATE:** D01–D05 pass; cross-source evidence remains individually queryable after operation assembly.

---

## PHASE 6 — App Server reconciliation and compatibility

### T6.1 — JSONL process client

Implement subprocess lifecycle, request IDs, initialize handshake, response/notification demultiplexing, timeout/kill/restart.

### T6.2 — Schema capture

Implement `schema capture|verify`; populate compatibility registry for installed Codex.

### T6.3 — `thread/list` pagination

Persist thread summaries without subscription/control calls.

### T6.4 — `thread/read(includeTurns=true)` reconciliation

Normalize persisted turns/items and idempotently merge evidence.

### T6.5 — Controlled live mapper tests

Under `tests/live`, intentionally use a dedicated App Server connection to exercise turn/item notifications and prove stream mappers.

### T6.6 — Unknown-version behavior

Additive schema → warning and preserve unknown fields. Required semantic break → adapter `INCOMPATIBLE`, service remains operational.

**PHASE-6 GATE:** B01–B09 pass. No production code calls `thread/resume` solely for observation.

---

## PHASE 7 — Read API and live event API

### T7.1 — Health endpoints

Implement `/healthz`, `/readyz`, `/api/v1/health` with component status and last error/success.

### T7.2 — Read API schemas/routes

Implement endpoints in §16 with bounded pagination and filters.

### T7.3 — Aggregates

Implement Overview queries and percentile calculation from native histogram/sample data.

### T7.4 — WebSocket replay/live fanout

Implement sequence replay, heartbeat, filter, replay limit, reset-required behavior.

### T7.5 — OpenAPI generation

Export schema deterministically for frontend type generation.

**PHASE-7 GATE:** integration tests prove REST consistency, WS replay across reconnect, and no HTTP mutation authority.

---

## PHASE 8 — Parquet, DuckDB, and retention

### T8.1 — Immutable Parquet writer

Batch eligible SQLite ranges; partition by date/source; write temp then atomically promote.

### T8.2 — Manifest and archive registry

Calculate SHA-256, row count, min/max time, event seq range; persist verified registry.

### T8.3 — DuckDB analytics reader

Read Parquet datasets and expose analytics query functions. Do not persist authoritative state in DuckDB.

### T8.4 — Retention scheduler

Run in process every 24h and via CLI. Never prune before archive proof.

### T8.5 — Reconciliation tests

Compare DuckDB results to exact SQLite fixture ranges.

**PHASE-8 GATE:** F01–F06 pass, including corruption failure where SQLite data remains untouched.

---

## PHASE 9 — Frontend

### T9.1 — Shell/routing/API client

Create React root, routes, typed fetch client, common loading/error/empty states.

### T9.2 — Health view

Implement component table, last success/error, dropped/rejected counts, WAL size, backlog, clock skew, compatibility state.

### T9.3 — Overview

Implement cards/charts from backend aggregates only.

### T9.4 — Sessions + detail timeline

Implement filters, deterministic timeline ordering, source/fact badges, correlation confidence, raw expansion only when privacy permits.

### T9.5 — Turns/Tools/Agents/Approvals/Skills/Git

Implement domain views and derived labels.

### T9.6 — Explorer

Implement bounded filters and pagination; no arbitrary SQL.

### T9.7 — WebSocket integration

Reconnect with last acknowledged `event_seq`; apply updates to active views; recover with REST on `reset_required`.

### T9.8 — Accessibility and responsive pass

Keyboard navigation, semantic headings/tables, focus states, reduced-motion respect, no information encoded by color alone.

**PHASE-9 GATE:** U01–U07 + frontend unit tests + Playwright smoke tests pass.

---

## PHASE 10 — OpenAI Admin Usage/Costs

### T10.1 — Optional Admin client

Instantiate only when enabled and `OPENAI_ADMIN_KEY` exists.

### T10.2 — Completion usage sync

Implement time buckets, filters/config, `next_page`, idempotent bucket upsert, cursor persistence.

### T10.3 — Cost sync

Persist cost records separately; preserve project/line-item dimensions available from API.

### T10.4 — Cost UI

Display aggregate scope and correlation classification prominently. Never manufacture session costs.

**PHASE-10 GATE:** G01–G05 pass with mocked pagination/faults and an optional live read-only Admin API smoke test.

---

## PHASE 11 — Hardening, performance, packaging

### T11.1 — Failure injection suite

Implement X01–X11 with adapter-specific health transitions.

### T11.2 — Performance harness

Generate workload from §26; capture machine metadata and JSON result artifact.

### T11.3 — Build/package frontend

Implement static build copy and wheel package data.

### T11.4 — Release verification

`verify_release.py` creates a temporary clean data directory, starts installed wheel, runs migrations, serves UI, ingests fixture OTLP, queries REST, connects WebSocket, archives a batch, and shuts down cleanly.

### T11.5 — Documentation closure

Finish README install/use/troubleshooting, architecture, privacy, compatibility, recovery, and upgrade docs.

**PHASE-11 GATE:** CI green on Windows + Ubuntu, performance SLO recorded, release verifier green from built wheel.

---

# 30. Final v1 Real-World Acceptance Run

Run on Windows first because Windows compatibility is a product requirement.

Preconditions:

```text
built/installed codex-observatory
current Codex CLI installed
doctor = healthy or explicitly understood degraded optional adapter
minimal privacy mode
OTel + hooks configured
```

Execute a real Codex session that, where practical, performs:

```text
session start
at least one model turn
at least one shell/tool call
at least one file change
at least one Git snapshot change
at least one skill injection if available
at least one subagent if available
an approval request if the selected policy produces one
turn completion
session stop/end
```

Required evidence chain:

```text
OTel received
hook event(s) spooled and normalized
Git snapshot captured
canonical events persisted
operation correlation visible with confidence
App Server later reconciles persisted thread without controlling/resuming it
SQLite live view queries correctly
WebSocket emits/replays durable event_seq
Parquet archive verifies
DuckDB reproduces canonical analytics
React UI renders timeline and source/fact labels
restart creates no duplicate canonical source records
prompt canary/raw user prompt absent in minimal mode
```

If an optional signal did not occur naturally (approval/subagent/skill), run a targeted live contract scenario for that signal; do not fake its presence in the main session.

---

# 31. Definition of Done

v1 is DONE only when all are true:

```text
[ ] Empty-repo bootstrap is reproducible from committed lockfiles.
[ ] Python/Node/Codex compatibility is surfaced by doctor.
[ ] OTel logs, metrics, and traces ingest correctly.
[ ] Hooks are observational and fail open with respect to Codex execution.
[ ] Git snapshots are command-derived.
[ ] App Server reconciliation never requires thread control/resume.
[ ] Independent source evidence remains immutable and queryable.
[ ] Correlation confidence/method is visible.
[ ] SQLite uses one-writer WAL architecture.
[ ] WebSocket resume uses durable event_seq.
[ ] Raw content is absent by default.
[ ] Archive verification precedes prune.
[ ] DuckDB reproduces archived analytics.
[ ] React UI has all required v1 views.
[ ] Admin cost data remains aggregate unless exact identity exists.
[ ] Failure injection cannot cause observatory telemetry to block Codex.
[ ] Performance gate passes or documented deviation blocks release.
[ ] Windows + Ubuntu CI passes.
[ ] Built wheel serves the production frontend without Node installed.
[ ] Real Codex acceptance run passes.
```

---

# 32. Explicit v1 Non-Goals

Do not expand v1 into:

```text
policy enforcement
approval granting or denial
tool input rewriting
automatic turn execution
a Codex replacement client
remote multi-user dashboard
SIEM replacement
employee/user productivity scoring
LLM anomaly verdicts
automatic ChatGPT-session dollar allocation
undocumented Codex DB parsing
arbitrary transcript parsing
production deployment authority
```

Any future feature that crosses from observation into authorization must be designed as a separate control-plane component with a new threat model and acceptance contract.

---

# 33. Known Residual Questions — Non-blocking for v1

These remain intentionally unresolved because they are future-product choices rather than prerequisites for a correct v1.

```text
RQ-001 Whether to add an OpenTelemetry Collector fan-out mode for users who already export Codex OTel elsewhere.
RQ-002 Whether future official App Server APIs will expose a non-mutating subscribe-to-existing-thread operation.
RQ-003 Whether to add OS-native secret storage instead of environment-only Admin keys.
RQ-004 Whether to ship a signed Windows executable wrapper in addition to the Python wheel.
RQ-005 Whether to add remote access; doing so requires a separate TLS/auth/CSRF/WS-origin/RBAC design.
RQ-006 Whether forensic payload encryption-at-rest should be added before expanding forensic mode beyond single-user local use.
```

These questions MUST NOT be “solved” opportunistically during v1 implementation.

---

# 34. Agent Handoff — Exact First Work Slice

Start here and do nothing beyond this slice until its gate is green:

```text
GOAL
Bootstrap the empty codex-observatory repository and establish the immutable v1 contracts.

SCOPE
- Create the repository tree through docs/, src/, frontend/, tests/, scripts/, compatibility/.
- Configure Python >=3.14,<3.15 with uv and commit uv.lock.
- Scaffold React + TypeScript + Vite with Node 24/npm and commit package-lock.json.
- Add only the dependency sets listed in §§4.4–4.5.
- Implement config.py, paths.py, CLI skeleton, and doctor skeleton.
- Write docs/event-schema.md, source-contracts.md, privacy.md, retention.md, compatibility.md from this roadmap.
- Do NOT implement collectors, DB schema, UI views, App Server control, or Codex config mutation yet.

VERIFY
uv sync --locked
uv run python -c "import codex_observatory"
uv run codex-observatory --help
uv run codex-observatory doctor
npm ci --prefix frontend
npm run build --prefix frontend

ACCEPT
- All commands run from a clean checkout.
- doctor reports missing/not-yet-configured external capabilities as structured status, not exceptions.
- no runtime database/data directory is committed.
- loopback-only server default is present in config contract.
- default privacy contract states prompts/raw args/raw output are not persisted.
- git status is clean after generated artifacts that belong in source are committed.

STOP
Return the files changed, commands executed, test/build results, and any evidence that the current environment contradicts this roadmap. Do not begin Phase 1 automatically.
```

---

# 35. Final Architectural Invariant

```text
The model reasons.
The control plane authorizes.
The runtime executes.
Git identifies engineering state.
The observatory reconstructs and measures what happened.
```

The success criterion is not “collect lots of logs.” It is **reconstructible, source-labeled, privacy-bounded evidence whose uncertainty is explicit and whose failure cannot take control of Codex execution**.
