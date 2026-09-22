"""Read-only OpenAI organization costs evidence adapter."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from .config import OpenAIAdminConfig
from .openai_admin import _error_status, _value, sanitize_admin_error
from .sqlite import utc_now

ADAPTER_SCHEMA_VERSION = "openai-admin-costs-v1"
COLLECTOR = "openai_admin_costs"
ENDPOINT = "/organization/costs"
PUBLIC_COST_FIELDS = (
    "result_identity",
    "revision",
    "source",
    "evidence_scope",
    "bucket_start",
    "bucket_end",
    "bucket_width",
    "project_id",
    "line_item",
    "api_key_id",
    "amount_value",
    "currency",
    "quantity_value",
    "quantity_unit",
    "request_start",
    "request_end",
    "retrieved_at",
    "adapter_schema_version",
    "first_observed_at",
    "last_observed_at",
)
_SYNC_LOCK = threading.Lock()
_LOCK_LEASE_SECONDS = 300


class CostsClient(Protocol):
    def costs(self, **kwargs: Any) -> Any: ...


def _validate_page(page: Any) -> tuple[list[Any], bool, str | None]:
    data, more, next_page = (
        _value(page, "data"),
        _value(page, "has_more"),
        _value(page, "next_page"),
    )
    if (
        _value(page, "object") != "page"
        or not isinstance(data, list)
        or not isinstance(more, bool)
        or (next_page is not None and not isinstance(next_page, str))
    ):
        raise ValueError("malformed costs response")
    if more and not next_page:
        raise ValueError("costs response has_more without next_page")
    return data, more, next_page


def health(
    connection: Any, *, enabled: bool, credential_present: bool
) -> dict[str, Any]:
    if not enabled:
        return {
            "collector": COLLECTOR,
            "status": "ADMIN_COSTS_DISABLED",
            "reason": "costs collector disabled",
            "last_success": None,
            "last_error": None,
        }
    if not credential_present:
        return {
            "collector": COLLECTOR,
            "status": "ADMIN_COSTS_CREDENTIAL_MISSING",
            "reason": "OPENAI_ADMIN_KEY is absent",
            "last_success": None,
            "last_error": None,
        }
    row = connection.execute(
        "SELECT * FROM openai_cost_sync_state WHERE collector=?", (COLLECTOR,)
    ).fetchone()
    if not row:
        return {
            "collector": COLLECTOR,
            "status": "ADMIN_COSTS_READY",
            "reason": "credential available; no sync completed",
            "last_success": None,
            "last_error": None,
        }
    return {
        "collector": COLLECTOR,
        "status": row["status"],
        "reason": "last costs synchronization state",
        "last_success": row["last_success"],
        "last_error": sanitize_admin_error(row["last_error"])
        if row["last_error"]
        else None,
    }


def _set_state(
    connection: Any,
    status: str,
    *,
    start: int | None = None,
    end: int | None = None,
    completed_start: int | None = None,
    completed_end: int | None = None,
    cursor: str | None = None,
    success: str | None = None,
    error: str | None = None,
    pages: int | None = None,
    buckets: int | None = None,
) -> None:
    old = connection.execute(
        "SELECT * FROM openai_cost_sync_state WHERE collector=?", (COLLECTOR,)
    ).fetchone()

    def oldv(key: str, default: Any = None) -> Any:
        return old[key] if old else default

    connection.execute(
        """INSERT INTO openai_cost_sync_state(collector,status,requested_start,requested_end,completed_start,completed_end,page_cursor,last_success,last_error,pages_total,buckets_total,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(collector) DO UPDATE SET status=excluded.status,requested_start=excluded.requested_start,requested_end=excluded.requested_end,completed_start=excluded.completed_start,completed_end=excluded.completed_end,page_cursor=excluded.page_cursor,last_success=excluded.last_success,last_error=excluded.last_error,pages_total=excluded.pages_total,buckets_total=excluded.buckets_total,updated_at=excluded.updated_at""",
        (
            COLLECTOR,
            status,
            start if start is not None else oldv("requested_start"),
            end if end is not None else oldv("requested_end"),
            completed_start if completed_start is not None else oldv("completed_start"),
            completed_end if completed_end is not None else oldv("completed_end"),
            cursor,
            success if success is not None else oldv("last_success"),
            error,
            pages if pages is not None else oldv("pages_total", 0),
            buckets if buckets is not None else oldv("buckets_total", 0),
            utc_now(),
        ),
    )


def _acquire_database_lock(connection: Any, owner_id: str) -> bool:
    """Acquire the costs lease across threads and OS processes."""
    try:
        connection.execute("PRAGMA busy_timeout=0")
        connection.execute("BEGIN IMMEDIATE")
    except Exception:  # noqa: BLE001 - a locked SQLite writer means busy.
        connection.rollback()
        connection.execute("PRAGMA busy_timeout=5000")
        return False
    now = int(time.time())
    row = connection.execute(
        "SELECT owner_id,lease_expires_at FROM openai_cost_sync_lock WHERE lock_id=1"
    ).fetchone()
    if row and int(row["lease_expires_at"]) > now:
        connection.rollback()
        connection.execute("PRAGMA busy_timeout=5000")
        return False
    connection.execute(
        "INSERT INTO openai_cost_sync_lock(lock_id,owner_id,lease_expires_at) VALUES(1,?,?) "
        "ON CONFLICT(lock_id) DO UPDATE SET owner_id=excluded.owner_id,lease_expires_at=excluded.lease_expires_at",
        (owner_id, now + _LOCK_LEASE_SECONDS),
    )
    connection.commit()
    connection.execute("PRAGMA busy_timeout=5000")
    return True


def _release_database_lock(connection: Any, owner_id: str) -> None:
    with connection:
        connection.execute(
            "DELETE FROM openai_cost_sync_lock WHERE lock_id=1 AND owner_id=?",
            (owner_id,),
        )


def _decimal(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError(f"invalid costs {name}")
    try:
        decimal = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"invalid costs {name}") from exc
    if not decimal.is_finite():
        raise ValueError(f"invalid costs {name}")
    return format(decimal, "f")


def _normalize(
    bucket_start: int,
    bucket_end: int,
    result: Any,
    start: int,
    end: int,
    retrieved: str,
) -> dict[str, Any]:
    if _value(result, "object") != "organization.costs.result":
        raise ValueError("unexpected costs result object")
    amount = _value(result, "amount")
    row: dict[str, Any] = {
        "source": "openai_admin_api",
        "evidence_scope": "organization_api_billing",
        "bucket_start": bucket_start,
        "bucket_end": bucket_end,
        "bucket_width": "1d",
        "project_id": _value(result, "project_id"),
        "line_item": _value(result, "line_item"),
        "api_key_id": _value(result, "api_key_id"),
        "amount_value": _decimal(_value(amount, "value"), "amount")
        if amount is not None
        else None,
        "currency": _value(amount, "currency") if amount is not None else None,
        "quantity_value": _decimal(_value(result, "quantity"), "quantity"),
        "quantity_unit": _value(result, "quantity_unit"),
        "request_start": start,
        "request_end": end,
        "retrieved_at": retrieved,
        "adapter_schema_version": ADAPTER_SCHEMA_VERSION,
    }
    for key in ("project_id", "line_item", "api_key_id", "currency", "quantity_unit"):
        if row[key] is not None and not isinstance(row[key], str):
            raise ValueError(f"invalid costs field: {key}")
    identity = {
        key: row[key]
        for key in (
            "bucket_start",
            "bucket_end",
            "project_id",
            "line_item",
            "api_key_id",
        )
    }
    row["result_identity"] = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    content = {
        key: row[key]
        for key in row
        if key not in {"retrieved_at", "request_start", "request_end"}
    }
    row["result_sha256"] = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    return row


def _store(connection: Any, row: dict[str, Any]) -> None:
    current = connection.execute(
        "SELECT * FROM openai_costs WHERE result_identity=? AND is_current=1",
        (row["result_identity"],),
    ).fetchone()
    if current and current["result_sha256"] == row["result_sha256"]:
        connection.execute(
            "UPDATE openai_costs SET last_observed_at=? WHERE result_identity=? AND is_current=1",
            (row["retrieved_at"], row["result_identity"]),
        )
        return
    revision = int(current["revision"]) + 1 if current else 1
    if current:
        connection.execute(
            "UPDATE openai_costs SET is_current=0 WHERE result_identity=? AND is_current=1",
            (row["result_identity"],),
        )
    fields = (
        "result_identity",
        "source",
        "evidence_scope",
        "bucket_start",
        "bucket_end",
        "bucket_width",
        "project_id",
        "line_item",
        "api_key_id",
        "amount_value",
        "currency",
        "quantity_value",
        "quantity_unit",
        "request_start",
        "request_end",
        "retrieved_at",
        "adapter_schema_version",
        "result_sha256",
    )
    connection.execute(
        f"INSERT INTO openai_costs({','.join(fields)},revision,first_observed_at,last_observed_at,is_current) VALUES ({','.join('?' for _ in fields)},?,?,?,?)",
        [
            *(row[key] for key in fields),
            revision,
            row["retrieved_at"],
            row["retrieved_at"],
            1,
        ],
    )


def _sync(
    connection: Any,
    config: OpenAIAdminConfig,
    client: CostsClient,
    *,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int = 3,
) -> dict[str, Any]:
    if not config.costs_enabled:
        _set_state(connection, "ADMIN_COSTS_DISABLED")
        return health(connection, enabled=False, credential_present=False)
    end = int((now or datetime.now(UTC)).astimezone(UTC).timestamp())
    state = connection.execute(
        "SELECT * FROM openai_cost_sync_state WHERE collector=?", (COLLECTOR,)
    ).fetchone()
    if state and state["page_cursor"] and state["requested_end"]:
        start, end, cursor = (
            state["requested_start"],
            state["requested_end"],
            state["page_cursor"],
        )
    else:
        start = end - config.initial_lookback_hours * 3600
        if state and state["completed_end"] is not None:
            start = int(state["completed_end"]) - config.overlap_hours * 3600
        cursor = None
    with connection:
        _set_state(
            connection,
            "ADMIN_COSTS_SYNCING",
            start=start,
            end=end,
            cursor=cursor,
            error=None,
        )
    pages = buckets = 0
    seen: set[str] = set()
    retrieved = utc_now()
    try:
        while True:
            if cursor and cursor in seen:
                raise ValueError("repeated costs pagination cursor")
            if cursor:
                seen.add(cursor)
            kwargs: dict[str, Any] = {
                "start_time": start,
                "end_time": end,
                "bucket_width": "1d",
                "group_by": config.costs_group_by,
                "limit": 180,
            }
            if cursor:
                kwargs["page"] = cursor
            for attempt in range(max_attempts):
                try:
                    response = client.costs(**kwargs)
                    break
                except Exception as exc:
                    status = _error_status(exc)
                    if (
                        not (
                            status is None
                            or status in {408, 409, 429}
                            or (status is not None and status >= 500)
                        )
                        or attempt == max_attempts - 1
                    ):
                        raise
                    sleep(0.25 * (2**attempt))
            data, more, next_page = _validate_page(response)
            with connection:
                for bucket in data:
                    if _value(bucket, "object") != "bucket":
                        raise ValueError("malformed costs bucket")
                    bucket_start, bucket_end, results = (
                        _value(bucket, "start_time"),
                        _value(bucket, "end_time"),
                        _value(bucket, "results"),
                    )
                    if (
                        not isinstance(bucket_start, int)
                        or not isinstance(bucket_end, int)
                        or not isinstance(results, list)
                    ):
                        raise TypeError("malformed costs bucket")
                    for result in results:
                        _store(
                            connection,
                            _normalize(
                                bucket_start, bucket_end, result, start, end, retrieved
                            ),
                        )
                        buckets += 1
            pages += 1
            with connection:
                _set_state(
                    connection,
                    "ADMIN_COSTS_SYNCING",
                    cursor=next_page if more else None,
                    pages=pages,
                    buckets=buckets,
                )
            if not more:
                break
            cursor = next_page
        with connection:
            _set_state(
                connection,
                "ADMIN_COSTS_HEALTHY",
                completed_start=start,
                completed_end=end,
                cursor=None,
                success=utc_now(),
                error=None,
                pages=pages,
                buckets=buckets,
            )
    except Exception as exc:  # noqa: BLE001 - all adapter failures cross one sanitization boundary.
        safe = sanitize_admin_error(exc)
        with connection:
            _set_state(
                connection,
                "ADMIN_COSTS_FAILED",
                cursor=cursor,
                error=safe,
                pages=pages,
                buckets=buckets,
            )
        raise RuntimeError(safe) from None
    return health(connection, enabled=True, credential_present=True)


def sync(
    connection: Any,
    config: OpenAIAdminConfig,
    client: CostsClient,
    *,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int = 3,
) -> dict[str, Any]:
    if not _SYNC_LOCK.acquire(blocking=False):
        return {
            "collector": COLLECTOR,
            "status": "ADMIN_COSTS_BUSY",
            "reason": "another costs sync is active",
            "last_success": None,
            "last_error": None,
        }
    owner_id = uuid.uuid4().hex
    try:
        if config.costs_enabled and not _acquire_database_lock(connection, owner_id):
            return {
                "collector": COLLECTOR,
                "status": "ADMIN_COSTS_BUSY",
                "reason": "another costs sync is active",
                "last_success": None,
                "last_error": None,
            }
        return _sync(
            connection, config, client, now=now, sleep=sleep, max_attempts=max_attempts
        )
    finally:
        if config.costs_enabled:
            _release_database_lock(connection, owner_id)
        _SYNC_LOCK.release()


def create_client(*, enabled: bool) -> Any | None:
    if not enabled or not os.environ.get("OPENAI_ADMIN_KEY"):
        return None
    from openai import OpenAI

    return OpenAI(
        admin_api_key=os.environ["OPENAI_ADMIN_KEY"], max_retries=0
    ).admin.organization.usage


def read_costs(
    connection: Any,
    *,
    limit: int,
    offset: int = 0,
    start: int | None = None,
    end: int | None = None,
) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    clauses = ["is_current=1"]
    params: list[Any] = []
    if start is not None:
        clauses.append("bucket_end > ?")
        params.append(start)
    if end is not None:
        clauses.append("bucket_start < ?")
        params.append(end)
    rows = connection.execute(
        f"SELECT {','.join(PUBLIC_COST_FIELDS)} FROM openai_costs WHERE {' AND '.join(clauses)} ORDER BY bucket_start DESC,result_identity DESC LIMIT ? OFFSET ?",
        [*params, limit + 1, offset],
    ).fetchall()
    return {
        "items": [
            {key: row[key] for key in PUBLIC_COST_FIELDS} for row in rows[:limit]
        ],
        "next_cursor": str(offset + limit) if len(rows) > limit else None,
        "has_more": len(rows) > limit,
    }
