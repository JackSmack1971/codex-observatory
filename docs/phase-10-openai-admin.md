# OpenAI Admin completions usage

The optional adapter uses the official `openai==3.16.2` Python SDK and its
read-only `GET /organization/usage/completions` operation. It is instantiated
only when `collectors.openai_admin.enabled` is true and `OPENAI_ADMIN_KEY` is
present in the process environment. The key is never stored, logged, or sent
to the frontend.

The API uses Unix-second `start_time` inclusive and `end_time` exclusive
boundaries. Supported bucket widths are `1m`, `1h`, and `1d`; the example
configuration uses `1h` and groups by `project_id` and `model`. The adapter
follows `has_more` and `next_page`, treating `next_page` as a page-chain cursor
only. The initial request uses the configured lookback; after a successful
sync, every later request starts at `completed_end - overlap`, even after a
multi-day outage. This preserves continuity and lets the overlap deduplicate
revisions at the read boundary.

Usage results are organization/API evidence, never local Codex session
attribution. Result identity is the SHA-256 of bucket boundaries and returned
grouping dimensions. A changed upstream result creates a new revision while
the previous revision remains durable and non-current.

Sync state is independent of facts and records the requested/completed window,
in-progress cursor, page/bucket counts, last success, and last error. Health
states are `ADMIN_DISABLED`, `ADMIN_CREDENTIAL_MISSING`, `ADMIN_READY`,
`ADMIN_SYNCING`, `ADMIN_HEALTHY`, `ADMIN_DEGRADED`, and `ADMIN_FAILED`.
Only one local Admin sync may run at a time; a concurrent invocation fails fast
with `ADMIN_BUSY` and cannot change the active sync's cursor or window. Admin
transport errors pass through one sanitization boundary before persistence,
health, CLI output, or re-raising.

The public usage read maps an explicit allow-list of contract fields. Internal
revision fields such as `is_current` and `result_sha256` are persistence-only.

Read-only inspection is available at `/api/v1/admin/usage/completions` and
`/api/v1/admin/usage/health`. Costs and other usage families are out of scope.
