from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class RawEnvelope:
    ingest_id: str
    received_at: str
    source: str
    source_instance: str
    source_event_type: str
    source_version: str | None
    content_type: str
    content_encoding: str | None
    payload_encoding: str
    payload_sha256: str
    payload_size: int
    payload_ref: str | None
    parse_status: str
    error_code: str | None = None
    error_message: str | None = None
    retention_class: str = "metadata"
    payload: bytes | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class CanonicalEvent:
    event_id: str
    event_time: str
    event_time_unix_nano: int | None
    observed_at: str
    source_event: str
    source_instance: str
    source_version: str | None
    raw_event_sha256: str
    adapter_version: str
    category: str
    name: str
    attributes: dict[str, object]
    session_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None
    call_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    operation_id: str | None = None
    status: str | None = None
