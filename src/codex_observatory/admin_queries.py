"""Fixed, read-only aggregates over persisted OpenAI Admin evidence."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from .openai_admin import health as usage_health
from .openai_costs import health as costs_health

RANGES: dict[str, timedelta] = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
MAX_GROUPS = 100


def interval(key: str, *, now: datetime | None = None) -> dict[str, Any]:
    if key not in RANGES:
        raise ValueError("range must be one of 24h, 7d, or 30d")
    end = int((now or datetime.now(UTC)).astimezone(UTC).timestamp())
    return {"key": key, "start_time": end - int(RANGES[key].total_seconds()), "end_time": end, "timezone": "UTC"}


def _where(start: int, end: int) -> tuple[str, list[int]]:
    return "is_current=1 AND bucket_end > ? AND bucket_start < ?", [start, end]


def _bucket(row: Any) -> dict[str, int | None]:
    return {"start_time": row["bucket_start"] if row else None, "end_time": row["bucket_end"] if row else None}


def _usage_groups(connection: Any, where: str, params: list[int]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for dimension, column in (("model", "model"), ("project", "project_id"), ("service_tier", "service_tier"), ("batch", "batch")):
        rows = connection.execute(
            f"SELECT {column} AS value, SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, SUM(num_model_requests) AS model_requests, COUNT(*) AS result_count FROM openai_usage_completions WHERE {where} GROUP BY {column} ORDER BY result_count DESC, value LIMIT ?",
            [*params, MAX_GROUPS],
        ).fetchall()
        result.extend({"dimension": dimension, "value": (bool(row["value"]) if dimension == "batch" and row["value"] is not None else row["value"]), "input_tokens": int(row["input_tokens"]), "output_tokens": int(row["output_tokens"]), "model_requests": int(row["model_requests"]), "result_count": int(row["result_count"])} for row in rows)
    return result


def completions_summary(connection: Any, *, range_key: str, enabled: bool, credential_present: bool, now: datetime | None = None) -> dict[str, Any]:
    selected = interval(range_key, now=now)
    where, params = _where(selected["start_time"], selected["end_time"])
    total = connection.execute(f"SELECT SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, SUM(num_model_requests) AS model_requests, COUNT(*) AS result_count FROM openai_usage_completions WHERE {where}", params).fetchone()
    latest = connection.execute(f"SELECT bucket_start, bucket_end FROM openai_usage_completions WHERE {where} ORDER BY bucket_start DESC, bucket_end DESC LIMIT 1", params).fetchone()
    return {"provenance": {"source": "openai_admin_api", "scope": "organization", "attribution": "unavailable"}, "interval": selected, "health": usage_health(connection, enabled=enabled, credential_present=credential_present), "input_tokens": int(total["input_tokens"]) if total["input_tokens"] is not None else None, "output_tokens": int(total["output_tokens"]) if total["output_tokens"] is not None else None, "model_requests": int(total["model_requests"]) if total["model_requests"] is not None else None, "result_count": int(total["result_count"]), "latest_bucket": _bucket(latest), "groups": _usage_groups(connection, where, params)}


def _money(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _cost_groups(connection: Any, where: str, params: list[int]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for dimension, column in (("project", "project_id"), ("line_item", "line_item")):
        rows = connection.execute(f"SELECT {column} AS value, currency, amount_value FROM openai_costs WHERE {where} ORDER BY bucket_start DESC LIMIT ?", [*params, MAX_GROUPS * 10]).fetchall()
        grouped: dict[tuple[str | None, str | None], tuple[Decimal, int]] = defaultdict(lambda: (Decimal(0), 0))
        for row in rows:
            amount = _money(row["amount_value"])
            if amount is None:
                continue
            key = (row["value"], row["currency"])
            current, count = grouped[key]
            grouped[key] = (current + amount, count + 1)
        for (value, currency), (amount, count) in sorted(grouped.items(), key=lambda item: (item[0][1] or "", item[0][0] or ""))[:MAX_GROUPS]:
            result.append({"dimension": dimension, "value": value, "amount": _decimal_text(amount), "currency": currency, "result_count": count})
    return result


def costs_summary(connection: Any, *, range_key: str, enabled: bool, credential_present: bool, now: datetime | None = None) -> dict[str, Any]:
    selected = interval(range_key, now=now)
    where, params = _where(selected["start_time"], selected["end_time"])
    rows = connection.execute(f"SELECT bucket_start, bucket_end, currency, amount_value FROM openai_costs WHERE {where} ORDER BY bucket_start DESC", params).fetchall()
    totals: dict[str | None, Decimal] = defaultdict(Decimal)
    for row in rows:
        amount = _money(row["amount_value"])
        if amount is not None:
            totals[row["currency"]] += amount
    latest = rows[0] if rows else None
    return {"provenance": {"source": "openai_admin_api", "scope": "organization", "attribution": "unavailable"}, "interval": selected, "health": costs_health(connection, enabled=enabled, credential_present=credential_present), "totals": [{"currency": currency, "amount": _decimal_text(amount)} for currency, amount in sorted(totals.items(), key=lambda item: item[0] or "")], "result_count": len(rows), "latest_bucket": _bucket(latest), "groups": _cost_groups(connection, where, params)}


def summary(connection: Any, *, range_key: str, enabled: bool, costs_enabled: bool, credential_present: bool, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    selected = interval(range_key, now=now)
    usage = completions_summary(connection, range_key=range_key, enabled=enabled, credential_present=credential_present, now=now)
    costs = costs_summary(connection, range_key=range_key, enabled=costs_enabled, credential_present=credential_present, now=now)
    return {"provenance": usage["provenance"], "interval": selected, "usage": usage, "costs": costs}
