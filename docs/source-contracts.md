# Source contracts v1

The observatory is read-only with respect to Codex execution. No collector or dashboard endpoint may grant approvals, modify a tool invocation, change sandbox policy, or start a Codex turn.

## Sources

| Source | Role | Authority | Phase-1 state |
| --- | --- | --- | --- |
| Codex OTel | logs, metrics, traces | asynchronous telemetry | OTLP/HTTP receiver implemented |
| Lifecycle hooks | rich local lifecycle facts | hook payload plus receipt metadata | not implemented |
| App Server | persisted thread reconciliation and controlled notification evidence | versioned JSON/TS schema | Phase 2 implemented |
| Codex Hooks | lifecycle command-hook observation | current Codex Hooks JSON stdin contract | Phase 3 implemented; enrichment only |
| Git | repository snapshots | command output at a repository cwd | Phase 4 implemented; read-only |
| OpenAI Admin API | optional aggregate completions usage | `GET /organization/usage/completions`, SDK `openai==3.16.2` | implemented; costs remain out of scope |

OpenAI Admin completions rows are durable historical evidence and are retained
indefinitely in this phase. They are not part of v1 retention pruning; a future
archive/coverage contract is required before pruning can be added.

Each source must preserve its observed version and provenance. A live contract that differs from a fixture/schema for the detected Codex version fails compatibility visibly; it is not silently adapted.

## Failure behavior

Collectors must be independently stoppable and must not block Codex execution. Phase 1 records accepted and rejected OTLP evidence in SQLite; later phases will add bounded queues, explicit dropped counts, and a dedicated writer actor.
