# Phase 3 — lifecycle-hook observation

Phase 3 adds a privacy-first, observational Codex command-hook adapter. The
handler reads one JSON object from stdin, redacts protected values, and writes
one durable JSON record to a per-invocation file under the platformdirs-resolved
observatory data directory. `hooks-import` imports those records into the
existing `RawEnvelope`/`event.v1` pipeline and SQLite.

Per-invocation files are used instead of a localhost HTTP receiver because they
are local-only, tolerate the collector being stopped, avoid listener lifetime
and port races on Windows, and make partial or malformed records diagnosable as
`.bad` files. Unique file creation is safe for concurrent hook processes; the
importer is restart-safe and duplicate-safe by source event and payload digest.

The production command is deliberately neutral: it exits 0, emits no stdout or
stderr, and never returns a hook decision, continuation, context, updated input,
updated permissions, or system message. `PermissionRequest` is recorded as an
approval request observation with `decision=none`; it never approves or denies.
`PostToolUse` is preferred for tool observation and records nonzero Bash results
as failure evidence when the native response exposes an exit code or success
flag.

## Events

The adapter supports `SessionStart`, `SessionEnd`, `PostToolUse`,
`PermissionRequest`, `PreCompact`, `PostCompact`, `SubagentStart`,
`SubagentStop`, `Stop`, and `Interrupt`. `UserPromptSubmit` and `PreToolUse`
are structurally accepted. Unknown names are retained as `hook/unknown` and
counted. Transcript paths are metadata only; transcript files are never read.

Minimal privacy replaces prompt text, last assistant messages, tool input and
tool response values, and subagent transcript paths with a digest marker before
the spool record is written. The SQLite raw payload remains absent in minimal
mode. Tool input descriptions are retained only as approval metadata.

## Commands

```text
codex-observatory hook --spool <optional-spool-root>
codex-observatory hooks-import --db <db> --spool <optional-spool-root>
```

For a temporary integration, configure command hooks in a disposable project
`.codex/hooks.json` with absolute Windows `commandWindows` paths, use `async`
where supported, and remove the file after the gate. `SessionEnd` is documented
as synchronous and `Interrupt` has a one-second default and three-second
maximum timeout. Background hooks cannot block, approve, rewrite, or control
the triggering operation; unfinished background hooks may be cancelled when a
session ends.

Doctor reports hook health independently as `HOOKS_DISABLED`,
`HOOKS_CONFIGURED_NOT_OBSERVED`, `HOOKS_HEALTHY`, or `HOOKS_DEGRADED`.

## Official contract consulted

The implementation follows the current [Codex Hooks documentation](https://developers.openai.com/codex/hooks), including event fields, matchers,
stdin JSON, exit-0/no-output success, trust and project/user discovery,
`commandWindows`, asynchronous behavior, timeout limits, tool coverage, and
the warning that `transcript_path` is not a stable data contract.

The live gate used Codex CLI `0.155.1` with the `hooks` feature enabled and a
temporary trusted project configuration. `SessionStart` and `SessionEnd` were
observed. The non-interactive `codex exec` path did not emit `PostToolUse` for
executed shell commands; this is an external Codex runtime limitation, not a
collector normalization failure. The production adapter remains ready for
`PostToolUse` when the runtime emits it.
