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
`/api/v1/admin/usage/health`.

## Admin Usage & Costs dashboard

The read-only dashboard destination `/admin` presents fixed, UTC-bounded
aggregates from the persisted Admin evidence. Its source is the OpenAI Admin
API and its scope is organization/API usage and billing evidence. It does not
establish attribution to a local Codex session, thread, turn, or agent; that
attribution is unavailable in this slice.

The page exposes independent completions and costs health, including disabled,
credential-missing, ready, syncing, healthy, degraded, failed, and busy states
when supplied by the adapters. A missing `OPENAI_ADMIN_KEY` is rendered as
credential missing and no credential material is returned by the API.

Supported presentation intervals are `24h`, `7d`, and `30d`, using UTC
inclusive/exclusive bucket overlap semantics. Usage groups are limited to
persisted dimensions. Cost totals are exact decimal strings and remain
separate when more than one currency is present; currencies are never
converted. Empty intervals and absent successful syncs are shown as unavailable
or no data rather than as zero. The dashboard uses the existing live
invalidation path to refetch authoritative HTTP data; Admin bodies are not
streamed as a second frontend state source.

Typed summary surfaces are `/api/v1/admin/summary`,
`/api/v1/admin/usage/completions/summary`, and
`/api/v1/admin/costs/summary`. Each response carries explicit
`source=openai_admin_api`, `scope=organization`, and
`attribution=unavailable` provenance.

The optional costs adapter reads `GET /organization/costs` through
`client.admin.organization.usage.costs` in the locked `openai==3.16.2` SDK.
It uses Unix-second inclusive/exclusive bounds, fixed `bucket_width="1d"`,
`group_by` values `project_id`, `line_item`, or `api_key_id`, optional project,
line-item, and API-key filters, `limit` 1–180, and page cursors from
`next_page`. Results are `bucket` objects containing nullable
`organization.costs.result` dimensions and optional `amount {currency, value}`,
`quantity`, and `quantity_unit`. Durable numeric values are canonical decimal
strings. This is organization/API billing evidence, never local Codex session,
thread, turn, or agent cost. Costs own separate sync state and health, and are
classified as `HISTORICAL_EVIDENCE` until archive coverage exists. Costs use a
SQLite lease in addition to the in-process lock, so separate CLI processes
share the `ADMIN_COSTS_BUSY` guard; an expired lease can be reclaimed after a
crashed process.
