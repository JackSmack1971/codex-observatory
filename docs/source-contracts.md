# Source contracts v1

The observatory is read-only with respect to Codex execution. No collector or dashboard endpoint may grant approvals, modify a tool invocation, change sandbox policy, or start a Codex turn.

## Sources

| Source | Role | Authority | Phase-0 state |
| --- | --- | --- | --- |
| Codex OTel | logs, metrics, traces | asynchronous telemetry | not implemented |
| Lifecycle hooks | rich local lifecycle facts | hook payload plus receipt metadata | not implemented |
| App Server | persisted thread reconciliation | versioned JSON/TS schema | not implemented |
| Git | repository snapshots | command output at a repository cwd | not implemented |
| OpenAI Admin API | optional aggregate usage/costs | remote admin data | not implemented |

Each source must preserve its observed version and provenance. A live contract that differs from a fixture/schema for the detected Codex version fails compatibility visibly; it is not silently adapted.

## Failure behavior

Collectors must be independently stoppable and must not block Codex execution. Later phases will use bounded queues, explicit dropped/rejected counts, and a single SQLite writer. Those mechanisms are contracts, not Phase-0 implementations.
