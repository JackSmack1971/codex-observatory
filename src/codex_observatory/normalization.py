from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
)
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.metrics.v1.metrics_pb2 import Metric

from .models import CanonicalEvent

ADAPTER_VERSION = "otel-adapter/1"
PROMPT_KEYS = re.compile(r"prompt|tool.?arguments?|tool.?output|output.?snippet", re.IGNORECASE)
KNOWN = {"codex.conversation_starts", "codex.api_request", "codex.sse_event", "codex.websocket_request", "codex.websocket_event", "codex.user_prompt", "codex.tool_decision", "codex.tool_result"}


def _value(value: Any) -> object:
    if hasattr(value, "WhichOneof"):
        kind = value.WhichOneof("value")
        return _value(getattr(value, kind)) if kind else None
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _attrs(attributes: Any) -> dict[str, object]:
    result: dict[str, object] = {}
    items = attributes.items() if hasattr(attributes, "items") else ((item.key, item.value) for item in attributes)
    for key, value in items:
        if not PROMPT_KEYS.search(key):
            result[key] = _value(value)
    return result


def _event_time(nano: int | None) -> str:
    if not nano:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return datetime.fromtimestamp(nano / 1_000_000_000, UTC).isoformat().replace("+00:00", "Z")


def _common(source_version: str | None, digest: str, now: str, name: str, attrs: dict[str, object], time_nano: int | None, *, trace_id: str | None = None, span_id: str | None = None) -> CanonicalEvent:
    return CanonicalEvent(str(uuid.uuid4()), _event_time(time_nano), time_nano, now, name, "local-default", source_version, f"sha256:{digest}", ADAPTER_VERSION,
                          "unknown" if name == "unknown" else ("metric" if name.startswith("metric:") else "log"), name, attrs, trace_id=trace_id, span_id=span_id)


def normalize(signal: str, message: object, *, source_version: str | None, digest: str, now: str) -> tuple[list[CanonicalEvent], int]:
    events: list[CanonicalEvent] = []
    unknown = 0
    if signal == "logs":
        for resource_logs in cast(ExportLogsServiceRequest, message).resource_logs:
            resource = _attrs(resource_logs.resource.attributes)
            version = source_version or str(resource.get("service.version", "")) or None
            for scope_logs in resource_logs.scope_logs:
                for record in scope_logs.log_records:
                    attrs = _attrs(record.attributes)
                    name = str(attrs.get("event.name") or attrs.get("event_name") or attrs.get("name") or "unknown")
                    if name not in KNOWN:
                        unknown += 1
                    events.append(_common(version, digest, now, name, attrs, record.time_unix_nano or record.observed_time_unix_nano))
    elif signal == "metrics":
        for resource_metrics in cast(ExportMetricsServiceRequest, message).resource_metrics:
            for scope_metrics in resource_metrics.scope_metrics:
                for metric in scope_metrics.metrics:
                    events.append(_metric_event(metric, source_version, digest, now))
    else:
        for resource_spans in cast(ExportTraceServiceRequest, message).resource_spans:
            for scope_spans in resource_spans.scope_spans:
                for span in scope_spans.spans:
                    attrs = _attrs(span.attributes)
                    events.append(_common(source_version, digest, now, f"span:{span.name or 'unknown'}", attrs, span.start_time_unix_nano, trace_id=span.trace_id.hex(), span_id=span.span_id.hex()))
    return events, unknown


def _metric_event(metric: Metric, source_version: str | None, digest: str, now: str) -> CanonicalEvent:
    attrs: dict[str, object] = {"metric_name": metric.name, "unit": metric.unit, "description": metric.description}
    kind = metric.WhichOneof("data") or "unknown"
    attrs["metric_type"] = kind
    if kind == "sum":
        attrs.update({"is_monotonic": metric.sum.is_monotonic, "aggregation_temporality": metric.sum.aggregation_temporality})
        points = metric.sum.data_points
    elif kind == "gauge":
        points = metric.gauge.data_points
    elif kind == "histogram":
        points = metric.histogram.data_points
    else:
        points = []
    attrs["recognized_codex_metric"] = metric.name in {"codex.api_request", "codex.api_request.duration_ms", "codex.tool.call", "codex.tool.call.duration_ms"}
    if points:
        point = points[0]
        if kind in {"sum", "gauge"}:
            attrs["value"] = point.as_double if point.WhichOneof("value") == "as_double" else point.as_int
        else:
            attrs["count"] = point.count
            attrs["sum"] = point.sum
            attrs["bucket_counts"] = list(point.bucket_counts)
            attrs["explicit_bounds"] = list(point.explicit_bounds)
        attrs["attributes"] = _attrs(point.attributes)
        time_nano = point.time_unix_nano
    else:
        time_nano = None
    return _common(source_version, digest, now, f"metric:{metric.name}", attrs, time_nano)


def message_type(signal: str) -> type[Any]:
    types: dict[str, type[Any]] = {"logs": ExportLogsServiceRequest, "metrics": ExportMetricsServiceRequest, "traces": ExportTraceServiceRequest}
    return types[signal]
