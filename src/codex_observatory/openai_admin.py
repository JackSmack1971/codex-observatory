"""Read-only OpenAI Admin completions usage adapter."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from .admin_sync_lock import acquire as acquire_database_lock
from .admin_sync_lock import release as release_database_lock
from .config import OpenAIAdminConfig
from .sqlite import utc_now

ADAPTER_SCHEMA_VERSION = "openai-admin-completions-v1"
COLLECTOR = "openai_admin"
ENDPOINT = "/organization/usage/completions"
HEALTH_STATES = {"ADMIN_DISABLED", "ADMIN_CREDENTIAL_MISSING", "ADMIN_READY", "ADMIN_SYNCING", "ADMIN_HEALTHY", "ADMIN_DEGRADED", "ADMIN_FAILED"}
TOKEN_FIELDS = (
    "input_audio_tokens", "input_cache_write_tokens", "input_cached_audio_tokens", "input_cached_image_tokens",
    "input_cached_text_tokens", "input_cached_tokens", "input_image_tokens", "input_text_tokens",
    "input_uncached_tokens", "output_audio_tokens", "output_image_tokens", "output_text_tokens",
)
GROUP_FIELDS = ("project_id", "user_id", "api_key_id", "model", "batch", "service_tier")
PUBLIC_USAGE_FIELDS = (
    "result_identity", "revision", "source", "usage_family", "bucket_start", "bucket_end", "bucket_width",
    *GROUP_FIELDS, "input_tokens", "output_tokens", "num_model_requests", *TOKEN_FIELDS,
    "request_start", "request_end", "retrieved_at", "adapter_schema_version", "first_observed_at", "last_observed_at",
)
_SYNC_LOCK = threading.Lock()
_LOCK_TABLE = "openai_admin_sync_lock"
_AUTHORIZATION_VALUE = re.compile(r"(?i)(authorization\s*[:=]\s*[\"']?(?:bearer\s+)?)[^\"'\s,}\]]+(?:[\"'])?")


class SanitizedAdminError(RuntimeError):
    """An Admin error safe to persist and render outside the adapter."""


def sanitize_admin_error(value: object) -> str:
    """Remove credential material from one externally rendered Admin error."""
    message = str(value)
    secret = os.environ.get("OPENAI_ADMIN_KEY")
    if secret:
        message = message.replace(secret, "[REDACTED]")
    return _AUTHORIZATION_VALUE.sub(r"\1[REDACTED]", message)


class UsageClient(Protocol):
    def completions(self, **kwargs: Any) -> Any: ...


def _value(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _error_status(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def redact_error(value: str) -> str:
    """Backward-compatible name for the central Admin error boundary."""
    return sanitize_admin_error(value)


def _validate_page(page: Any) -> tuple[list[Any], bool, str | None]:
    data = _value(page, "data")
    has_more = _value(page, "has_more")
    next_page = _value(page, "next_page")
    if not isinstance(data, list) or not isinstance(has_more, bool) or (next_page is not None and not isinstance(next_page, str)):
        raise ValueError("malformed completions usage response")
    if has_more and not next_page:
        raise ValueError("usage response has_more without next_page")
    return data, has_more, next_page


def health(connection: Any, *, enabled: bool, credential_present: bool) -> dict[str, Any]:
    if not enabled:
        return {"collector": COLLECTOR, "status": "ADMIN_DISABLED", "reason": "collector disabled", "last_success": None, "last_error": None}
    if not credential_present:
        return {"collector": COLLECTOR, "status": "ADMIN_CREDENTIAL_MISSING", "reason": "OPENAI_ADMIN_KEY is absent", "last_success": None, "last_error": None}
    row = connection.execute("SELECT * FROM openai_admin_sync_state WHERE collector=?", (COLLECTOR,)).fetchone()
    if not row:
        return {"collector": COLLECTOR, "status": "ADMIN_READY", "reason": "credential available; no sync completed", "last_success": None, "last_error": None}
    return {
        "collector": row["collector"],
        "status": row["status"],
        "reason": "last synchronization state",
        "last_success": row["last_success"],
        "last_error": sanitize_admin_error(row["last_error"]) if row["last_error"] is not None else None,
    }


def _set_state(connection: Any, status: str, *, start: int | None = None, end: int | None = None,
               completed_start: int | None = None, completed_end: int | None = None,
               cursor: str | None = None, success: str | None = None, error: str | None = None,
               pages: int | None = None, buckets: int | None = None) -> None:
    old = connection.execute("SELECT * FROM openai_admin_sync_state WHERE collector=?", (COLLECTOR,)).fetchone()
    def oldv(key: str, default: Any = None) -> Any: return old[key] if old else default
    connection.execute(
        """INSERT INTO openai_admin_sync_state(collector,status,requested_start,requested_end,completed_start,completed_end,page_cursor,last_success,last_error,pages_total,buckets_total,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(collector) DO UPDATE SET status=excluded.status,requested_start=excluded.requested_start,requested_end=excluded.requested_end,
        completed_start=excluded.completed_start,completed_end=excluded.completed_end,page_cursor=excluded.page_cursor,last_success=excluded.last_success,last_error=excluded.last_error,
        pages_total=excluded.pages_total,buckets_total=excluded.buckets_total,updated_at=excluded.updated_at""",
        (COLLECTOR, status, start if start is not None else oldv("requested_start"), end if end is not None else oldv("requested_end"),
         completed_start if completed_start is not None else oldv("completed_start"), completed_end if completed_end is not None else oldv("completed_end"),
         cursor, success if success is not None else oldv("last_success"), error, pages if pages is not None else oldv("pages_total", 0),
         buckets if buckets is not None else oldv("buckets_total", 0), utc_now()),
    )


def _identity(bucket_start: int, bucket_end: int, result: Any) -> str:
    dimensions = {key: _value(result, key) for key in GROUP_FIELDS}
    payload = json.dumps({"bucket_start": bucket_start, "bucket_end": bucket_end, "dimensions": dimensions}, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def _normalize(bucket_start: int, bucket_end: int, result: Any, start: int, end: int, width: str, retrieved: str) -> dict[str, Any]:
    object_name = _value(result, "object")
    if object_name != "organization.usage.completions.result":
        raise ValueError("unexpected completions usage result object")
    required = {key: _value(result, key) for key in ("input_tokens", "output_tokens", "num_model_requests")}
    if any(not isinstance(value, int) or value < 0 for value in required.values()):
        raise ValueError("invalid required completions usage value")
    row = {"result_identity": _identity(bucket_start, bucket_end, result), "source": "openai_admin_api", "usage_family": "completions",
           "bucket_start": bucket_start, "bucket_end": bucket_end, "bucket_width": width, "request_start": start, "request_end": end,
           "retrieved_at": retrieved, "adapter_schema_version": ADAPTER_SCHEMA_VERSION, "input_tokens": required["input_tokens"],
           "output_tokens": required["output_tokens"], "num_model_requests": required["num_model_requests"]}
    for key in GROUP_FIELDS + TOKEN_FIELDS:
        value = _value(result, key)
        if value is not None and key != "batch" and not isinstance(value, (str, int)):
            raise ValueError(f"invalid completions usage field: {key}")
        row[key] = value
    row["batch"] = None if row["batch"] is None else int(bool(row["batch"]))
    canonical = json.dumps({key: value for key, value in row.items() if key not in {"retrieved_at", "request_start", "request_end"}}, sort_keys=True, separators=(",", ":"))
    row["result_sha256"] = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    return row


def _store(connection: Any, row: dict[str, Any]) -> bool:
    current = connection.execute("SELECT * FROM openai_usage_completions WHERE result_identity=? AND is_current=1", (row["result_identity"],)).fetchone()
    if current and current["result_sha256"] == row["result_sha256"]:
        connection.execute("UPDATE openai_usage_completions SET last_observed_at=? WHERE result_identity=? AND is_current=1", (row["retrieved_at"], row["result_identity"]))
        return False
    revision = int(current["revision"]) + 1 if current else 1
    if current:
        connection.execute("UPDATE openai_usage_completions SET is_current=0 WHERE result_identity=? AND is_current=1", (row["result_identity"],))
    values = [row.get(key) for key in ("result_identity", "source", "usage_family", "bucket_start", "bucket_end", "bucket_width", *GROUP_FIELDS,
        "input_tokens", "output_tokens", "num_model_requests", *TOKEN_FIELDS, "request_start", "request_end", "retrieved_at",
        "adapter_schema_version", "result_sha256")]
    connection.execute(f"INSERT INTO openai_usage_completions(result_identity,revision,source,usage_family,bucket_start,bucket_end,bucket_width,{','.join(GROUP_FIELDS)},input_tokens,output_tokens,num_model_requests,{','.join(TOKEN_FIELDS)},request_start,request_end,retrieved_at,adapter_schema_version,result_sha256,first_observed_at,last_observed_at,is_current) VALUES ({','.join('?' for _ in range(len(values)+4))})",
                       [values[0], revision, *values[1:], row["retrieved_at"], row["retrieved_at"], 1])
    return True


def _sync(connection: Any, config: OpenAIAdminConfig, client: UsageClient, *, now: datetime | None = None,
          sleep: Callable[[float], None] = time.sleep, max_attempts: int = 3) -> dict[str, Any]:
    """Synchronize one bounded window. Only the injected official SDK client performs network I/O."""
    if not config.enabled:
        _set_state(connection, "ADMIN_DISABLED")
        return health(connection, enabled=False, credential_present=False)
    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    end = int(current_time.timestamp())
    state = connection.execute("SELECT * FROM openai_admin_sync_state WHERE collector=?", (COLLECTOR,)).fetchone()
    if state and state["page_cursor"] and state["requested_end"]:
        start, end, cursor = state["requested_start"], state["requested_end"], state["page_cursor"]
    else:
        start = end - config.initial_lookback_hours * 3600
        if state and state["completed_end"] is not None:
            start = int(state["completed_end"]) - config.overlap_hours * 3600
        cursor = None
    retrieved = utc_now()
    with connection:
        _set_state(connection, "ADMIN_SYNCING", start=start, end=end, cursor=cursor, error=None)
    pages = 0
    buckets = 0
    seen: set[str] = set()
    try:
        while True:
            if cursor and cursor in seen:
                raise ValueError("repeated pagination cursor")
            if cursor:
                seen.add(cursor)
            kwargs: dict[str, Any] = {"start_time": start, "end_time": end, "bucket_width": config.bucket_width, "group_by": config.group_by}
            if cursor:
                kwargs["page"] = cursor
            response = None
            for attempt in range(max_attempts):
                try:
                    response = client.completions(**kwargs)
                    break
                except Exception as exc:  # SDK exceptions expose status_code without leaking request headers.
                    status = _error_status(exc)
                    retryable = status is None or status in {408, 409, 429} or (status is not None and status >= 500)
                    if not retryable or attempt == max_attempts - 1:
                        raise
                    sleep(0.25 * (2 ** attempt))
            data, has_more, next_page = _validate_page(response)
            with connection:
                for bucket in data:
                    bucket_start = _value(bucket, "start_time")
                    bucket_end = _value(bucket, "end_time")
                    results = _value(bucket, "results")
                    if not isinstance(bucket_start, int) or not isinstance(bucket_end, int) or not isinstance(results, list):
                        raise TypeError("malformed completions usage bucket")
                    for result in results:
                        _store(connection, _normalize(bucket_start, bucket_end, result, start, end, config.bucket_width, retrieved))
                        buckets += 1
            pages += 1
            _set_state(connection, "ADMIN_SYNCING", cursor=next_page if has_more else None, pages=pages, buckets=buckets)
            if not has_more:
                break
            cursor = next_page
        with connection:
            _set_state(connection, "ADMIN_HEALTHY", completed_start=start, completed_end=end, cursor=None, success=utc_now(), error=None, pages=pages, buckets=buckets)
    except Exception as exc:  # noqa: BLE001 - all adapter failures cross one sanitization boundary.
        safe_error = sanitize_admin_error(exc)
        if "OPENAI_ADMIN_KEY" in safe_error:
            safe_error = "Admin authentication failure"
        with connection:
            _set_state(connection, "ADMIN_FAILED", cursor=cursor, error=safe_error, pages=pages, buckets=buckets)
        try:
            safe_exception = type(exc)(safe_error)
        except Exception:  # noqa: BLE001 - fallback must remain inside the sanitization boundary.
            safe_exception = SanitizedAdminError(safe_error)
        raise safe_exception from None
    return health(connection, enabled=True, credential_present=True)


def sync(connection: Any, config: OpenAIAdminConfig, client: UsageClient, *, now: datetime | None = None,
          sleep: Callable[[float], None] = time.sleep, max_attempts: int = 3) -> dict[str, Any]:
    """Run one Admin sync, failing fast when another process syncs it."""
    if not _SYNC_LOCK.acquire(blocking=False):
        return {"collector": COLLECTOR, "status": "ADMIN_BUSY", "reason": "another Admin sync is active", "last_success": None, "last_error": None}
    owner_id = uuid.uuid4().hex
    try:
        if config.enabled and not acquire_database_lock(connection, _LOCK_TABLE, owner_id):
            return {"collector": COLLECTOR, "status": "ADMIN_BUSY", "reason": "another Admin sync is active", "last_success": None, "last_error": None}
        return _sync(connection, config, client, now=now, sleep=sleep, max_attempts=max_attempts)
    finally:
        if config.enabled:
            release_database_lock(connection, _LOCK_TABLE, owner_id)
        _SYNC_LOCK.release()


def create_client(*, enabled: bool) -> Any | None:
    if not enabled:
        return None
    key = os.environ.get("OPENAI_ADMIN_KEY")
    if not key:
        return None
    from openai import OpenAI
    return OpenAI(admin_api_key=key, max_retries=0).admin.organization.usage


def read_usage(connection: Any, *, limit: int, offset: int = 0, start: int | None = None, end: int | None = None) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    clauses = ["is_current=1"]
    params: list[Any] = []
    if start is not None:
        clauses.append("bucket_end > ?"); params.append(start)
    if end is not None:
        clauses.append("bucket_start < ?"); params.append(end)
    where = " AND ".join(clauses)
    fields = ",".join(PUBLIC_USAGE_FIELDS)
    rows = connection.execute(f"SELECT {fields} FROM openai_usage_completions WHERE {where} ORDER BY bucket_start DESC,result_identity DESC LIMIT ? OFFSET ?", [*params, limit + 1, offset]).fetchall()
    items = []
    for row in rows[:limit]:
        item = {key: row[key] for key in PUBLIC_USAGE_FIELDS}
        if item["batch"] is not None:
            item["batch"] = bool(item["batch"])
        items.append(item)
    return {"items": items, "next_cursor": str(offset + limit) if len(rows) > limit else None, "has_more": len(rows) > limit}
