# Phase 2 — App Server observation

Phase 2 adds a read-only Codex App Server source adapter. It launches the
documented stdio JSONL server, sends `initialize` followed by `initialized`,
pages `thread/list`, reads stored threads with `thread/read` (without resuming
or subscribing), and discovers in-memory IDs with `thread/loaded/list`.

The production allowlist is intentionally limited to those four methods.
Mutating methods, experimental history endpoints, and deprecated
`thread/compacted` are rejected or unused. Notifications are kept as raw
metadata plus canonical `event.v1` evidence; unknown notifications are
preserved and counted.

Migration 2 adds source health, messages, threads, turns, thread items, and
App Server token-usage evidence. Native IDs are primary keys. `item/completed`
is final authority, and terminal turns cannot regress to `inProgress` on a
replay. Minimal privacy removes prompt/text, reasoning, arguments, and output
content while preserving structural metadata and digests where applicable.

Reconnect restarts the stdio child, repeats initialization and stable
reconciliation, increments `reconnect_total`, and relies on native-ID upserts
for idempotency. It never resumes a thread or issues a turn/control method.

Stable read APIs cannot reconstruct notifications that occurred while the
observatory was disconnected. Reconciliation therefore marks state as
reconciled state, never as reconstructed historical events. Live notification
coverage requires a connection intentionally subscribed by a controlled test
driver; App Server has no documented passive subscription to arbitrary loaded
threads.

Run the local, read-only gate with:

```text
uv run python scripts/app_server_gate.py --codex C:\Users\<user>\AppData\Roaming\npm\codex.cmd
```

The gate uses a temporary database and does not change Codex configuration or
issue control/mutation methods. It reports stored/loaded threads and whether
persisted turns/items were available for observation.

Authoritative documentation consulted on 2026-09-20:

- [Codex App Server](https://developers.openai.com/codex/app-server)
- [Codex App Server protocol source](https://github.com/openai/codex/tree/main/codex-rs/app-server-protocol)
