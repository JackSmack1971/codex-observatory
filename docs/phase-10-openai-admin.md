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
only. It uses a 24-hour initial lookback and a one-hour overlap on later syncs.

Usage results are organization/API evidence, never local Codex session
attribution. Result identity is the SHA-256 of bucket boundaries and returned
grouping dimensions. A changed upstream result creates a new revision while
the previous revision remains durable and non-current.

Sync state is independent of facts and records the requested/completed window,
in-progress cursor, page/bucket counts, last success, and last error. Health
states are `ADMIN_DISABLED`, `ADMIN_CREDENTIAL_MISSING`, `ADMIN_READY`,
`ADMIN_SYNCING`, `ADMIN_HEALTHY`, `ADMIN_DEGRADED`, and `ADMIN_FAILED`.

Read-only inspection is available at `/api/v1/admin/usage/completions` and
`/api/v1/admin/usage/health`. Costs and other usage families are out of scope.
