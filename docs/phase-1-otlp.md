# Phase 1 OTLP ingestion foundation

The local receiver accepts `POST /v1/logs`, `/v1/metrics`, and `/v1/traces` on
loopback. It accepts OTLP binary protobuf (`application/x-protobuf`) and OTLP
JSON (`application/json`), with optional `Content-Encoding: gzip` and a 64 MiB
decompressed body limit. Responses use the matching signal-specific OTLP
response and media type. Malformed or unsupported requests return a diagnostic
OTLP `Status` body and are recorded in `raw_events`.

The pipeline is transport decode -> immutable `RawEnvelope` metadata -> raw
SQLite evidence -> canonical `event.v1` rows -> collector health. Unknown
Codex event names and native attributes are retained; prompt, tool argument,
and tool output keys are removed from canonical minimal-mode data. Durable wire
payload bytes are not stored in minimal mode. An exact replay is retained as a
second raw row with `parse_status=duplicate` and does not create another event.

SQLite is created through numbered migrations and uses WAL, `foreign_keys=ON`,
`busy_timeout=5000`, and `synchronous=NORMAL`. Read-only evidence is available
at `GET /health`, `GET /events`, and `GET /raw-events`; the CLI provides
`codex-observatory health`, `query`, and `serve`.

## Codex compatibility

Current Codex documentation describes OTel as opt-in, with separate log,
metric, and trace exporters; representative events include
`codex.conversation_starts`, `codex.api_request`, `codex.tool_result`, and
`codex.tool_decision`. Current metrics include `codex.api_request`,
`codex.api_request.duration_ms`, `codex.tool.call`, and
`codex.tool.call.duration_ms`. The default `otel.log_user_prompt = false` is
compatible with this collector's minimal privacy behavior.

The receiver does not mutate Codex configuration. A real Codex export requires
the user to configure the documented exporter endpoint in their user-level
Codex config; no automatic configuration is performed in this phase.

## Implementation evidence and limits

OTLP protobuf attributes arrive as repeated `KeyValue` messages, not as a
Python mapping. The adapter recursively preserves OTLP `AnyValue` strings,
booleans, integers, doubles, bytes (base64), arrays, and key/value lists.
Histogram metrics remain histogram observations: count, sum, bucket counts,
explicit bounds, timestamp, and point attributes are stored without inventing
a scalar `value`. A valid transport payload that fails semantic normalization
is retained as a raw `normalization_failed` diagnostic.

Each FastAPI request owns and closes its SQLite connection. The receiver does
not disable SQLite thread checking on a shared connection. Replays are
identified by `(source_event_type, payload_sha256)`: the later raw envelope is
retained as `duplicate` and produces no second canonical event.

The real local gate was run with Codex CLI `0.155.1` using command-line OTel
overrides and `otel.log_user_prompt=false`; it produced 12 log, 1 metric, and
2 trace requests, 1,020 canonical events, zero rejected records, zero
persistence errors, and healthy final collector state. This is an
implementation-specific observation, not a guarantee about every Codex
version or workload. Prompt-bearing event names/metadata may remain as
non-content identifiers; prompt text itself is excluded in minimal mode, and
raw wire payloads are not persisted.

Authoritative sources consulted on 2026-09-20:

- [Codex advanced configuration](https://developers.openai.com/codex/config-advanced/)
- [Codex configuration reference](https://developers.openai.com/codex/config-reference/)
- [OTLP specification 1.11.0](https://opentelemetry.io/docs/specs/otlp/)
- [SQLite WAL](https://www.sqlite.org/wal.html)
- [SQLite foreign keys](https://www.sqlite.org/foreignkeys.html)
- [SQLite busy timeout](https://www.sqlite.org/c3ref/busy_timeout.html)
